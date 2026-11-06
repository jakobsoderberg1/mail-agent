"""Email agent entry point.

    uv run main.py auth       # one-time Google login (local), writes token.json
    uv run main.py chat-id    # print your Telegram chat id after messaging the bot
    uv run main.py triage     # every 15 min: handle approvals + new email
    uv run main.py triage --dry-run   # read-only: print what it would do
    uv run main.py debrief    # weekday mornings: summary + events that waited for it
    uv run main.py run        # every 15 min on Railway: triage, then debrief when due
"""

import html
import sys
import traceback
from datetime import datetime, timedelta
from email.utils import parseaddr

from dotenv import load_dotenv

load_dotenv()

import agent  # noqa: E402
import google_api  # noqa: E402
import store  # noqa: E402
import telegram_bot  # noqa: E402


def process_approvals() -> None:
    """Apply Approve/Skip taps made on Telegram since the last run."""
    offset = store.get_state().get("telegram_offset", 0)
    for update in telegram_bot.get_updates(offset):
        offset = update["update_id"] + 1
        cq = update.get("callback_query")
        if not cq or str(cq["message"]["chat"]["id"]) != telegram_bot.chat_id():
            continue
        action, _, proposal_id = cq.get("data", "").partition(":")
        approve = action == "approve"
        p = store.claim_proposal(proposal_id, "approved" if approve else "declined")
        original = cq["message"].get("text", "")
        if p is None:
            telegram_bot.answer_callback(cq["id"], "Already handled")
            continue
        if not approve:
            store.log("event_declined", summary=p["summary"], start=p["start"])
            telegram_bot.edit_message(cq["message"], f"{original}\n\n❌ Skipped")
            continue
        try:
            link = google_api.create_event(p)
        except Exception as e:
            store.set_proposal_status(proposal_id, "pending")
            telegram_bot.send(f"⚠️ Couldn't add <b>{html.escape(p['summary'])}</b>: "
                              f"{html.escape(str(e))}. Tap Add again to retry.")
            continue
        store.log("event_created", summary=p["summary"], start=p["start"], link=link)
        telegram_bot.edit_message(cq["message"], f"{original}\n\n✅ Added to calendar")
    store.set_state(telegram_offset=offset)


def expire_proposals() -> None:
    """Expire pending proposals whose event has started, and take the buttons off their
    Telegram message. All-day events start at 00:00 local, so they expire that midnight."""
    now = store.now()
    for proposal_id, p in store.pending_proposals():
        starts = agent.proposal_start(p)
        if not starts or starts >= now:
            continue
        if store.claim_proposal(proposal_id, "expired") is None:
            continue  # tapped in the meantime
        if message_id := p.get("telegram_message_id"):
            message = {"chat": {"id": telegram_bot.chat_id()}, "message_id": message_id}
            telegram_bot.edit_message(message, f"{agent.format_proposal(p)}\n\n⌛ Expired",
                                      html=True)


def triage(dry_run: bool = False) -> None:
    if dry_run:
        agent.DRY_RUN = True
        print("[dry-run] Reading only: no changes to Gmail, Calendar, Firestore or Telegram.")
    else:
        process_approvals()
        expire_proposals()
    emails = google_api.unprocessed_unread(dry_run=dry_run)
    if not emails:
        print("No new email.")
        return
    agent.run_triage(emails)
    if dry_run:
        unsent = [i["proposal"] for i in agent.dry_activity
                  if i["kind"] == "dry_proposal" and not i["proposal"].get("sent_at")]
        print("\n[dry-run] Debrief preview:\n")
        print(format_debrief(agent.dry_activity, store.now(), len(unsent)))
        for i, p in enumerate(unsent):
            agent.send_proposal(f"dry-{i}", p)


def _mail_line(t: dict) -> str:
    sender = parseaddr(t["sender"])[0] or parseaddr(t["sender"])[1]
    return f"{html.escape(sender)}: {html.escape(t['summary'])}"


