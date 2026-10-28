"""Firestore-backed state: run cursors, the activity log and calendar proposals."""

import json
import os
from datetime import datetime, timezone
from functools import cache

from google.cloud import firestore
from google.oauth2 import service_account
from google.cloud.firestore import FieldFilter


@cache
def db() -> firestore.Client:
    # Off Google Cloud (e.g. Railway) a service account key comes in via the environment;
    # on Cloud Run and locally, application default credentials are used.
    if key_json := os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(info)
        return firestore.Client(project=info["project_id"], credentials=creds)
    return firestore.Client()


def now() -> datetime:
    return datetime.now(timezone.utc)


def _state_ref():
    return db().collection("state").document("main")


def get_state() -> dict:
    snap = _state_ref().get()
    return snap.to_dict() if snap.exists else {}


def set_state(**fields) -> None:
    _state_ref().set(fields, merge=True)


def log(kind: str, **fields) -> None:
    """Record something the agent did, for the morning debrief."""
    db().collection("activity").add({"at": now(), "kind": kind, **fields})


def activity_since(since: datetime) -> list[dict]:
    query = (
        db().collection("activity")
        .where(filter=FieldFilter("at", ">=", since))
        .order_by("at")
    )
    return [doc.to_dict() for doc in query.stream()]


def add_proposal(proposal: dict) -> str:
    ref = db().collection("proposals").document()
    ref.set({**proposal, "status": "pending", "created_at": now()})
    return ref.id


def pending_proposals() -> list[tuple[str, dict]]:
    query = db().collection("proposals").where(filter=FieldFilter("status", "==", "pending"))
    return [(doc.id, doc.to_dict()) for doc in query.stream()]


def set_proposal_status(proposal_id: str, status: str) -> None:
    db().collection("proposals").document(proposal_id).update({"status": status})


@firestore.transactional
def _claim(transaction, ref, status: str) -> dict | None:
    snap = ref.get(transaction=transaction)
    if not snap.exists or snap.get("status") != "pending":
        return None
    transaction.update(ref, {"status": status, "decided_at": now()})
    return snap.to_dict()


def claim_proposal(proposal_id: str, status: str) -> dict | None:
    """Atomically move a pending proposal to `status`. Returns None if already decided,
    so a double tap or overlapping runs can't create the same event twice."""
    ref = db().collection("proposals").document(proposal_id)
    return _claim(db().transaction(), ref, status)
