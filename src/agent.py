"""
agent.py
--------
Smart Inbox-to-Calendar Assistant - the autonomous background agent.

WHAT IT DOES
------------
Each time it runs, it:
  1. Signs in to Gmail (and Google Calendar) using the cached token.
  2. Finds inbox mail it hasn't handled yet (every handled message gets an
     "AI/Processed" label, so re-running never repeats work).
  3. Classifies each email (rule engine -> ML model -> explainability ->
     priority score) and applies category + priority labels.
  4. Detects time-bound commitments in the email - meetings, interviews,
     appointments, exams, payment due dates, submission deadlines, events -
     and creates a Google Calendar event with adaptive reminders, a short
     summary and a link back to the email. Duplicates, follow-ups,
     reschedules and cancellations update the existing event instead of
     adding new ones. Uncertain extractions go to a review queue as
     "[Review]" tentative events (see review.py).
  5. Logs everything it did (or would do) for you to audit.

SAFETY BY DESIGN
----------------
The agent starts in **dry-run mode**: it logs exactly what it *would* label
and which calendar events it *would* create, but changes nothing and does
not mark anything as processed. You opt in to real changes with `--enable`.

HOW TO RUN IT
-------------
    python agent.py --authorize          # one-time: grant Gmail + Calendar access
    python agent.py --once               # one pass (dry-run until --enable)
    python agent.py --once --enable      # turn on real changes from now on
    python agent.py --loop --interval-minutes 15
    python agent.py --scan-reminders --days 14
        # look back over the last 14 days of mail for commitments only
        # (also covers mail labelled before the calendar feature existed)
    python agent.py --no-calendar        # labels only, no reminders (saved)

Background scheduling: install_windows_task.bat / install_cron.sh.
"""

import argparse
import json
import logging
import os
import sys
import time

import paths
import gmail_client
from pipeline import EmailClassifierPipeline
from reminder_engine import DEFAULT_REMINDER_CONFIG, ReminderEngine
from reminder_store import ReminderStore

APP_NAME = "Smart Inbox-to-Calendar Assistant"

DEFAULT_CONFIG = {
    "auto_apply": False,        # False = dry run (log only, never touches Gmail/Calendar)
    "max_per_run": 25,          # how many new emails to process per run
    "archive_low_priority": False,  # also remove Promotions/Spam/Social from Inbox
    "interval_minutes": 15,     # only used by --loop
}
DEFAULT_CONFIG.update(DEFAULT_REMINDER_CONFIG)

PROCESSED_LABEL = "Processed"
REMINDER_SET_LABEL = "Reminder-Set"
REMINDER_REVIEW_LABEL = "Reminder-Review"
LOW_PRIORITY_CATEGORIES = {"Promotions", "Spam", "Social"}


# ----------------------------------------------------------------------
# Config persistence (~/.smart_inbox_calendar_assistant/agent_config.json)
# ----------------------------------------------------------------------
def load_config() -> dict:
    path = paths.agent_config_path()
    if not os.path.exists(path):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg: dict):
    with open(paths.agent_config_path(), "w") as f:
        json.dump(cfg, f, indent=2)


# ----------------------------------------------------------------------
# Logging (console + persistent log file, so a scheduled/background run
# still leaves a readable trail)
# ----------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("smart_inbox_calendar_assistant")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(paths.agent_log_path(), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
        if sys.stdout is not None:  # pythonw (background task) has no console
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(sh)
    return logger


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


# ----------------------------------------------------------------------
# Calendar reminders
# ----------------------------------------------------------------------
def build_reminder_engine(logger, cfg: dict):
    """Connect Google Calendar if allowed; never opens a browser from a
    background run. Returns a ReminderEngine (calendar may be None, in which
    case reminders are kept in the local review queue)."""
    if not cfg.get("calendar_reminders", True):
        return None
    calendar = None
    try:
        import calendar_client
        service = calendar_client.get_calendar_service(interactive=_interactive())
        calendar = calendar_client.CalendarClient(service, cfg.get("calendar_id", "primary"), cfg.get("timezone", ""))
        logger.info(f"Google Calendar connected (calendar: {cfg.get('calendar_id', 'primary')}).")
    except Exception as e:
        logger.info(f"Calendar not connected - reminders will wait in the review queue. ({e})")
    return ReminderEngine(ReminderStore(), calendar, cfg, logger=logger)


