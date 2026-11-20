"""
calendar_client.py
------------------
Smart Inbox-to-Calendar Assistant - Google Calendar API wrapper.

Turns a reminder record into a Google Calendar event that carries:
  * the extracted title, start/end (timed or all-day) and location/link
  * custom popup (and, for Critical items, email) reminders computed by
    reminder_planner.py - Google Calendar itself delivers the notifications
    to the user's phone/desktop/email
  * a description with a short summary of the email, why it was detected,
    any ambiguities to double-check, and a link back to the original email
  * `source` = the Gmail link (shown by Google Calendar as a clickable chip)
  * a colour by urgency, and status "tentative" while it awaits review
  * private extended properties (sica_msg_id, sica_fp) so the event can be
    traced back to its email - used as a second, calendar-side duplicate
    check that still works if the local database is lost.

Scope used: calendar.events only (cannot read calendar settings/sharing).
"""

from datetime import datetime, timedelta

from googleapiclient.discovery import build

import google_auth

SCOPES = [google_auth.CALENDAR_SCOPE]
REVIEW_PREFIX = "[Review] "

# Google Calendar colorIds: 11 tomato, 6 tangerine, 5 banana, 2 sage, 8 graphite
PRIORITY_COLORS = {"Critical": "11", "High": "6", "Medium": "5", "Low": "2"}


def get_calendar_service(interactive: bool = True):
    creds = google_auth.get_credentials(SCOPES, interactive=interactive)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _time_field(iso: str, all_day: bool, tz_name: str = "", is_end: bool = False):
    dt = datetime.fromisoformat(iso)
    if all_day:
        return {"date": dt.date().isoformat()}
    aware = dt.astimezone() if dt.tzinfo is None else dt   # attach the machine's local offset
    field = {"dateTime": aware.isoformat()}
    if tz_name:
        field["timeZone"] = tz_name
    return field


def build_description(rec: dict) -> str:
    lines = ["Created by Smart Inbox-to-Calendar Assistant", ""]
    if rec.get("summary"):
        lines += ["Summary:", rec["summary"], ""]
    lines.append(f"From: {rec.get('sender', '')}")
    lines.append(f"Subject: {rec.get('subject', '')}")
    if rec.get("location"):
        lines.append(f"Where / link: {rec['location']}")
    lines.append(f"Detected as: {rec.get('event_type', '').replace('_', ' ')} "
                 f"(confidence {float(rec.get('confidence') or 0) * 100:.0f}%, priority {rec.get('priority_band', '-')})")
    issues = [i for i in (rec.get("issues") or []) if i.get("severity") in ("major", "blocking")]
    if issues:
        lines += ["", "Please double-check:"] + [f"  - {i['message']}" for i in issues]
    if rec.get("reminder_rationale"):
        lines += ["", f"Reminder plan: {rec['reminder_rationale']}"]
    if rec.get("gmail_link"):
        lines += ["", f"Open the original email: {rec['gmail_link']}"]
    lines += ["", f"Review / edit / dismiss: python src/review.py show {rec.get('id', '')}".rstrip()]
    return "\n".join(lines)


def build_event_body(rec: dict, tz_name: str = "") -> dict:
    tentative = rec.get("status") == "needs_review"
    title = rec["title"]
    if tentative and not title.startswith(REVIEW_PREFIX):
        title = REVIEW_PREFIX + title
    body = {
        "summary": title,
        "description": build_description(rec),
        "start": _time_field(rec["start"], bool(rec.get("all_day")), tz_name),
        "end": _time_field(rec["end"], bool(rec.get("all_day")), tz_name, is_end=True),
        "status": "tentative" if tentative else "confirmed",
        "reminders": {"useDefault": False, "overrides": rec.get("reminders") or []},
        "extendedProperties": {"private": {
            "sica_msg_id": rec.get("gmail_msg_id") or "",
            "sica_fp": rec.get("fingerprint") or "",
            "sica_type": rec.get("event_type") or "",
        }},
    }
    if rec.get("all_day"):
        body["transparency"] = "transparent"   # a deadline shouldn't block the whole day as "busy"
    if rec.get("location"):
        body["location"] = rec["location"]
    color = PRIORITY_COLORS.get(rec.get("priority_band") or "")
    if rec.get("status") == "cancelled":
        color = "8"
    if color:
        body["colorId"] = color
    if rec.get("gmail_link"):
        body["source"] = {"title": "Original email in Gmail", "url": rec["gmail_link"]}
    return body


class CalendarClient:
    def __init__(self, service=None, calendar_id: str = "primary", tz_name: str = ""):
        self.service = service
        self.calendar_id = calendar_id or "primary"
        self.tz_name = tz_name

    def create(self, rec: dict) -> str:
        created = self.service.events().insert(calendarId=self.calendar_id,
                                               body=build_event_body(rec, self.tz_name)).execute()
        return created["id"]

    def update(self, event_id: str, rec: dict) -> str:
        updated = self.service.events().patch(calendarId=self.calendar_id, eventId=event_id,
                                              body=build_event_body(rec, self.tz_name)).execute()
        return updated["id"]

    def delete(self, event_id: str):
        try:
            self.service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except Exception as e:  # already deleted by the user in Calendar -> fine
            if "410" not in str(e) and "404" not in str(e):
                raise

    def find_by_message(self, msg_id: str):
        """Calendar-side idempotency: is there already an event created from
        this Gmail message (e.g. local database was reset)?"""
        resp = self.service.events().list(calendarId=self.calendar_id,
                                          privateExtendedProperty=f"sica_msg_id={msg_id}",
                                          maxResults=1, showDeleted=False).execute()
        items = resp.get("items", [])
        return items[0]["id"] if items else None