def format_debrief(items: list[dict], since, new_proposals: int) -> str:
    by_kind = lambda kind: [i for i in items if i["kind"] == kind]  # noqa: E731

    triaged = by_kind("triaged")
    notified = by_kind("notified")
    created, declined = by_kind("event_created"), by_kind("event_declined")

    lines = [f"☀️ <b>Morning debrief</b> since {since.astimezone(agent.TZ):%a %H:%M}"]
    if not items:
        lines.append("Quiet: no new email.")
    else:
        read_count = sum(1 for t in triaged if t.get("marked_read"))
        lines.append(f"📬 {len(triaged)} emails triaged, {read_count} marked read")
    if notified:
        lines.append(f"\n🔔 <b>Important ({len(notified)})</b>")
        lines += [f"• {html.escape(n['title'])}: {html.escape(n['message'])}" for n in notified]
    if created or declined:
        lines.append("\n📅 <b>Calendar</b>")
        lines += [f"• Added: {html.escape(e['summary'])} ({e['start']})" for e in created]
        lines += [f"• Skipped: {html.escape(e['summary'])}" for e in declined]
    # Below the alert threshold, only bills stay unread.
    bills = [t for t in triaged if not t.get("marked_read")
             and t.get("scores", {}).get("important", 1) < agent.ALERT_AT]
    if bills:
        lines.append("\n💳 <b>Bills</b> (left unread)")
        lines += [f"• {_mail_line(t)}" for t in bills]
    alerted = {n["message_id"] for n in notified}
    listed = [t for t in triaged if t.get("in_summary", True) and t not in bills
              and t["message_id"] not in alerted]
    if listed:
        lines.append("\n🗂 <b>Mail</b> (✓ = marked read)")
        lines += [f"{'✓' if t.get('marked_read') else '•'} {_mail_line(t)}" for t in listed]
    if hidden := len(triaged) - len(listed) - len(bills) - len(alerted & {t["message_id"] for t in triaged}):
        lines.append(f"\n+ {hidden} newsletters/promotions marked read")
    if new_proposals:
        lines.append(f"\n📅 <b>{new_proposals} new event(s) to review</b> below")
    return "\n".join(lines)


def debrief() -> None:
    process_approvals()
    now = store.now()
    since = store.get_state().get("last_debrief_at") or now - timedelta(days=1)
    items = store.activity_since(since)

    expire_proposals()
    # Only proposals that waited for the debrief; ones already sent are never re-sent.
    # A missing sent_at (proposals from before it existed) counts as already sent.
    unsent = [(pid, p) for pid, p in store.pending_proposals()
              if "sent_at" in p and p["sent_at"] is None]

    telegram_bot.send(format_debrief(items, since, len(unsent)))
    for proposal_id, p in unsent:
        agent.send_proposal(proposal_id, p)
    store.set_state(last_debrief_at=now)


def debrief_due(now: datetime) -> bool:
    """True on weekdays from DEBRIEF_HOUR (Stockholm time) until today's debrief is sent."""
    local = now.astimezone(agent.TZ)
    if local.weekday() >= 5 or local.hour < agent.DEBRIEF_HOUR:
        return False
    last = store.get_state().get("last_debrief_at")
    return last is None or last.astimezone(agent.TZ).date() < local.date()


def run() -> None:
    """One scheduler, one job: Railway cron is UTC-only and one schedule per service,
    so the debrief rides along with triage and stays at 07:00 local across DST."""
    triage()
    if debrief_due(store.now()):
        debrief()


def print_chat_ids() -> None:
    for update in telegram_bot.get_updates(0):
        if chat := update.get("message", {}).get("chat"):
            print(f"{chat['id']}  ({chat.get('first_name', '')} {chat.get('username', '')})")


def report_failure(command: str, error: Exception) -> None:
    """Tell the user on Telegram, but only once per distinct error so a broken
    token doesn't page them every 15 minutes."""
    summary = f"{type(error).__name__}: {error}"[:500]
    try:
        if store.get_state().get("last_error") == summary:
            return
        store.set_state(last_error=summary)
    except Exception:
        pass
    try:
        telegram_bot.send(f"⚠️ mail-agent <b>{command}</b> failed\n<code>{html.escape(summary)}</code>")
    except Exception:
        pass


COMMANDS = {
    "auth": google_api.run_auth_flow,
    "chat-id": print_chat_ids,
    "triage": triage,
    "debrief": debrief,
    "run": run,
}


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    command = next((a for a in args if not a.startswith("--")), "")
    if command not in COMMANDS or (dry_run and command != "triage"):
        sys.exit(__doc__)
    reports = command in ("triage", "debrief", "run") and not dry_run
    try:
        COMMANDS[command](dry_run=True) if dry_run else COMMANDS[command]()
    except Exception as e:
        traceback.print_exc()
        if reports:
            report_failure(command, e)
        sys.exit(1)
    if reports:
        store.set_state(last_error=None)


if __name__ == "__main__":
    main()
