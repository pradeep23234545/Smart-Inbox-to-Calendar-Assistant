"""
review.py
---------
Smart Inbox-to-Calendar Assistant - review, edit or dismiss the reminders
the assistant created from your email.

    python src/review.py list                 # upcoming reminders (default)
    python src/review.py list --pending       # only the ones waiting for review
    python src/review.py list --all           # everything, incl. dismissed/past
    python src/review.py show 7               # full details + why it was created
    python src/review.py approve 7            # confirm it (removes "[Review]")
    python src/review.py approve --all-pending
    python src/review.py edit 7 --date 2026-12-05 --time 14:30
    python src/review.py edit 7 --title "Pay HDFC card" --all-day
    python src/review.py edit 7 --remind 2880,60      # minutes before
    python src/review.py dismiss 7            # delete from calendar, never re-create
    python src/review.py restore 7            # undo a dismissal
    python src/review.py try "Subject" "Body text..."  # test extraction offline

Approve / edit / dismiss decisions are remembered per sender and type, so
the assistant becomes more (or less) autonomous for each sender over time.
"""

import argparse
import sys
from datetime import datetime

import reminder_engine
from reminder_engine import ReminderEngine, describe
from reminder_store import ReminderStore, STATUS_REVIEW, STATUS_SCHEDULED


def _load_config():
    try:
        import agent
        return agent.load_config()
    except Exception:
        return {}


def _engine(offline: bool) -> ReminderEngine:
    cfg = _load_config()
    calendar = None
    if not offline:
        try:
            import calendar_client
            service = calendar_client.get_calendar_service(interactive=sys.stdin is not None and sys.stdin.isatty())
            calendar = calendar_client.CalendarClient(service, cfg.get("calendar_id", "primary"),
                                                      cfg.get("timezone", ""))
        except Exception as e:
            print(f"(Calendar not connected: {e})\n(Changes will be saved locally only.)\n")
    return ReminderEngine(ReminderStore(), calendar, cfg)


def cmd_list(args):
    store = ReminderStore()
    if args.all:
        rows = store.list()
    elif args.pending:
        rows = store.list([STATUS_REVIEW])
    else:
        rows = store.list([STATUS_REVIEW, STATUS_SCHEDULED], upcoming_only=True)
    if not rows:
        print("No reminders to show.")
        return
    print(f"{'ID':>4}  {'STATUS':<13} {'WHEN':<17} {'TYPE':<20} {'CONF':>5}  TITLE")
    for r in rows:
        when = r["start"][:10] + (" all-day" if r["all_day"] else " " + r["start"][11:16])
        print(f"{r['id']:>4}  {r['status']:<13} {when:<17} {r['event_type']:<20} "
              f"{float(r['confidence'] or 0):>4.0%}  {r['title'][:60]}")


def cmd_show(args):
    rec = ReminderStore().get(args.id)
    if not rec:
        sys.exit(f"No reminder #{args.id}")
    print(describe(rec))
    print("\n    Why it was created:")
    for line in rec.get("explanation") or []:
        print(f"      - {line}")
    if rec.get("reminder_rationale"):
        print(f"\n    Reminder plan: {rec['reminder_rationale']}")
    print("\n    History:")
    for h in rec.get("history") or []:
        print(f"      {h['at']}  {h['action']}")


def cmd_approve(args):
    eng = _engine(args.offline)
    ids = [r["id"] for r in eng.store.list([STATUS_REVIEW])] if args.all_pending else [args.id]
    if not ids or ids == [None]:
        print("Nothing to approve.")
        return
    for rid in ids:
        rec = eng.approve(rid)
        print(f"Approved #{rid}: {rec['title']}")


def cmd_edit(args):
    eng = _engine(args.offline)
    rec = eng.store.get(args.id)
    if not rec:
        sys.exit(f"No reminder #{args.id}")
    start = None
    if args.date or args.time:
        cur = datetime.fromisoformat(rec["start"])
        d = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else cur.date()
        if args.time:
            t = datetime.strptime(args.time, "%H:%M").time()
        else:
            t = cur.time()
        start = datetime.combine(d, t)
    all_day = True if args.all_day else (False if args.time else None)
    end = None
    if args.duration and start is not None:
        from datetime import timedelta
        end = start + timedelta(minutes=args.duration)
    remind = [int(x) for x in args.remind.split(",")] if args.remind else None
    rec = eng.edit(args.id, title=args.title, start=start, end=end, all_day=all_day,
                   reminder_minutes=remind, location=args.location)
    print("Updated:\n" + describe(rec))


def cmd_dismiss(args):
    rec = _engine(args.offline).dismiss(args.id)
    print(f"Dismissed #{args.id}: {rec['title']} (removed from calendar; it will not be re-created)")


def cmd_restore(args):
    rec = _engine(args.offline).restore(args.id)
    print(f"Restored #{args.id}: {rec['title']}")


def cmd_try(args):
    """Offline what-if: run extraction + planning on text you type."""
    eng = ReminderEngine(ReminderStore(":memory:"), None, _load_config())
    email = {"id": None, "sender": args.sender, "subject": args.subject, "body": args.body, "date": datetime.now()}
    cand = eng.analyse(email, {"predicted_category": "Primary", "priority_band": args.priority})
    if not cand.get("is_event"):
        print(f"No reminder would be created: {cand.get('reason')}")
        return
    print(describe(cand))
    print("\n    Why:")
    for line in cand["explanation"]:
        print(f"      - {line}")
    print(f"\n    Reminder plan: {cand['reminder_rationale']}")


def main():
    p = argparse.ArgumentParser(description="Smart Inbox-to-Calendar Assistant - review reminders")
    p.add_argument("--offline", action="store_true", help="Change only the local ledger, not Google Calendar")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("list")
    s.add_argument("--pending", action="store_true")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("approve")
    s.add_argument("id", type=int, nargs="?")
    s.add_argument("--all-pending", action="store_true")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("edit")
    s.add_argument("id", type=int)
    s.add_argument("--title")
    s.add_argument("--date", help="YYYY-MM-DD")
    s.add_argument("--time", help="HH:MM (24h)")
    s.add_argument("--duration", type=int, help="minutes")
    s.add_argument("--all-day", action="store_true")
    s.add_argument("--remind", help="comma-separated minutes before, e.g. 1440,60")
    s.add_argument("--location")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("dismiss")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_dismiss)

    s = sub.add_parser("restore")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("try")
    s.add_argument("subject")
    s.add_argument("body")
    s.add_argument("--sender", default="someone@example.com")
    s.add_argument("--priority", default="High", choices=["Low", "Medium", "High", "Critical"])
    s.set_defaults(fn=cmd_try)

    args = p.parse_args()
    if not getattr(args, "fn", None):
        args = p.parse_args(["list"])
    if args.cmd == "approve" and not args.all_pending and args.id is None:
        p.error("approve needs an id or --all-pending")
    args.fn(args)


if __name__ == "__main__":
    main()
