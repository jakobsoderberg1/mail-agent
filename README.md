# mail-agent

Triages your unread Gmail every 15 minutes on Cloud Run. [TypeSafe Jev](https://docs.typesafe.ai)
scores every email (important? worth a summary line? contains an event?) and Claude Haiku is
only called to pull out event details when Jev thinks there is one. Thresholds are at the top
of `agent.py`; `--dry-run` prints each email's scores so you can tune them.

- **Important email** → instant Telegram alert (left unread in the inbox)
- **Everything else** → marked read, one-line summary saved for the debrief
- **Events / deadlines** → proposed as calendar events with ✅ Add / ❌ Skip buttons.
  Sent right away if the event is before the next debrief, otherwise in the debrief.
- **Morning debrief** → 07:00 Mon–Fri (Europe/Stockholm) on Telegram: alerts, calendar
  changes, every email handled, and pending approvals. Weekend activity rolls into Monday.

Button taps are picked up by the next 15-minute run (no web server needed), so it can take
up to 15 minutes for an approved event to appear.

## Setup

1. **Google Cloud project**: create one, then in *APIs & Services*:
   - *OAuth consent screen*: External, add yourself as a test user, then **Publish app**
     ("In production"). In *Testing* mode refresh tokens expire after 7 days and the agent
     stops working. Unverified is fine for personal use.
   - *Credentials*: create an OAuth client ID of type **Desktop app**, download it as
     `credentials.json` into this folder.
2. **Telegram**: message [@BotFather](https://t.me/BotFather), `/newbot`, put the token in
   `.env`. Send your new bot any message, then `uv run main.py chat-id` and put the id in `.env`.
3. **Anthropic / TypeSafe**: put `ANTHROPIC_API_KEY` (a key created inside a workspace;
   keys not scoped to a workspace are rejected) and `TYPESAFE_API_KEY`
   ([console.typesafe.ai/keys](https://console.typesafe.ai/keys)) in `.env`.
4. **Google login**: `uv run main.py auth` (opens a browser, writes `token.json`).
5. **Deploy**: `PROJECT_ID=your-project ./deploy.sh`

Run once to test: `gcloud run jobs execute mail-agent-triage --region=europe-west1 --wait`

## Dry run

`uv run main.py triage --dry-run` reads your real inbox and calendar and runs Claude, but
writes nothing (no mark-as-read, labels, events, Firestore or Telegram). It prints the
alerts and approval requests it would send, plus a debrief preview. Only needs
`ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY` and the Google login, no Telegram or Cloud setup.

## Local runs

Set `GOOGLE_CLOUD_PROJECT` in `.env` and run `gcloud auth application-default login` (for
Firestore), then `uv run main.py triage` or `uv run main.py debrief`. Local runs share state
with the deployed jobs.

## Notes

- Handled emails get a hidden `mail-agent-processed` Gmail label so they're never re-triaged.
- If a run fails you get one Telegram alert per distinct error, not one every 15 minutes.
- After re-running `auth`, re-run `deploy.sh` to upload the new token.
