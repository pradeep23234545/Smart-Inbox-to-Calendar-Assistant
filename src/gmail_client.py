"""
gmail_client.py
----------------
Smart Inbox-to-Calendar Assistant - thin wrapper around the Gmail API for:
  - OAuth2 authentication (runs a local browser consent flow once, then
    reuses a cached token.json)
  - Fetching recent inbox messages (sender, subject, body)
  - Creating custom Gmail labels for our categories/priority bands
  - Applying a label to a message (our "segmentation"/"filtering" action -
    Gmail doesn't have folders, it has labels, and removing the INBOX label
    is the equivalent of "moving it out of the inbox")

IMPORTANT - this file talks to a REAL Gmail account and must be run on YOUR
OWN machine, not inside a sandboxed cloud session, because:
  1. It needs a `credentials.json` OAuth client file that only you can
     generate from your own Google Cloud Console project (no one else
     has access to your Gmail account).
  2. The OAuth consent step opens a real browser window on your machine.

See ../README.md for the exact one-time setup steps.
"""

import base64
import re
from datetime import datetime

from googleapiclient.discovery import build

import google_auth

# Minimum Gmail scope needed: read messages + modify labels. We deliberately
# do NOT request send/delete scopes - this assistant only reads and
# re-labels mail. (Calendar access is a separate scope, see google_auth.py.)
SCOPES = [google_auth.GMAIL_SCOPE]

LABEL_PREFIX = "AI"  # all labels we create are namespaced under "AI/..."


def get_gmail_service(interactive: bool = True):
    """Authenticate (via cached token, or a fresh OAuth consent flow the
    first time) and return an authorized Gmail API service object."""
    creds = google_auth.get_credentials(SCOPES, interactive=interactive)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def gmail_link(msg_id: str) -> str:
    """Deep link that opens this exact message in the Gmail web UI."""
    return f"https://mail.google.com/mail/u/0/#all/{msg_id}"


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ASCII")).decode("utf-8", errors="ignore")


def _extract_body(payload) -> str:
    """Walk the (possibly multipart) MIME payload and return the best-effort
    plain-text body."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_part(payload["body"]["data"])

    if "parts" in payload:
        # Prefer text/plain; fall back to text/html stripped of tags.
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return _decode_part(part["body"]["data"])
        for part in payload["parts"]:
            text = _extract_body(part)
            if text:
                return text

    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = _decode_part(payload["body"]["data"])
        return re.sub("<[^<]+?>", " ", html)

    return ""


def list_recent_messages(service, max_results: int = 20, query: str = "in:inbox"):
    """Return a list of message IDs matching the query (default: inbox)."""
    resp = service.users().messages().list(userId="me", maxResults=max_results, q=query).execute()
    return [m["id"] for m in resp.get("messages", [])]


def get_message_content(service, msg_id: str):
    """Fetch one message and return {id, thread_id, sender, subject, body,
    date, link}. `date` is when Gmail received the message, as a naive
    local-time datetime - it is the reference point for resolving relative
    expressions such as "tomorrow" or "next Friday"."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    sender = headers.get("from", "")
    subject = headers.get("subject", "")
    body = _extract_body(msg["payload"])
    try:
        received = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000)
    except (TypeError, ValueError, OSError):
        received = datetime.now()
    return {
        "id": msg_id,
        "thread_id": msg.get("threadId", msg_id),
        "sender": sender,
        "subject": subject,
        "body": body,
        "date": received,
        "link": gmail_link(msg_id),
    }


_label_cache = {}


def ensure_label(service, label_name: str) -> str:
    """Get-or-create a Gmail label (namespaced e.g. 'AI/Finance_Bills') and
    return its label ID."""
    full_name = f"{LABEL_PREFIX}/{label_name}"
    if full_name in _label_cache:
        return _label_cache[full_name]

    resp = service.users().labels().list(userId="me").execute()
    for label in resp.get("labels", []):
        if label["name"] == full_name:
            _label_cache[full_name] = label["id"]
            return label["id"]

    created = service.users().labels().create(
        userId="me",
        body={"name": full_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    _label_cache[full_name] = created["id"]
    return created["id"]


def apply_label(service, msg_id: str, label_name: str, remove_from_inbox: bool = False, dry_run: bool = True):
    """Apply a (namespaced) label to a message, optionally archiving it out
    of the inbox. When dry_run=True (the default), NO changes are made to
    the mailbox - the intended action is only returned/printed. This keeps
    the tool safe to demo without risking real inbox changes until the user
    explicitly opts in, per the ethics discussion in Module 6."""
    label_id = ensure_label(service, label_name) if not dry_run else f"(would-create) AI/{label_name}"

    if dry_run:
        return {"applied": False, "label": f"AI/{label_name}", "removed_from_inbox": remove_from_inbox}

    body = {"addLabelIds": [label_id]}
    if remove_from_inbox:
        body["removeLabelIds"] = ["INBOX"]
    service.users().messages().modify(userId="me", id=msg_id, body=body).execute()
    return {"applied": True, "label": f"AI/{label_name}", "removed_from_inbox": remove_from_inbox}
