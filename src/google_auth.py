"""
google_auth.py
--------------
Shared Google OAuth handling for the Smart Inbox-to-Calendar Assistant.

Two scopes are used, and nothing broader:

  * gmail.modify     - read mail and add/remove labels (never send/delete)
  * calendar.events  - create/update/delete events on the user's calendars
                       (cannot read or change calendar settings/sharing)

Scope upgrade without breaking background runs
-----------------------------------------------
Users who installed an earlier build granted only the Gmail scope. Asking
for the Calendar scope requires the Google consent screen in a browser,
which must never pop up from a silent scheduled task. So:

  * `get_credentials(..., interactive=False)` raises `AuthorizationRequired`
    instead of opening a browser when the cached token lacks a scope.
  * `python src/agent.py --authorize` runs the consent flow once, by hand,
    requesting both scopes together.
"""

import json
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import paths

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
ALL_SCOPES = [GMAIL_SCOPE, CALENDAR_SCOPE]


class AuthorizationRequired(RuntimeError):
    """The cached token is missing or lacks a required scope, and we are
    not allowed to open a browser consent window right now."""


def granted_scopes() -> list:
    """Scopes recorded in the cached token file (empty if none)."""
    path = paths.token_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return list(scopes)


def has_scopes(required) -> bool:
    return set(required).issubset(set(granted_scopes()))


def _run_consent_flow() -> Credentials:
    cred_path = paths.credentials_path()
    if not os.path.exists(cred_path):
        raise FileNotFoundError(
            "credentials.json not found. Download your OAuth client "
            "credentials from Google Cloud Console and place them at "
            f"{os.path.abspath(cred_path)}. See README.md."
        )
    flow = InstalledAppFlow.from_client_secrets_file(cred_path, ALL_SCOPES)
    return flow.run_local_server(port=0)


def get_credentials(required_scopes=None, interactive: bool = True) -> Credentials:
    """Return valid credentials covering `required_scopes`.

    interactive=False never opens a browser; it raises AuthorizationRequired
    instead, so scheduled/background runs fail soft."""
    required = list(required_scopes or ALL_SCOPES)
    token_path = paths.token_path()
    creds = None

    granted = granted_scopes()
    if granted and set(required).issubset(granted):
        creds = Credentials.from_authorized_user_file(token_path, granted)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            error_response = next((a for a in exc.args if isinstance(a, dict)), {})
            if error_response.get("error") != "invalid_grant" and "invalid_grant" not in str(exc):
                raise
            creds = None

    if not creds or not creds.valid:
        if not interactive:
            raise AuthorizationRequired(
                "Google access for "
                + ", ".join(s.rsplit("/", 1)[-1] for s in required)
                + " has not been granted yet. Run:  python src/agent.py --authorize"
            )
        creds = _run_consent_flow()

    with open(token_path, "w") as f:
        f.write(creds.to_json())
    return creds


def authorize_all() -> list:
    """Force the consent flow for every scope the assistant uses."""
    creds = _run_consent_flow()
    with open(paths.token_path(), "w") as f:
        f.write(creds.to_json())
    return granted_scopes()