def handle_reminder(logger, engine, service, content: dict, result: dict, dry: bool):
    if engine is None:
        return None
    outcome = engine.process(content, result, dry_run=dry)
    action = outcome["action"]
    if action == "none":
        return outcome
    rec = outcome["record"] or {}
    when = (rec.get("start") or "")[:16].replace("T", " ")
    prefix = "would-" if dry else ""
    logger.info(f"      reminder [{prefix}{action}] {rec.get('title', '')} @ {when} "
                f"(conf {float(rec.get('confidence') or 0):.0%}) {outcome.get('reason', '')}".rstrip())
    if not dry and action in ("created", "created_review", "held_review", "rescheduled"):
        label = REMINDER_REVIEW_LABEL if rec.get("status") == "needs_review" else REMINDER_SET_LABEL
        try:
            gmail_client.apply_label(service, content["id"], label, dry_run=False)
        except Exception as e:
            logger.info(f"      (could not add {label} label: {e})")
    return outcome


def retry_pending(logger, engine, dry: bool):
    """Push reminders saved earlier but not yet on the calendar."""
    if engine is None or dry:
        return
    try:
        for rec in engine.retry_unsynced():
            logger.info(f"  reminder [synced] {rec['title']} @ {rec['start'][:16].replace('T', ' ')} "
                        f"(was waiting for Google Calendar)")
    except Exception as e:
        logger.info(f"  Google Calendar still unavailable - pending reminders will retry next run ({e})")


# ----------------------------------------------------------------------
# Core work
# ----------------------------------------------------------------------
def run_once(logger: logging.Logger, cfg: dict, pipeline: EmailClassifierPipeline, service, engine=None) -> int:
    """Process one batch of not-yet-handled inbox mail. Returns how many
    emails were found (processed or, in dry-run, just reported)."""
    retry_pending(logger, engine, not cfg["auto_apply"])
    query = f"in:inbox -label:AI/{PROCESSED_LABEL}"
    ids = gmail_client.list_recent_messages(service, max_results=cfg["max_per_run"], query=query)

    if not ids:
        logger.info("No new emails to process.")
        return 0

    dry = not cfg["auto_apply"]
    logger.info(f"Found {len(ids)} new email(s) to process. Mode: {'DRY-RUN' if dry else 'APPLYING'}")

    for msg_id in ids:
        try:
            content = gmail_client.get_message_content(service, msg_id)
            result = pipeline.classify_email(content["sender"], content["subject"], content["body"])

            remove_from_inbox = cfg["archive_low_priority"] and result["predicted_category"] in LOW_PRIORITY_CATEGORIES

            gmail_client.apply_label(service, msg_id, result["predicted_category"],
                                      remove_from_inbox=remove_from_inbox, dry_run=dry)
            gmail_client.apply_label(service, msg_id, f"Priority-{result['priority_band']}", dry_run=dry)

            mode = "APPLIED" if not dry else "would-apply"
            archived_note = " [archived]" if (not dry and remove_from_inbox) else ""
            logger.info(
                f"  [{mode}] \"{content['subject'][:60]}\" from {content['sender']} "
                f"-> {result['predicted_category']} / {result['priority_band']}{archived_note} "
                f"({result['decision_source']})"
            )
            try:
                handle_reminder(logger, engine, service, content, result, dry)
            except Exception as e:  # a reminder failure must never block labelling
                logger.info(f"      reminder ERROR for {msg_id}: {e}")

            if not dry:
                # Mark as handled so future runs never reprocess this email.
                gmail_client.apply_label(service, msg_id, PROCESSED_LABEL, dry_run=False)
        except Exception as e:  # keep the batch going even if one message fails
            logger.info(f"  ERROR processing message {msg_id}: {e}")

    return len(ids)


