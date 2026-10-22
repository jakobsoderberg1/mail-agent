"""Minimal Telegram Bot API client for alerts, the debrief and approval buttons."""

import os

import requests

MAX_MESSAGE_CHARS = 4000  # Telegram's limit is 4096


def chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _call(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def _chunks(text: str) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > MAX_MESSAGE_CHARS and current:
            chunks.append(current)
            current = ""
        current += line[:MAX_MESSAGE_CHARS]
    return chunks + [current] if current else chunks


def send(text: str, buttons: list[list[dict]] | None = None) -> None:
    """Send an HTML-formatted message; long text is split, buttons go on the last part."""
    chunks = _chunks(text)
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id(), "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if buttons and i == len(chunks) - 1:
            params["reply_markup"] = {"inline_keyboard": buttons}
        _call("sendMessage", **params)


def get_updates(offset: int) -> list[dict]:
    return _call("getUpdates", offset=offset, timeout=0,
                 allowed_updates=["message", "callback_query"])


def answer_callback(callback_id: str, text: str) -> None:
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except RuntimeError:
        pass  # queries older than a few minutes can't be answered; that's fine


def edit_message(message: dict, text: str) -> None:
    """Replace a message's text, which also removes its buttons."""
    try:
        _call("editMessageText", chat_id=message["chat"]["id"],
              message_id=message["message_id"], text=text, disable_web_page_preview=True)
    except RuntimeError:
        pass
