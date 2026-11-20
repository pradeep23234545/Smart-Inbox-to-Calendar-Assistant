"""
paths.py
--------
Centralised path handling for the Smart Inbox-to-Calendar Assistant.

All data the assistant creates or changes at runtime - trained models, the
training dataset, user corrections, the cached Google OAuth token, the
reminder database, its config and its logs - is kept in a per-user
application-data folder, never inside the project folder:

    Windows     : C:\\Users\\<you>\\.smart_inbox_calendar_assistant\\
    macOS/Linux : ~/.smart_inbox_calendar_assistant/

Earlier builds of this project stored their data in a differently named
folder (``.gmail_ai_classifier``). If that folder exists and the new one
does not, its contents are COPIED (never moved or deleted) into the new
folder on first run, so an existing credentials.json, trained models and
corrections carry over without any manual steps.
"""

import os
import shutil

APP_DIR_NAME = ".smart_inbox_calendar_assistant"
LEGACY_APP_DIR_NAMES = (".gmail_ai_classifier",)

_migrated = False


def _migrate_legacy_dir(new_dir: str):
    """Copy a legacy data folder into the new location exactly once."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    if os.path.exists(new_dir):
        return
    home = os.path.expanduser("~")
    for legacy in LEGACY_APP_DIR_NAMES:
        legacy_dir = os.path.join(home, legacy)
        if os.path.isdir(legacy_dir):
            try:
                shutil.copytree(legacy_dir, new_dir)
            except OSError:
                pass  # fall back to a fresh, empty folder below
            return


def get_user_data_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), APP_DIR_NAME)
    _migrate_legacy_dir(base)
    os.makedirs(base, exist_ok=True)
    os.makedirs(os.path.join(base, "data"), exist_ok=True)
    os.makedirs(os.path.join(base, "models"), exist_ok=True)
    return base


def data_dir() -> str:
    d = os.path.join(get_user_data_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def models_dir() -> str:
    d = os.path.join(get_user_data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def emails_csv() -> str:
    return os.path.join(data_dir(), "emails.csv")


def corrections_csv() -> str:
    return os.path.join(data_dir(), "corrections.csv")


def merged_csv() -> str:
    return os.path.join(data_dir(), "emails_with_feedback.csv")


def credentials_path() -> str:
    """Where the user should drop their Google OAuth credentials.json."""
    return os.path.join(get_user_data_dir(), "credentials.json")


def token_path() -> str:
    return os.path.join(get_user_data_dir(), "token.json")


def agent_config_path() -> str:
    return os.path.join(get_user_data_dir(), "agent_config.json")


def reminders_db_path() -> str:
    """SQLite database holding every reminder the assistant has generated,
    its review status, and the user's approve/edit/dismiss feedback."""
    return os.path.join(data_dir(), "reminders.db")


def logs_dir() -> str:
    d = os.path.join(get_user_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def agent_log_path() -> str:
    return os.path.join(logs_dir(), "assistant.log")


def best_model_name_path() -> str:
    return os.path.join(models_dir(), "best_model_name.joblib")


def vectorizer_path() -> str:
    return os.path.join(models_dir(), "vectorizer.joblib")


def model_path(model_name: str) -> str:
    fname = model_name.lower().replace(" ", "_") + ".joblib"
    return os.path.join(models_dir(), fname)
