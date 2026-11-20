"""
reminder_engine.py
------------------
Smart Inbox-to-Calendar Assistant - the decision engine that turns an email
into a calendar reminder.

For every email, after the existing classifier has assigned a category and
priority (pipeline.py), the engine:

  1. GATES     - skips Spam/Promotions entirely; Social only for strong
                 meeting/event evidence.
  2. EXTRACTS  - event_extractor.extract_event(): type, date/time, title,
                 location, amount, confidence, ambiguities.
  3. CHECKS    - drops commitments already in the past.
  4. PLANS     - reminder_planner.plan_reminders(): adaptive lead times.
  5. SUMMARISES- email_summarizer.summarize(): 1-2 sentence gist.
  6. DEDUPES   - reminder_store.find_match(): same message / same thread /
                 fingerprint / fuzzy; detects reschedules and cancellations
                 and UPDATES the existing event instead of adding another.
  7. DECIDES   - confidence-gated autonomy:
                   confidence >= auto_threshold, no major/blocking issue,
                   sender not distrusted                 -> "scheduled"
                   otherwise                             -> "needs_review"
                 needs_review items are still placed on the calendar as
                 TENTATIVE "[Review]" events (policy "tentative", default) so
                 nothing is missed, or held only in the review queue
                 (policy "hold").
  8. ACTS      - creates / updates the Google Calendar event (unless dry-run)
                 and writes the ledger entry with its full explanation.

Review operations (approve / edit / dismiss / restore) used by review.py and
the GUI also live here, so the calendar and the ledger always stay in step
and every decision feeds the per-sender feedback memory.
"""

from datetime import datetime, timedelta

import event_extractor
import reminder_planner
from email_summarizer import summarize
from reminder_store import (ReminderStore, STATUS_SCHEDULED, STATUS_REVIEW, STATUS_DISMISSED,
                            STATUS_CANCELLED, ACTIVE_STATUSES, make_fingerprint, sender_domain)

DEFAULT_REMINDER_CONFIG = {
    "calendar_reminders": True,       # master switch for the calendar feature
    "calendar_id": "primary",
    "timezone": "",                   # IANA name e.g. "Asia/Kolkata"; empty = this computer's local time
    "date_order": "DMY",              # how to read 04/05/2026: DMY (4 May) or MDY (April 5)
    "auto_create_threshold": 0.70,    # confidence needed to schedule without review
    "min_event_confidence": 0.35,     # below this, nothing is created at all
    "review_policy": "tentative",     # "tentative" = put [Review] events on calendar; "hold" = queue only
    "skip_categories": ["Spam", "Promotions"],
    "email_notification_bands": ["Critical"],
}