def scan_reminders(logger, cfg: dict, pipeline: EmailClassifierPipeline, service, engine, days: int) -> int:
    """Reminder-only pass over recent mail regardless of the Processed
    label - e.g. to cover mail labelled before the calendar feature existed.
    The ledger's duplicate detection guarantees nothing is created twice."""
    if engine is None:
        logger.info("Calendar reminders are turned off (--no-calendar); nothing to scan.")
        return 0
    query = f"in:inbox newer_than:{int(days)}d"
    ids = gmail_client.list_recent_messages(service, max_results=max(cfg["max_per_run"], 100), query=query)
    dry = not cfg["auto_apply"]
    retry_pending(logger, engine, dry)
    logger.info(f"Scanning {len(ids)} email(s) from the last {days} day(s) for commitments. "
                f"Mode: {'DRY-RUN' if dry else 'APPLYING'}")
    found = 0
    for msg_id in ids:
        if engine.store.was_seen(msg_id) or engine.store.get_by_message(msg_id):
            continue
        try:
            content = gmail_client.get_message_content(service, msg_id)
            result = pipeline.classify_email(content["sender"], content["subject"], content["body"])
            outcome = handle_reminder(logger, engine, service, content, result, dry)
            if outcome and outcome["action"] != "none":
                found += 1
                logger.info(f"        from email: \"{content['subject'][:60]}\"")
            elif not dry:
                engine.store.mark_seen(msg_id, "no-commitment")
        except Exception as e:
            logger.info(f"  ERROR scanning message {msg_id}: {e}")
    logger.info(f"Scan complete: {found} commitment(s) found.")
    return found


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - autonomous background agent")
    parser.add_argument("--once", action="store_true",
                         help="Process the current batch once and exit (used by Task Scheduler/cron)")
    parser.add_argument("--loop", action="store_true",
                         help="Run forever, checking every --interval-minutes (for a terminal you leave open)")
    parser.add_argument("--interval-minutes", type=int, default=None, help="Override the loop interval")
    parser.add_argument("--enable", action="store_true",
                         help="Turn ON auto-apply (real label/calendar changes) from now on, and save that setting")
    parser.add_argument("--disable", action="store_true",
                         help="Turn OFF auto-apply (back to safe dry-run/logging only), and save that setting")
    parser.add_argument("--max", type=int, default=None, help="Override how many emails to process per run")
    parser.add_argument("--archive-low-priority", action="store_true",
                         help="Also archive Promotions/Spam/Social out of the Inbox (saved setting)")
    parser.add_argument("--authorize", action="store_true",
                         help="Open the Google consent screen once to grant Gmail + Calendar access, then exit")
    parser.add_argument("--no-calendar", action="store_true",
                         help="Turn OFF calendar reminders (labels only), and save that setting")
    parser.add_argument("--calendar", action="store_true",
                         help="Turn calendar reminders back ON, and save that setting")
    parser.add_argument("--scan-reminders", action="store_true",
                         help="Reminder-only pass over recent mail (see --days), then exit")
    parser.add_argument("--days", type=int, default=14, help="Look-back window for --scan-reminders")
    args = parser.parse_args()

    if args.authorize:
        import google_auth
        granted = google_auth.authorize_all()
        print(f"{APP_NAME}: access granted for: " + ", ".join(s.rsplit("/", 1)[-1] for s in granted))
        return

    cfg = load_config()
    changed = False
    if args.interval_minutes is not None:
        cfg["interval_minutes"] = args.interval_minutes
        changed = True
    if args.max is not None:
        cfg["max_per_run"] = args.max
        changed = True
    if args.archive_low_priority:
        cfg["archive_low_priority"] = True
        changed = True
    if args.enable:
        cfg["auto_apply"] = True
        changed = True
    if args.disable:
        cfg["auto_apply"] = False
        changed = True
    if args.no_calendar:
        cfg["calendar_reminders"] = False
        changed = True
    if args.calendar:
        cfg["calendar_reminders"] = True
        changed = True
    if changed:
        save_config(cfg)

    logger = setup_logging()
    logger.info("=" * 78)
    logger.info(
        f"{APP_NAME} starting | auto_apply={cfg['auto_apply']} | "
        f"max_per_run={cfg['max_per_run']} | archive_low_priority={cfg['archive_low_priority']} | "
        f"calendar_reminders={cfg.get('calendar_reminders', True)}"
    )
    if not cfg["auto_apply"]:
        logger.info("Running in DRY-RUN mode - no mailbox or calendar changes will be made. "
                     "Run with --enable once you're happy with the predictions in this log.")

    logger.info("Authenticating with Google...")
    service = gmail_client.get_gmail_service(interactive=_interactive())
    pipeline = EmailClassifierPipeline()
    logger.info(f"Model loaded: {pipeline.model_name}")
    engine = build_reminder_engine(logger, cfg)

    if args.scan_reminders:
        scan_reminders(logger, cfg, pipeline, service, engine, args.days)
        logger.info("Run complete.")
        return

    if args.loop:
        interval = cfg["interval_minutes"]
        logger.info(f"Looping every {interval} minute(s). Press Ctrl+C to stop.")
        try:
            while True:
                run_once(logger, cfg, pipeline, service, engine)
                time.sleep(interval * 60)
        except KeyboardInterrupt:
            logger.info("Assistant stopped by user (Ctrl+C).")
    else:
        # Default behaviour, and what --once uses: a single pass then exit.
        run_once(logger, cfg, pipeline, service, engine)
        logger.info("Run complete.")


if __name__ == "__main__":
    main()
