"""
reminder_planner.py
-------------------
Smart Inbox-to-Calendar Assistant - Adaptive Reminder Lead-Time Planner.

A fixed "remind me 30 minutes before" is wrong for most commitments: a
credit-card payment needs days of notice, an interview needs preparation
time the day before plus a nudge to leave, and a deadline announced two
months ahead needs an early "start working on this" prompt.

This planner computes up to three Google Calendar reminder offsets for each
event from four inputs:

  1. Commitment type  - each type has a profile: a fraction of the available
                        lead window for the early "preparation" reminder,
                        plus fixed "final" reminders (e.g. interview: 1 day
                        and 2 hours before; payment: 3 days and 1 day).
  2. Urgency          - the classifier's priority band scales the early
                        reminder (Critical x1.5 ... Low x0.75).
  3. Lead window      - time between NOW and the event. Reminders that would
                        fire in the past are dropped; an event that is very
                        close still gets one immediate reminder.
  4. Quiet hours      - a timed reminder that would fire between 22:00 and
                        07:00 is moved earlier to 21:00 the evening before,
                        so a 6 AM interview reminder doesn't fire at 4 AM.

All-day events (deadlines/payments without a time) get "N days before at
09:00" reminders, expressed in Google's minutes-before-midnight form.

Google Calendar limits: at most 5 overrides, each <= 40320 minutes (4 weeks).
Every chosen reminder carries a human-readable rationale.
"""

from datetime import datetime, timedelta

MAX_OVERRIDE_MINUTES = 40320          # Google Calendar's 4-week ceiling
MAX_REMINDERS = 3
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7
QUIET_SHIFT_TO_HOUR = 21

URGENCY_MULTIPLIER = {"Critical": 1.5, "High": 1.25, "Medium": 1.0, "Low": 0.75}

# prep_fraction: share of the lead window for the early reminder
# prep_min/prep_max: bounds on that early reminder (minutes)
# finals: fixed reminders close to the event (minutes before start)
# all_day_days: for all-day events, "N days before at 09:00"
TYPE_PROFILES = {
    "interview":           {"prep_fraction": 0.25, "prep_min": 1440, "prep_max": 3 * 1440, "finals": [1440, 120],
                            "all_day_days": [2, 1]},
    "exam":                {"prep_fraction": 0.30, "prep_min": 2 * 1440, "prep_max": 7 * 1440, "finals": [1440, 120],
                            "all_day_days": [7, 2, 1]},
    "payment_due":         {"prep_fraction": 0.20, "prep_min": 3 * 1440, "prep_max": 5 * 1440, "finals": [1440, 180],
                            "all_day_days": [3, 1]},
    "submission_deadline": {"prep_fraction": 0.30, "prep_min": 1440, "prep_max": 10 * 1440, "finals": [1440, 180],
                            "all_day_days": [5, 2, 1]},
    "meeting":             {"prep_fraction": 0.0, "prep_min": 0, "prep_max": 0, "finals": [1440, 15],
                            "all_day_days": [1]},
    "appointment":         {"prep_fraction": 0.0, "prep_min": 0, "prep_max": 0, "finals": [1440, 60],
                            "all_day_days": [1]},
    "event":               {"prep_fraction": 0.10, "prep_min": 1440, "prep_max": 3 * 1440, "finals": [60],
                            "all_day_days": [1]},
}
DEFAULT_PROFILE = {"prep_fraction": 0.2, "prep_min": 1440, "prep_max": 3 * 1440, "finals": [60], "all_day_days": [1]}

ALL_DAY_REMIND_HOUR = 9


def _fmt(minutes: int) -> str:
    if minutes % 1440 == 0 and minutes:
        d = minutes // 1440
        return f"{d} day{'s' if d > 1 else ''}"
    if minutes >= 1440:
        return f"{minutes / 1440:.1f} days"
    if minutes % 60 == 0 and minutes:
        h = minutes // 60
        return f"{h} hour{'s' if h > 1 else ''}"
    return f"{minutes} min"


