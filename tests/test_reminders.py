"""
Offline tests for the Smart Inbox-to-Calendar Assistant reminder features.
No Google account is needed - the calendar is replaced by an in-memory fake.

Run from the project root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from event_extractor import extract_event, strip_noise                      # noqa: E402
from reminder_planner import plan_reminders                                  # noqa: E402
from reminder_store import ReminderStore                                     # noqa: E402
from reminder_engine import ReminderEngine                                   # noqa: E402
from email_summarizer import summarize                                       # noqa: E402
from calendar_client import build_event_body                                 # noqa: E402

REF = datetime(2026, 11, 20, 9, 15)   # a Friday
PRIMARY_HIGH = {"predicted_category": "Primary", "priority_band": "High"}


class FakeCalendar:
    def __init__(self):
        self.events, self._n = {}, 0

    def create(self, rec):
        self._n += 1
        eid = f"evt{self._n}"
        self.events[eid] = build_event_body(rec)
        return eid

    def update(self, eid, rec):
        self.events[eid] = build_event_body(rec)
        return eid

    def delete(self, eid):
        self.events.pop(eid, None)

    def find_by_message(self, msg_id):
        for eid, body in self.events.items():
            if body["extendedProperties"]["private"]["sica_msg_id"] == msg_id:
                return eid
        return None


def ev(subject, body, ref=REF, **kw):
    return extract_event(subject, body, ref, **kw)


class ExtractionTests(unittest.TestCase):
    def test_interview_tomorrow_with_time(self):
        r = ev("Interview Invitation", "You are shortlisted. Please join the interview tomorrow at 10 AM.")
        self.assertTrue(r["is_event"])
        self.assertEqual(r["event_type"], "interview")
        self.assertEqual(r["start"], "2026-11-21T10:00")
        self.assertFalse(r["all_day"])

    def test_payment_due_ignores_statement_date(self):
        r = ev("Credit card statement", "Your statement dated 15 Nov 2026 is ready. Amount due Rs. 4,599. "
                                        "Payment due date: 05 Dec 2026.")
        self.assertEqual(r["event_type"], "payment_due")
        self.assertEqual(r["start"][:10], "2026-12-05")
        self.assertTrue(r["all_day"])
        self.assertIn("Rs.4,599", r["title"])

    def test_deadline_numeric_date_and_time(self):
        r = ev("Assignment 3", "Please submit assignment 3 no later than 28/11/2026 11:59 PM.")
        self.assertEqual(r["event_type"], "submission_deadline")
        self.assertEqual(r["start"], "2026-11-28T23:59")

    def test_ambiguous_day_month_is_flagged(self):
        r = ev("Exam schedule", "The end-semester examination will be held on 04/12 from 10:00 to 13:00.")
        codes = {i["code"] for i in r["issues"]}
        self.assertIn("date_order_ambiguous", codes)
        self.assertEqual(r["start"], "2026-12-04T10:00")
        self.assertEqual(r["end"], "2026-12-04T13:00")

    def test_mdy_preference(self):
        r = ev("Exam schedule", "The examination will be held on 04/12 at 10 AM.", date_order="MDY")
        self.assertEqual(r["start"][:10], "2027-04-12")

    def test_past_reading_resolved_to_future(self):
        # 13/11 -> 13 Nov (past) vs invalid month 13 -> only DMY valid; 11/02 -> 11 Feb or 2 Nov(past)
        r = ev("Fee payment", "Fee payment due on 11/02 to avoid penalty.")
        self.assertEqual(r["start"][:10], "2027-02-11")

    def test_weekday_conflict_flagged(self):
        r = ev("Meeting", "Team meeting on Friday, 24 Nov 2026 at 3 PM.")
        self.assertIn("weekday_conflict", {i["code"] for i in r["issues"]})

    def test_timezone_conversion(self):
        import zoneinfo
        r = ev("Webinar", "Join the webinar on Dec 3rd at 7 PM PST.", local_tz=zoneinfo.ZoneInfo("Asia/Kolkata"))
        self.assertEqual(r["start"], "2026-12-04T08:30")

    def test_missing_time_for_meeting_is_major(self):
        r = ev("Project meeting", "Let's have the project meeting on 2 Dec 2026.")
        self.assertTrue(r["all_day"])
        self.assertIn("time_missing", {i["code"] for i in r["issues"]})

    def test_no_commitment(self):
        self.assertFalse(ev("Dinner?", "Are you free for dinner this Saturday?")["is_event"])
        self.assertFalse(ev("Order shipped", "Your order placed on 12 Nov 2026 has been shipped.")["is_event"])
        self.assertFalse(ev("Flat 60% OFF", "Hurry, sale ends tonight! Use code SAVE60.")["is_event"])

    def test_quoted_reply_is_ignored(self):
        body = "Thanks!\n\nOn Mon, 16 Nov 2026 at 10:00, Ravi wrote:\n> meeting on 18 Nov at 4pm"
        self.assertNotIn("18 Nov", strip_noise(body))

    def test_relative_working_days(self):
        r = ev("Fee payment", "Semester fee must be paid within 5 working days.")
        self.assertEqual(r["start"][:10], "2026-11-27")

    def test_next_weekday_ambiguity(self):
        r = ev("Review meeting", "Let's have the review meeting next Friday at 4 pm.")
        self.assertEqual(r["start"], "2026-11-27T16:00")
        r2 = ev("Review meeting", "Let's have the review meeting next Wednesday at 4 pm.",
                ref=datetime(2026, 11, 23, 9, 0))  # Monday
        self.assertIn("next_weekday_ambiguous", {i["code"] for i in r2["issues"]})

    def test_cancel_and_reschedule_flags(self):
        self.assertTrue(ev("Interview cancelled", "The interview on 25 Nov 2026 at 2 PM is cancelled.")["cancelled"])
        self.assertTrue(ev("Interview update", "Your interview has been rescheduled to 26 Nov 2026 at 3 PM.")["reschedule"])


class PlannerTests(unittest.TestCase):
    def test_interview_profile(self):
        start = datetime(2026, 11, 25, 14, 0)
        p = plan_reminders("interview", start, False, "High", now=REF)
        minutes = [o["minutes"] for o in p["overrides"] if o["method"] == "popup"]
        self.assertIn(1440, minutes)
        self.assertIn(120, minutes)
        self.assertTrue(all(start - timedelta(minutes=m) >= REF for m in minutes))

    def test_quiet_hours_shift(self):
        start = datetime(2026, 11, 25, 6, 0)          # 6 AM interview -> 2h-before reminder would be 4 AM
        p = plan_reminders("interview", start, False, "Medium", now=REF)
        for item in p["plan"]:
            fire = datetime.fromisoformat(item["fires_at"])
            self.assertFalse(fire.hour >= 22 or fire.hour < 7, item)

    def test_all_day_reminders_and_cap(self):
        start = datetime(2026, 12, 5)
        p = plan_reminders("payment_due", start, True, "Critical", now=REF)
        self.assertTrue(all(0 < o["minutes"] <= 40320 for o in p["overrides"]))
        self.assertIn("email", {o["method"] for o in p["overrides"]})

    def test_imminent_event_gets_single_reminder(self):
        start = REF + timedelta(minutes=20)
        p = plan_reminders("meeting", start, False, "Medium", now=REF)
        self.assertEqual(len(p["plan"]), 1)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.cal = FakeCalendar()
        self.engine = ReminderEngine(ReminderStore(":memory:"), self.cal, {}, now_fn=lambda: REF)

    def tearDown(self):
        self.engine.store.close()

    def email(self, mid, subject, body, thread=None, sender="hr@company.com", date=REF):
        return {"id": mid, "thread_id": thread or mid, "sender": sender, "subject": subject, "body": body,
                "date": date, "link": f"https://mail.google.com/mail/u/0/#all/{mid}"}

    def test_create_with_link_summary_and_reminders(self):
        out = self.engine.process(self.email("m1", "Interview Invitation",
                                             "Dear candidate, you are shortlisted. Please join the interview on "
                                             "25 Nov 2026 at 2:00 PM. Venue: Block A, Room 101."),
                                  PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "created")
        body = list(self.cal.events.values())[0]
        self.assertIn("#all/m1", body["description"])
        self.assertEqual(body["source"]["url"], "https://mail.google.com/mail/u/0/#all/m1")
        self.assertFalse(body["reminders"]["useDefault"])
        self.assertEqual(body["status"], "confirmed")
        self.assertEqual(body["location"], "Block A, Room 101")

    def test_same_message_and_duplicate_mail(self):
        e1 = self.email("m1", "Interview Invitation", "Please join the interview on 25 Nov 2026 at 2:00 PM.")
        self.engine.process(e1, PRIMARY_HIGH, dry_run=False)
        self.assertEqual(self.engine.process(e1, PRIMARY_HIGH, dry_run=False)["action"], "duplicate")
        e2 = self.email("m2", "Gentle reminder: Interview Invitation",
                        "Reminder - your interview is on 25 Nov 2026 at 2:00 PM.")
        self.assertEqual(self.engine.process(e2, PRIMARY_HIGH, dry_run=False)["action"], "duplicate")
        self.assertEqual(len(self.cal.events), 1)

    def test_reschedule_updates_existing_event(self):
        self.engine.process(self.email("m1", "Interview Invitation",
                                       "Please join the interview on 25 Nov 2026 at 2:00 PM.", thread="t1"),
                            PRIMARY_HIGH, dry_run=False)
        out = self.engine.process(self.email("m2", "Re: Interview Invitation",
                                             "Your interview has been rescheduled to 26 Nov 2026 at 3:00 PM.",
                                             thread="t1"), PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "rescheduled")
        self.assertEqual(len(self.cal.events), 1)
        self.assertTrue(list(self.cal.events.values())[0]["start"]["dateTime"].startswith("2026-11-26T15:00"))

    def test_cancellation(self):
        self.engine.process(self.email("m1", "Interview Invitation",
                                       "Please join the interview on 25 Nov 2026 at 2:00 PM.", thread="t1"),
                            PRIMARY_HIGH, dry_run=False)
        out = self.engine.process(self.email("m2", "Re: Interview Invitation",
                                             "Sorry, the interview on 25 Nov 2026 at 2:00 PM is cancelled.",
                                             thread="t1"), PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "cancelled")
        self.assertTrue(list(self.cal.events.values())[0]["summary"].startswith("[Cancelled]"))

    def test_ambiguous_goes_to_review_then_approve(self):
        out = self.engine.process(self.email("m1", "Exam schedule",
                                             "The examination will be held on 04/12 from 10:00 to 13:00."),
                                  PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "created_review")
        body = list(self.cal.events.values())[0]
        self.assertTrue(body["summary"].startswith("[Review]"))
        self.assertEqual(body["status"], "tentative")
        self.engine.approve(out["record"]["id"])
        body = list(self.cal.events.values())[0]
        self.assertFalse(body["summary"].startswith("[Review]"))
        self.assertEqual(body["status"], "confirmed")

    def test_edit_and_dismiss_prevents_recreation(self):
        out = self.engine.process(self.email("m1", "Interview Invitation",
                                             "Please join the interview on 25 Nov 2026 at 2:00 PM."),
                                  PRIMARY_HIGH, dry_run=False)
        rid = out["record"]["id"]
        rec = self.engine.edit(rid, start=datetime(2026, 11, 25, 16, 0), reminder_minutes=[60])
        self.assertEqual(rec["start"], "2026-11-25T16:00")
        self.assertEqual(rec["reminders"], [{"method": "popup", "minutes": 60}])
        self.engine.dismiss(rid)
        self.assertEqual(self.cal.events, {})
        again = self.engine.process(self.email("m9", "Interview Invitation",
                                               "Please join the interview on 25 Nov 2026 at 2:00 PM."),
                                    PRIMARY_HIGH, dry_run=False)
        self.assertEqual(again["action"], "skipped_dismissed")
        self.assertEqual(self.cal.events, {})

    def test_repeated_dismissals_force_review(self):
        for i, day in enumerate((24, 25, 26)):
            out = self.engine.process(self.email(f"d{i}", f"Webinar {i} on growth",
                                                 f"Join our webinar number {i} on {day} Nov 2026 at 6 PM.",
                                                 sender="events@spammy.io"), PRIMARY_HIGH, dry_run=False)
            if i < 2:
                self.engine.dismiss(out["record"]["id"])
        self.assertEqual(out["record"]["status"], "needs_review")

    def test_skip_categories_and_dry_run(self):
        out = self.engine.process(self.email("p1", "Offer", "Payment due on 5 Dec 2026"),
                                  {"predicted_category": "Promotions", "priority_band": "Low"}, dry_run=False)
        self.assertEqual(out["action"], "none")
        out = self.engine.process(self.email("m1", "Interview Invitation",
                                             "Please join the interview on 25 Nov 2026 at 2:00 PM."),
                                  PRIMARY_HIGH, dry_run=True)
        self.assertEqual(out["action"], "created")
        self.assertEqual(self.cal.events, {})
        self.assertEqual(self.engine.store.list(), [])

    def test_past_event_skipped(self):
        out = self.engine.process(self.email("m1", "Meeting", "The meeting is on 10 Nov 2026 at 3 PM.",
                                             date=datetime(2026, 11, 1)), PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "none")

    def test_calendar_error_is_saved_and_retried(self):
        class Broken(FakeCalendar):
            def find_by_message(self, msg_id):
                raise RuntimeError("403 Calendar API disabled")
        broken = Broken()
        self.engine.calendar = broken
        out = self.engine.process(self.email("m1", "Interview Invitation",
                                             "Please join the interview on 25 Nov 2026 at 2:00 PM."),
                                  PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "pending_sync")
        self.assertIsNone(out["record"]["calendar_event_id"])
        self.engine.calendar = self.cal            # API enabled again
        self.engine.store.list = lambda statuses=None, upcoming_only=False, limit=200, _f=self.engine.store.list: \
            _f(statuses, False, limit)              # REF is in the past relative to the real clock
        synced = self.engine.retry_unsynced()
        self.assertEqual(len(synced), 1)
        self.assertEqual(len(self.cal.events), 1)
        self.assertEqual(self.engine.retry_unsynced(), [])

    def test_no_calendar_holds_in_queue(self):
        engine = ReminderEngine(ReminderStore(":memory:"), None, {}, now_fn=lambda: REF)
        out = engine.process(self.email("m1", "Interview Invitation",
                                        "Please join the interview on 25 Nov 2026 at 2:00 PM."),
                             PRIMARY_HIGH, dry_run=False)
        self.assertEqual(out["action"], "held_review")
        self.assertEqual(out["record"]["status"], "needs_review")
        engine.store.close()


class SummaryTests(unittest.TestCase):
    def test_summary_prefers_commitment_sentence(self):
        text = ("Dear Student, Hope you are doing well. The library will be closed on Sunday. "
                "Please submit your project report by 28 Nov 2026 on the portal. Regards, Office")
        s = summarize(text, "28 Nov 2026", max_sentences=1)
        self.assertIn("submit your project report", s)


if __name__ == "__main__":
    unittest.main()