class ReminderEngine:
    def __init__(self, store: ReminderStore = None, calendar=None, config: dict = None, now_fn=None, logger=None):
        self.store = store or ReminderStore()
        self.calendar = calendar           # CalendarClient or None (offline / not authorised)
        self.cfg = dict(DEFAULT_REMINDER_CONFIG)
        self.cfg.update(config or {})
        self.now_fn = now_fn or datetime.now
        self.log = logger
        self.local_tz = None
        if self.cfg.get("timezone"):
            try:
                from zoneinfo import ZoneInfo
                self.local_tz = ZoneInfo(self.cfg["timezone"])
            except Exception:
                self.local_tz = None

    # ------------------------------------------------------------------
    def analyse(self, email: dict, classification: dict = None) -> dict:
        """Pure analysis, no side effects. Returns the candidate record (or a
        dict with is_event False and a reason)."""
        classification = classification or {}
        category = classification.get("predicted_category", "Primary")
        band = classification.get("priority_band", "Medium")

        if category in self.cfg["skip_categories"]:
            return {"is_event": False, "reason": f"category '{category}' is excluded from reminders"}

        ev = event_extractor.extract_event(email.get("subject", ""), email.get("body", ""),
                                           received=email.get("date"), date_order=self.cfg["date_order"],
                                           local_tz=self.local_tz, sender=email.get("sender", ""))
        if not ev["is_event"]:
            return ev
        if category == "Social" and (ev["event_type"] not in ("meeting", "event") or ev["type_confidence"] < 0.6):
            return {"is_event": False, "reason": "social notification without a strong meeting/event signal"}

        now = self.now_fn()
        end = datetime.fromisoformat(ev["end"])
        if end < now and not ev["cancelled"]:
            return {"is_event": False, "reason": f"commitment already over ({ev['start']})", "event": ev}

        domain = sender_domain(email.get("sender", ""))
        trust = self.store.sender_trust(domain, ev["event_type"])
        confidence = max(0.0, min(1.0, ev["confidence"] + trust["adjustment"]))
        explanation = list(ev["explanation"])
        if trust["adjustment"]:
            explanation.append(f"sender history for {domain} ({trust['approvals']} approved, "
                               f"{trust['dismissals']} dismissed): {trust['adjustment']:+.2f} confidence")
        if confidence < self.cfg["min_event_confidence"]:
            return {"is_event": False, "reason": f"confidence {confidence:.2f} below minimum", "event": ev}

        start = datetime.fromisoformat(ev["start"])
        plan = reminder_planner.plan_reminders(ev["event_type"], start, ev["all_day"], band, now=now,
                                               email_for_bands=tuple(self.cfg["email_notification_bands"]))
        summary = summarize(ev["clean_body"], ev["date_text"]) or ev["subject"]

        needs_review = (confidence < self.cfg["auto_create_threshold"]
                        or bool(event_extractor.review_issues(ev))
                        or trust["force_review"])
        review_reasons = [i["message"] for i in event_extractor.review_issues(ev)]
        if confidence < self.cfg["auto_create_threshold"]:
            review_reasons.append(f"confidence {confidence:.0%} is below the auto-create threshold "
                                  f"{self.cfg['auto_create_threshold']:.0%}")
        if trust["force_review"]:
            review_reasons.append(f"you dismissed {trust['dismissals']} earlier reminders like this from {domain}")

        return {
            "is_event": True,
            "gmail_msg_id": email.get("id"),
            "thread_id": email.get("thread_id"),
            "event_type": ev["event_type"],
            "title": ev["title"],
            "start": ev["start"],
            "end": ev["end"],
            "all_day": 1 if ev["all_day"] else 0,
            "location": ev["location"],
            "reminders": plan["overrides"],
            "reminder_rationale": plan["rationale"],
            "summary": summary,
            "gmail_link": email.get("link") or (f"https://mail.google.com/mail/u/0/#all/{email['id']}"
                                                 if email.get("id") else ""),
            "sender": email.get("sender", ""),
            "sender_domain": domain,
            "subject": email.get("subject", ""),
            "priority_band": band,
            "category": category,
            "confidence": round(confidence, 3),
            "status": STATUS_REVIEW if needs_review else STATUS_SCHEDULED,
            "issues": ev["issues"],
            "explanation": explanation + ([f"sent to review: {'; '.join(review_reasons)}"] if needs_review else []),
            "fingerprint": make_fingerprint(ev["event_type"], ev["start"], ev["title"]),
            "reschedule": ev["reschedule"],
            "cancelled": ev["cancelled"],
            "alternates": ev["alternates"],
            "plan": plan["plan"],
        }

    # ------------------------------------------------------------------
    def process(self, email: dict, classification: dict = None, dry_run: bool = True) -> dict:
        """Analyse + dedupe + act. Returns {"action", "record", "reason"}.
        action in: none, created, created_review, held_review, updated,
        rescheduled, cancelled, duplicate, skipped_dismissed."""
        cand = self.analyse(email, classification)
        if not cand.get("is_event"):
            return {"action": "none", "record": None, "reason": cand.get("reason", "")}

        kind, existing = self.store.find_match(cand)
        if kind == "same_message":
            return {"action": "duplicate", "record": existing, "reason": "this email was already handled"}

        if existing and existing["status"] == STATUS_DISMISSED:
            if not dry_run:
                self.store.mark_seen(cand["gmail_msg_id"] or "", "duplicate-of-dismissed")
            return {"action": "skipped_dismissed", "record": existing,
                    "reason": f"you dismissed a matching reminder (#{existing['id']}) earlier"}

        if existing and cand["cancelled"]:
            return self._apply_cancel(existing, cand, dry_run)

        if kind == "reschedule":
            return self._apply_reschedule(existing, cand, dry_run)

        if kind == "duplicate":
            if not dry_run and cand["gmail_msg_id"]:
                self.store.mark_seen(cand["gmail_msg_id"], f"duplicate-of-{existing['id']}")
            return {"action": "duplicate", "record": existing,
                    "reason": f"same commitment as reminder #{existing['id']} ('{existing['title']}')"}

        if cand["cancelled"]:
            return {"action": "none", "record": None, "reason": "cancellation notice for an unknown event"}

        return self._apply_create(cand, dry_run)

    # ------------------------------------------------------------------
    def _record_fields(self, cand: dict) -> dict:
        keep = ("gmail_msg_id", "thread_id", "fingerprint", "event_type", "title", "start", "end", "all_day",
                "location", "reminders", "reminder_rationale", "summary", "gmail_link", "sender", "sender_domain",
                "subject", "priority_band", "category", "confidence", "status", "issues", "explanation")
        return {k: cand.get(k) for k in keep}

    def _calendar_ok(self):
        return self.calendar is not None

    def _apply_create(self, cand, dry_run):
        hold = cand["status"] == STATUS_REVIEW and self.cfg["review_policy"] == "hold"
        action = "held_review" if hold else ("created_review" if cand["status"] == STATUS_REVIEW else "created")
        if dry_run:
            return {"action": action, "record": cand, "reason": "dry-run: nothing written", "dry_run": True}

        rec = self._record_fields(cand)
        if not hold and not self._calendar_ok():
            # No calendar connection: keep it safe in the review queue; it is
            # pushed to Google Calendar when the user approves it.
            rec["status"] = STATUS_REVIEW
            rec["explanation"] = (rec.get("explanation") or []) + ["calendar not connected - queued for review"]
            rid = self.store.insert(rec)
            self.store.mark_seen(cand["gmail_msg_id"] or "", "held_review")
            return {"action": "held_review", "record": self.store.get(rid),
                    "reason": "calendar not connected - kept in the review queue"}
        rid = self.store.insert(rec)
        rec["id"] = rid
        self.store.mark_seen(cand["gmail_msg_id"] or "", action)
        if hold:
            return {"action": action, "record": self.store.get(rid), "reason": ""}
        try:
            existing_event = (self.calendar.find_by_message(cand["gmail_msg_id"])
                              if cand.get("gmail_msg_id") else None)
            if existing_event:
                self.store.update(rid, "calendar_linked", calendar_event_id=existing_event)
                return {"action": "duplicate", "record": self.store.get(rid),
                        "reason": "calendar already had an event for this email"}
            event_id = self.calendar.create(rec)
            self.store.update(rid, "calendar_created", calendar_event_id=event_id)
        except Exception as e:
            # Never lose a detected commitment because Google was unreachable or
            # misconfigured: it stays in the ledger and retry_unsynced() pushes
            # it to the calendar on the next run.
            self.store.update(rid, "calendar_error", explanation=(rec.get("explanation") or []) +
                              [f"Google Calendar error, will retry on the next run: {str(e)[:200]}"])
            return {"action": "pending_sync", "record": self.store.get(rid),
                    "reason": "Google Calendar error - saved, will retry on the next run"}
        return {"action": action, "record": self.store.get(rid), "reason": ""}

    def retry_unsynced(self) -> list:
        """Push upcoming reminders that are in the ledger but not yet on the
        calendar (earlier Calendar API error, or created while the calendar
        was not connected). Returns the records that were synced."""
        if not self._calendar_ok():
            return []
        synced = []
        for rec in self.store.list(list(ACTIVE_STATUSES), upcoming_only=True):
            if rec.get("calendar_event_id"):
                continue
            if rec["status"] == STATUS_REVIEW and self.cfg["review_policy"] == "hold":
                continue
            event_id = self.calendar.create(rec)
            self.store.update(rec["id"], "calendar_created", calendar_event_id=event_id)
            synced.append(self.store.get(rec["id"]))
        return synced

    def _apply_reschedule(self, existing, cand, dry_run):
        reason = (f"rescheduled: {existing['start']} -> {cand['start']} (reminder #{existing['id']})")
        if dry_run:
            return {"action": "rescheduled", "record": cand, "reason": "dry-run: " + reason, "dry_run": True}
        fields = {k: cand[k] for k in ("start", "end", "all_day", "reminders", "reminder_rationale", "issues",
                                       "explanation", "confidence", "summary")}
        fields["title"] = existing["title"]
        if existing["status"] in ACTIVE_STATUSES:
            fields["status"] = cand["status"] if cand["status"] == STATUS_REVIEW else existing["status"]
        rec = self.store.update(existing["id"], "rescheduled", **fields)
        if self._calendar_ok() and rec.get("calendar_event_id"):
            self.calendar.update(rec["calendar_event_id"], rec)
        self.store.mark_seen(cand["gmail_msg_id"] or "", f"reschedule-of-{existing['id']}")
        return {"action": "rescheduled", "record": rec, "reason": reason}

    def _apply_cancel(self, existing, cand, dry_run):
        reason = f"sender cancelled '{existing['title']}' (reminder #{existing['id']})"
        if dry_run:
            return {"action": "cancelled", "record": existing, "reason": "dry-run: " + reason, "dry_run": True}
        title = existing["title"] if existing["title"].startswith("[Cancelled]") else "[Cancelled] " + existing["title"]
        rec = self.store.update(existing["id"], "cancelled_by_sender", status=STATUS_CANCELLED, title=title,
                                reminders=[])
        if self._calendar_ok() and rec.get("calendar_event_id"):
            self.calendar.update(rec["calendar_event_id"], rec)
        self.store.mark_seen(cand["gmail_msg_id"] or "", f"cancel-of-{existing['id']}")
        return {"action": "cancelled", "record": rec, "reason": reason}

    # ------------------------------------------------------------------
    # Review operations
    # ------------------------------------------------------------------
    def approve(self, rid: int) -> dict:
        rec = self.store.get(rid)
        if rec is None:
            raise KeyError(f"No reminder #{rid}")
        rec = self.store.update(rid, "approved", status=STATUS_SCHEDULED)
        self._sync_calendar(rec)
        self.store.record_feedback(rec["sender_domain"], rec["event_type"], "approve")
        return self.store.get(rid)

    def edit(self, rid: int, title: str = None, start: datetime = None, end: datetime = None,
             all_day: bool = None, reminder_minutes: list = None, location: str = None) -> dict:
        rec = self.store.get(rid)
        if rec is None:
            raise KeyError(f"No reminder #{rid}")
        fields = {}
        if title:
            fields["title"] = title
        if location is not None:
            fields["location"] = location
        new_all_day = rec["all_day"] if all_day is None else (1 if all_day else 0)
        if start is not None or all_day is not None:
            old_start = datetime.fromisoformat(rec["start"])
            old_end = datetime.fromisoformat(rec["end"])
            s = start or old_start
            if new_all_day:
                s = datetime.combine(s.date(), datetime.min.time())
                e = s + timedelta(days=1)
            else:
                duration = (old_end - old_start) if not rec["all_day"] else timedelta(hours=1)
                e = end or (s + duration)
            fields.update(start=s.isoformat(timespec="minutes"), end=e.isoformat(timespec="minutes"),
                          all_day=new_all_day)
            if reminder_minutes is None:
                plan = reminder_planner.plan_reminders(rec["event_type"], s, bool(new_all_day),
                                                       rec["priority_band"] or "Medium", now=self.now_fn(),
                                                       email_for_bands=tuple(self.cfg["email_notification_bands"]))
                fields.update(reminders=plan["overrides"], reminder_rationale=plan["rationale"])
        elif end is not None:
            fields["end"] = end.isoformat(timespec="minutes")
        if reminder_minutes is not None:
            fields["reminders"] = [{"method": "popup", "minutes": int(m)} for m in reminder_minutes][:5]
            fields["reminder_rationale"] = "set manually by you"
        fields["status"] = STATUS_SCHEDULED      # an edited reminder is a reviewed reminder
        fields["issues"] = []
        rec = self.store.update(rid, "edited", **fields)
        self._sync_calendar(rec)
        self.store.record_feedback(rec["sender_domain"], rec["event_type"], "edit")
        return self.store.get(rid)

    def dismiss(self, rid: int) -> dict:
        rec = self.store.get(rid)
        if rec is None:
            raise KeyError(f"No reminder #{rid}")
        if self._calendar_ok() and rec.get("calendar_event_id"):
            self.calendar.delete(rec["calendar_event_id"])
        rec = self.store.update(rid, "dismissed", status=STATUS_DISMISSED, calendar_event_id=None)
        self.store.record_feedback(rec["sender_domain"], rec["event_type"], "dismiss")
        return rec

    def restore(self, rid: int) -> dict:
        """Undo a dismissal: put the reminder back on the calendar."""
        rec = self.store.get(rid)
        if rec is None:
            raise KeyError(f"No reminder #{rid}")
        rec = self.store.update(rid, "restored", status=STATUS_SCHEDULED)
        self._sync_calendar(rec)
        return self.store.get(rid)

    def _sync_calendar(self, rec: dict):
        if not self._calendar_ok():
            return
        if rec.get("calendar_event_id"):
            self.calendar.update(rec["calendar_event_id"], rec)
        else:
            event_id = self.calendar.create(rec)
            self.store.update(rec["id"], "calendar_created", calendar_event_id=event_id)


