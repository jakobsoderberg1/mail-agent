"""Gmail and Google Calendar access for a single personal account."""

import base64
import html
import json
import os
import re
from functools import cache

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read, mark as read, label
    "https://www.googleapis.com/auth/calendar.events",
]
PROCESSED_LABEL = "mail-agent-processed"
MAX_BODY_CHARS = 20_000


def run_auth_flow() -> None:
    """Interactive browser login; run locally once, then upload token.json as a secret."""
    flow = InstalledAppFlow.from_client_secrets_file(
        os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"), SCOPES
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    with open(os.getenv("GOOGLE_TOKEN_FILE", "token.json"), "w") as f:
        f.write(creds.to_json())


@cache
def _credentials() -> Credentials:
    # In Cloud Run the token comes from Secret Manager; locally from token.json.
    if token_json := os.getenv("GOOGLE_TOKEN_JSON"):
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    else:
        token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
        if not os.path.exists(token_file):
            raise RuntimeError("No Google token found. Run `uv run main.py auth` first.")
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return creds


@cache
def gmail():
    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


@cache
def calendar():
    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html with tags stripped."""

    def find(part: dict, mime: str) -> str | None:
        if part.get("mimeType") == mime and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
        for sub in part.get("parts", []):
            if found := find(sub, mime):
                return found
        return None

    if text := find(payload, "text/plain"):
        return text
    if markup := find(payload, "text/html"):
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return ""


def _find_label_id() -> str | None:
    labels = gmail().users().labels().list(userId="me").execute().get("labels", [])
    return next((label["id"] for label in labels if label["name"] == PROCESSED_LABEL), None)


@cache
def _processed_label_id() -> str:
    if label_id := _find_label_id():
        return label_id
    created = gmail().users().labels().create(
        userId="me",
        body={"name": PROCESSED_LABEL, "labelListVisibility": "labelHide"},
    ).execute()
    return created["id"]


def unprocessed_unread(limit: int = 50, dry_run: bool = False) -> list[dict]:
    """Unread inbox emails the agent hasn't handled yet, with basic metadata.
    A dry run never creates the label, so it only filters on it if it already exists."""
    query = "is:unread in:inbox"
    if not dry_run or _find_label_id():
        _processed_label_id()  # make sure the label exists before querying on it
        query += f" -label:{PROCESSED_LABEL}"
    resp = gmail().users().messages().list(
        userId="me", q=query, maxResults=limit
    ).execute()
    emails = []
    for ref in resp.get("messages", []):
        msg = gmail().users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        h = msg["payload"]["headers"]
        emails.append({
            "id": msg["id"],
            "from": _header(h, "From"),
            "subject": _header(h, "Subject"),
            "date": _header(h, "Date"),
            "snippet": msg.get("snippet", ""),
        })
    return emails


def read_email(message_id: str) -> str:
    msg = gmail().users().messages().get(userId="me", id=message_id, format="full").execute()
    h = msg["payload"]["headers"]
    body = _extract_body(msg["payload"])
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n[... truncated, {len(body)} chars total]"
    return (
        f"From: {_header(h, 'From')}\nTo: {_header(h, 'To')}\n"
        f"Subject: {_header(h, 'Subject')}\nDate: {_header(h, 'Date')}\n\n{body}"
    )


def mark_as_read(message_ids: list[str]) -> None:
    gmail().users().messages().batchModify(
        userId="me", body={"ids": message_ids, "removeLabelIds": ["UNREAD"]}
    ).execute()


def label_processed(message_ids: list[str]) -> None:
    gmail().users().messages().batchModify(
        userId="me", body={"ids": message_ids, "addLabelIds": [_processed_label_id()]}
    ).execute()


def list_events(time_min: str, time_max: str) -> list[dict]:
    resp = calendar().events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime",
    ).execute()
    return resp.get("items", [])


def create_event(p: dict) -> str:
    """Create an event from a stored proposal; returns its Calendar link."""
    key = "date" if p.get("all_day") else "dateTime"
    event = calendar().events().insert(
        calendarId="primary",
        body={
            "summary": p["summary"],
            "description": p.get("description", ""),
            "location": p.get("location", ""),
            "start": {key: p["start"]},
            "end": {key: p["end"]},
        },
    ).execute()
    return event.get("htmlLink", "")