def _shift_out_of_quiet_hours(start: datetime, minutes: int):
    fire = start - timedelta(minutes=minutes)
    if fire.hour >= QUIET_START_HOUR or fire.hour < QUIET_END_HOUR:
        evening = fire.replace(hour=QUIET_SHIFT_TO_HOUR, minute=0)
        if fire.hour < QUIET_END_HOUR:
            evening -= timedelta(days=1)
        new_minutes = int((start - evening).total_seconds() // 60)
        return new_minutes, True
    return minutes, False


def plan_reminders(event_type: str, start: datetime, all_day: bool, priority_band: str = "Medium",
                   now: datetime = None, email_for_bands=("Critical",)) -> dict:
    """Return {"overrides": [{"method", "minutes"}], "plan": [{"minutes",
    "fires_at", "why"}], "rationale": str}."""
    now = now or datetime.now()
    profile = TYPE_PROFILES.get(event_type, DEFAULT_PROFILE)
    mult = URGENCY_MULTIPLIER.get(priority_band, 1.0)
    lead_min = int((start - now).total_seconds() // 60)
    picked = {}  # minutes -> why

    if all_day:
        for days in profile["all_day_days"]:
            minutes = days * 1440 - ALL_DAY_REMIND_HOUR * 60
            picked.setdefault(minutes, f"{days} day{'s' if days > 1 else ''} before at "
                                       f"{ALL_DAY_REMIND_HOUR:02d}:00 ({event_type.replace('_', ' ')} profile)")
        if priority_band in ("Critical", "High") and lead_min > 7 * 1440:
            minutes = 7 * 1440 - ALL_DAY_REMIND_HOUR * 60
            picked.setdefault(minutes, f"one week early because priority is {priority_band}")
    else:
        if profile["prep_fraction"] and lead_min > 0:
            prep = lead_min * profile["prep_fraction"] * mult
            prep = int(max(profile["prep_min"], min(profile["prep_max"] * mult, prep)))
            prep = (prep // 60) * 60 or prep
            picked[prep] = (f"preparation reminder: {profile['prep_fraction']:.0%} of the {_fmt(lead_min)} lead "
                            f"time x{mult} urgency ({priority_band})")
        for minutes in profile["finals"]:
            picked.setdefault(minutes, f"standard {_fmt(minutes)} reminder for this {event_type.replace('_', ' ')}")

    plan = []
    for minutes in sorted(picked, reverse=True):
        why = picked[minutes]
        if not all_day:
            shifted, moved = _shift_out_of_quiet_hours(start, minutes)
            if moved:
                why += f"; moved to {QUIET_SHIFT_TO_HOUR}:00 to avoid quiet hours ({QUIET_START_HOUR}:00-0{QUIET_END_HOUR}:00)"
                minutes = shifted
        minutes = min(minutes, MAX_OVERRIDE_MINUTES)
        fires_at = start - timedelta(minutes=minutes)
        if fires_at < now or minutes <= 0:
            continue
        if any(abs(p["minutes"] - minutes) < 30 for p in plan):
            continue
        plan.append({"minutes": minutes, "fires_at": fires_at.isoformat(timespec="minutes"), "why": why})

    plan = sorted(plan, key=lambda p: -p["minutes"])
    if len(plan) > MAX_REMINDERS:
        # keep the earliest (preparation) and the closest ones
        plan = [plan[0]] + plan[-(MAX_REMINDERS - 1):]

    if not plan:
        minutes = max(0, min(lead_min - 1, 10)) if lead_min > 0 else 0
        plan.append({"minutes": minutes, "fires_at": (start - timedelta(minutes=minutes)).isoformat(timespec="minutes"),
                     "why": "event is very close - single immediate reminder"})

    method_email = priority_band in email_for_bands
    overrides = [{"method": "popup", "minutes": p["minutes"]} for p in plan]
    if method_email:
        overrides.append({"method": "email", "minutes": plan[0]["minutes"]})
    overrides = overrides[:5]

    rationale = "; ".join(f"{datetime.fromisoformat(p['fires_at']):%a %d %b %H:%M} - {p['why']}" for p in plan)
    if method_email:
        rationale += f"; plus an email notification because priority is {priority_band}"
    return {"overrides": overrides, "plan": plan, "rationale": rationale}