def describe(rec: dict) -> str:
    """One-paragraph, human-readable view of a reminder record."""
    when = rec["start"][:10] if rec.get("all_day") else rec["start"].replace("T", " ")
    start_dt = datetime.fromisoformat(rec["start"])
    fires = [f"{start_dt - timedelta(minutes=r['minutes']):%d %b %H:%M}" + (" (email)" if r["method"] == "email" else "")
             for r in (rec.get("reminders") or [])]
    lines = [f"#{rec.get('id', '-')}  [{rec.get('status')}]  {rec.get('title')}",
             f"    When      : {when}{' (all day)' if rec.get('all_day') else ''}",
             f"    Type      : {rec.get('event_type')}   confidence {float(rec.get('confidence') or 0):.0%}   "
             f"priority {rec.get('priority_band')}",
             f"    Reminders : " + (", ".join(fires) or "none"),
             f"    From      : {rec.get('sender')}",
             f"    Summary   : {rec.get('summary')}"]
    if rec.get("location"):
        lines.append(f"    Where     : {rec['location']}")
    for i in rec.get("issues") or []:
        if i.get("severity") in ("major", "blocking"):
            lines.append(f"    CHECK     : {i['message']}")
    if rec.get("gmail_link"):
        lines.append(f"    Email     : {rec['gmail_link']}")
    return "\n".join(lines)
