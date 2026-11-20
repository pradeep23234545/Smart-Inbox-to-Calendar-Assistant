"""
event_extractor.py
------------------
Smart Inbox-to-Calendar Assistant - Temporal Commitment Extraction Engine.

Given one email (subject + body + the moment it was received), this module
decides whether the email contains a time-bound commitment - a meeting,
interview, appointment, exam, payment due date, submission deadline or
event - and, if so, extracts a structured, explainable event record:

    type, title, start/end, all-day or timed, location / meeting link,
    amount (for payments), a confidence score, every ambiguity it noticed,
    alternative interpretations, and a plain-language explanation.

It is pure Python (regular expressions + scored heuristics), runs fully
offline and never sends email content to any third-party service.

Pipeline
========
  1. Noise removal      - quoted replies ("On ... wrote:"), forwarded
                          headers and signatures/disclaimers are cut, so old
                          dates in a reply chain are never mistaken for the
                          new commitment.
  2. Date-mention scan  - absolute (15 Dec 2026, Dec 15th, 2026-12-15,
                          15/12/2026), relative (today, tomorrow, day after
                          tomorrow, in 3 days, end of week), and weekday
                          (this Friday, next Monday, on Thursday) forms.
  3. Time-mention scan  - 10 AM, 10:30 p.m., 14:00 hrs, noon, ranges
                          (2-3 PM, 10:00 to 11:30), time-zone suffixes
                          (IST, PST, UTC ...) converted to local time.
  4. Date-time binding  - each time is bound to the nearest date mention in
                          the same clause.
  5. Anchor scoring     - every candidate is scored by the cue words around
                          it (deadline / due / scheduled / by / before ...),
                          negative context (sent on, debited on, ordered on,
                          ago ...), whether it is in the subject, whether it
                          has a time, and whether it lies in the future
                          relative to when the email was RECEIVED.
  6. Ambiguity reasoning- DD/MM vs MM/DD, missing year, "next Friday",
                          weekday/date conflicts, business-hours guess for
                          times without AM/PM, missing time for a meeting,
                          vague expressions ("next week"), several competing
                          dates. Each produces a typed issue with a severity
                          that lowers confidence and can route the reminder
                          to human review instead of silent auto-creation.
  7. Commitment typing  - weighted keyword evidence (subject counts double,
                          words near the chosen date count double again).
  8. Record assembly    - title, duration by type, location/link/amount.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
            r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
WEEKDAY_ANY_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?|r)?|fri|sat|sun)"
WEEKDAY_FULL_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fourteen": 14,
    "fifteen": 15, "thirty": 30,
}

# Fixed offsets (hours) for common time-zone abbreviations seen in mail.
TZ_OFFSETS = {
    "ist": 5.5, "utc": 0, "gmt": 0, "pst": -8, "pdt": -7, "est": -5, "edt": -4,
    "cst": -6, "cdt": -5, "mst": -7, "mdt": -6, "bst": 1, "cet": 1, "cest": 2,
    "sgt": 8, "jst": 9, "aest": 10, "aedt": 11,
}
TZ_RE = r"\s*\(?\b(" + "|".join(TZ_OFFSETS) + r")\b\)?"

MERIDIEM_RE = r"(a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z])"

# Cue words that, appearing shortly BEFORE a date, mark it as the commitment.
CUES = [
    (r"\bdeadline\b", 0.32), (r"\bdue\b", 0.32), (r"\blast date\b", 0.34),
    (r"\bno later than\b", 0.34), (r"\bon or before\b", 0.34),
    (r"\bscheduled\b", 0.32), (r"\bwill be (?:held|conducted)\b", 0.32), (r"\bheld on\b", 0.3),
    (r"\bremind(?:er)?\b", 0.18), (r"\bby\b", 0.22), (r"\bbefore\b", 0.2),
    (r"\buntil\b|\btill\b", 0.18), (r"\bexpires?\b|\bexpiry\b", 0.22), (r"\bcloses?\b", 0.22),
    (r"\bstarts?\b|\bbegins?\b|\bcommences?\b", 0.22), (r"\binterview\b", 0.24),
    (r"\bmeeting\b|\bcall\b|\bsync\b", 0.2), (r"\bappointment\b", 0.24), (r"\bexam(?:ination)?\b", 0.24),
    (r"\bjoin\b|\battend\b|\breport\b", 0.16), (r"\bslot\b", 0.2), (r"\bon\b", 0.1), (r"\bat\b", 0.06),
    (r"\bfrom\b", 0.06), (r"\brescheduled\b|\bmoved to\b|\bpostponed\b|\bchanged to\b", 0.3),
]
# Context that marks a date as historical / bookkeeping, not a commitment.
NEGATIVE_CUES = [
    r"\bsent\b", r"\bposted\b", r"\bdated\b", r"\bsince\b", r"\bago\b", r"\bplaced on\b",
    r"\bordered on\b", r"\btransaction\b", r"\bdebited\b", r"\bcredited\b", r"\bwas\b", r"\bwere\b",
    r"\blast updated\b", r"\bcopyright\b", r"©", r"\bjoined\b", r"\bpublished\b",
    r"\breceived on\b", r"\bgenerated on\b", r"\bstatement (?:date|period)\b", r"\bbill date\b",
    r"\bissued on\b", r"\binvoice date\b", r"\bpaid on\b",
]

# Commitment types: (label, default duration minutes, keyword evidence)
EVENT_TYPES = {
    "interview": ("Interview", 60, [
        (r"\binterview(?:s|ed)?\b", 3), (r"\bshortlisted\b", 2), (r"\b(?:hr|technical|final|panel) round\b", 2.5),
        (r"\bassessment centre\b|\bassessment center\b", 2), (r"\brecruit(?:er|ment)\b", 1),
    ]),
    "meeting": ("Meeting", 60, [
        (r"\bmeeting\b", 3), (r"\bmeet\.google\.com\b|\bzoom\.us\b|\bteams\.microsoft\.com\b", 3),
        (r"\b(?:con)?call\b", 1.5), (r"\bsync(?:-up)?\b|\bstand-?up\b|\bcatch-?up\b", 2),
        (r"\bagenda\b", 1.5), (r"\bdiscussion\b|\breview session\b", 1), (r"\binvit(?:e|ation)\b", 1),
    ]),
    "appointment": ("Appointment", 30, [
        (r"\bappointment\b", 3), (r"\bconsultation\b", 2.5), (r"\b(?:doctor|dentist|clinic|hospital|dr\.)\b", 2),
        (r"\bbooking (?:is )?confirmed\b|\bslot (?:is )?(?:booked|confirmed)\b", 2.5), (r"\bvisit\b", 1),
        (r"\bservice centre\b|\bservice center\b", 1.5),
    ]),
    "exam": ("Exam", 180, [
        (r"\bexam(?:ination)?s?\b", 3), (r"\bquiz\b|\bviva\b|\bmid-?sem\b|\bend-?sem\b", 2.5),
        (r"\bhall ticket\b|\badmit card\b", 2.5), (r"\btest\b", 1),
    ]),
    "payment_due": ("Payment due", 0, [
        (r"\bpayment due\b|\bdue date\b|\bamount due\b|\bminimum due\b|\btotal due\b", 3),
        (r"\bpay (?:by|before|now)\b|\bmake (?:the |your )?payment\b", 2.5),
        (r"\bbill\b|\binvoice\b|\bstatement\b", 1.5), (r"\bemi\b|\bpremium\b|\bfee(?:s)?\b|\brent\b", 2),
        (r"\boverdue\b|\boutstanding\b|\blate fee\b|\bpenalty\b", 2), (r"\brenew(?:al|s)?\b|\bauto-?debit\b", 1.5),
    ]),
    "submission_deadline": ("Deadline", 0, [
        (r"\bdeadline\b", 3), (r"\bsubmi(?:t|ssion|tted)\b", 2.5), (r"\blast date\b", 3),
        (r"\bassignment\b|\bproject report\b|\bthesis\b|\bproposal\b", 2), (r"\bapply by\b|\bapplications? close\b", 2.5),
        (r"\bregistration(?:s)? (?:close|ends?)\b|\bregister by\b", 2.5), (r"\bupload\b|\bfill (?:the|out)\b", 1.5),
        (r"\bno later than\b|\bon or before\b", 2), (r"\bexpires?\b|\bexpiry\b", 1.5),
    ]),
    "event": ("Event", 120, [
        (r"\bwebinar\b|\bworkshop\b|\bseminar\b|\bconference\b|\bhackathon\b", 3),
        (r"\bfest\b|\bceremony\b|\borientation\b|\bconvocation\b|\bsession\b|\btalk\b", 2),
        (r"\brsvp\b|\bregistered\b|\byour ticket\b", 1.5),
    ]),
}

RESCHEDULE_RE = re.compile(r"\b(re-?scheduled|postponed|moved to|changed to|new (?:date|time|slot)|"
                           r"preponed|updated (?:date|time|schedule))\b", re.I)
CANCEL_RE = re.compile(r"\b(cancell?ed|called off|will not take place|has been withdrawn|stands cancell?ed)\b", re.I)

SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|reminder|gentle reminder|friendly reminder|urgent|important|action required|"
    r"imp|attn|attention|update|updated|invitation|invite)\s*[:\-|]\s*|\[[^\]]*\]\s*)", re.I)

MEETING_LINK_RE = re.compile(r"https?://(?:[\w.-]*\.)?(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|"
                             r"teams\.live\.com|webex\.com|gotomeeting\.com)/\S+", re.I)
LOCATION_RE = re.compile(r"\b(?:venue|location|address|where)\s*[:\-]\s*([^\n;]{3,120}?)(?=\.\s|\.?\n|;|\.?$)", re.I)
PROMO_RE = re.compile(r"(?:\bsale\b|% off\b|\bdiscount|\bcoupon|\buse code\b|\bcashback\b|\bdeals?\b|"
                      r"\boffer ends\b|\bshop now\b|\blimited time\b)", re.I)
AMOUNT_RE = re.compile(r"(?:(?:rs\.?|inr|₹)\s?([\d,]+(?:\.\d{1,2})?)|(?:\$|usd\s?)([\d,]+(?:\.\d{1,2})?))", re.I)

QUOTE_CUT_PATTERNS = [
    re.compile(r"^\s*On .{5,200}wrote:\s*$", re.M | re.I),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.M | re.I),
    re.compile(r"^\s*From:\s.+\n\s*(?:Sent|Date):\s", re.M | re.I),
    re.compile(r"^\s*>", re.M),
]
FOOTER_CUT_PATTERNS = [
    re.compile(r"^\s*(?:--\s*$|Sent from my )", re.M),
    re.compile(r"\b(?:unsubscribe|this e-?mail and any attachments|disclaimer:|confidentiality notice)", re.I),
]


# ----------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------
@dataclass
class Issue:
    code: str
    message: str
    severity: str = "minor"   # "minor" | "major" | "blocking"

    def to_dict(self):
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class DateMention:
    day: date
    span: tuple
    text: str
    kind: str                       # absolute | relative | weekday | time-only
    issues: list = field(default_factory=list)
    alternates: list = field(default_factory=list)   # [(date, reason)]
    default_time: time = None       # e.g. "tonight" -> 20:00, "EOD" -> 18:00
    vague: bool = False


@dataclass
class TimeMention:
    start: time
    end: time
    span: tuple
    text: str
    issues: list = field(default_factory=list)
    tz_offset_hours: float = None


@dataclass
class Candidate:
    start: datetime
    end: datetime
    has_time: bool
    span: tuple
    text: str
    kind: str
    score: float
    reasons: list
    issues: list
    alternates: list


# ----------------------------------------------------------------------
# Text preparation
# ----------------------------------------------------------------------
def strip_noise(body: str) -> str:
    """Remove quoted reply history, forwarded headers and footers, so dates
    from older messages in a thread are not mistaken for the new one."""
    if not body:
        return ""
    text = body.replace("\r\n", "\n")
    cut = len(text)
    for pat in QUOTE_CUT_PATTERNS:
        m = pat.search(text)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    text = text[:cut]
    for pat in FOOTER_CUT_PATTERNS:
        m = pat.search(text)
        if m and m.start() > 40:
            text = text[:m.start()]
    return text.strip()


def clean_subject(subject: str) -> str:
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = SUBJECT_PREFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -:|")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _num(word: str) -> int:
    return int(word) if word.isdigit() else NUMBER_WORDS.get(word.lower(), 0)


def _overlaps(span, taken):
    return any(span[0] < t[1] and t[0] < span[1] for t in taken)


def _resolve_year(month: int, day: int, ref: date, mention: str, issues: list):
    """Year was not written: choose the next occurrence on/after the
    reference date (allowing 1 day of slack for "yesterday's" mails)."""
    d = _safe_date(ref.year, month, day)
    if d is None:
        return None
    if d < ref - timedelta(days=1):
        d2 = _safe_date(ref.year + 1, month, day)
        if d2:
            d = d2
    issues.append(Issue("year_assumed", f"Year not stated in '{mention}' - assumed {d.year}.", "minor"))
    return d


def _parse_meridiem_hour(hour: int, meridiem: str) -> int:
    mer = meridiem.lower().replace(".", "").replace(" ", "")
    if mer == "pm" and hour < 12:
        return hour + 12
    if mer == "am" and hour == 12:
        return 0
    return hour


# ----------------------------------------------------------------------
# Date mentions
# ----------------------------------------------------------------------
def find_date_mentions(text: str, ref: datetime, date_order: str = "DMY") -> list:
    ref_d = ref.date()
    mentions, taken = [], []

    def add(m, mention):
        if mention is None or _overlaps(m.span(), taken):
            return
        taken.append(m.span())
        mentions.append(mention)

    # 1) ISO 2026-12-15
    for m in re.finditer(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add(m, DateMention(d, m.span(), m.group(0), "absolute"))

    # 2) Named month forms (optionally preceded by a weekday)
    named = [
        (rf"(?:\b{WEEKDAY_ANY_RE},?\s+(?:the\s+)?)?\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?[\s\-]+{MONTH_RE}\b\.?(?:,?[\s\-]+(\d{{4}}))?",
         lambda g: (g[1], g[2], g[3], g[0])),
        (rf"(?:\b{WEEKDAY_ANY_RE},?\s+)?\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?",
         lambda g: (g[2], g[1], g[3], g[0])),
    ]
    for pattern, pick in named:
        for m in re.finditer(pattern, text, re.I):
            day_s, mon_s, year_s, wd_s = pick(m.groups())
            month = MONTHS.get(mon_s.lower().rstrip("."))
            day = int(day_s)
            issues = []
            if year_s:
                d = _safe_date(int(year_s), month, day)
            else:
                d = _resolve_year(month, day, ref_d, m.group(0).strip(), issues)
            if d is None:
                continue
            if wd_s:
                d = _check_weekday(d, WEEKDAYS[wd_s.lower()], bool(year_s), m.group(0).strip(), issues)
            add(m, DateMention(d, m.span(), m.group(0).strip(), "absolute", issues))

    # 3) Numeric 15/12/2026, 15-12-2026, 15.12.2026, 15/12
    for m in re.finditer(r"(?<![\d/.\-:])(\d{1,2})([/.\-])(\d{1,2})(?:\2(\d{4}|\d{2}))?(?![\d/\-:]|\.\d)", text):
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        if sep in ".-" and not y:
            continue  # "10.30" / "10-11" are times or ranges, not dates
        mention = _numeric_date(a, b, y, ref_d, date_order, m.group(0))
        if mention:
            mention.span = m.span()
            add(m, mention)

    # 4) Relative expressions
    rel = [
        (r"\bday after tomorrow\b", lambda m: (ref_d + timedelta(days=2), None)),
        (r"\b(?:tomorrow|tmrw|tmr)\b", lambda m: (ref_d + timedelta(days=1), None)),
        (r"\btonight\b", lambda m: (ref_d, time(20, 0))),
        (r"\bthis evening\b", lambda m: (ref_d, time(18, 0))),
        (r"\bthis afternoon\b", lambda m: (ref_d, time(14, 0))),
        (r"\bthis morning\b", lambda m: (ref_d, time(9, 0))),
        (r"\btoday\b", lambda m: (ref_d, None)),
        (r"\b(?:end of (?:the )?(?:business )?day|eod|cob|close of business)\b", lambda m: (ref_d, time(18, 0))),
        (r"\b(?:end of (?:the |this )?week|eow)\b",
         lambda m: (ref_d + timedelta(days=(4 - ref_d.weekday()) % 7), time(18, 0))),
        (r"\b(?:end of (?:the |this )?month|eom)\b", lambda m: (_month_end(ref_d), time(23, 59))),
    ]
    for pattern, fn in rel:
        for m in re.finditer(pattern, text, re.I):
            d, t = fn(m)
            add(m, DateMention(d, m.span(), m.group(0), "relative", default_time=t))

    for m in re.finditer(r"\b(in|within|after)\s+(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fourteen|fifteen|thirty)"
                         r"\s+(business\s+|working\s+)?(day|week|hour)s?\b", text, re.I):
        n, unit = _num(m.group(2)), m.group(4).lower()
        if n <= 0:
            continue
        if unit == "hour":
            dt = ref + timedelta(hours=n)
            add(m, DateMention(dt.date(), m.span(), m.group(0), "relative",
                               default_time=dt.time().replace(second=0, microsecond=0)))
            continue
        days = n * 7 if unit == "week" else n
        if m.group(3):  # business/working days -> skip weekends
            d, left = ref_d, n
            while left:
                d += timedelta(days=1)
                if d.weekday() < 5:
                    left -= 1
        else:
            d = ref_d + timedelta(days=days)
        add(m, DateMention(d, m.span(), m.group(0), "relative"))

    for m in re.finditer(r"\bnext week\b", text, re.I):
        d = ref_d + timedelta(days=7 - ref_d.weekday())
        add(m, DateMention(d, m.span(), m.group(0), "relative", vague=True, issues=[
            Issue("vague_date", "'next week' does not name a day - placed on Monday as a placeholder.", "major")]))

    # 5) Weekday expressions: this/next/coming Friday, on Friday
    for m in re.finditer(rf"\b(?:(this|next|coming|upcoming)\s+)?{WEEKDAY_FULL_RE}\b", text, re.I):
        qualifier = (m.group(1) or "").lower()
        wd = WEEKDAYS[m.group(2).lower()]
        delta = (wd - ref_d.weekday()) % 7
        issues, alts = [], []
        if qualifier == "this":
            d = ref_d + timedelta(days=delta)
        elif qualifier == "next":
            next_monday = ref_d + timedelta(days=7 - ref_d.weekday())
            d = next_monday + timedelta(days=wd)
            coming = ref_d + timedelta(days=delta or 7)
            if coming != d:
                alts.append((coming, f"the coming {WEEKDAY_NAMES[wd]}"))
                issues.append(Issue("next_weekday_ambiguous",
                                    f"'{m.group(0)}' could mean {coming:%a %d %b} or {d:%a %d %b}; "
                                    f"chose {d:%d %b} (next calendar week).", "major"))
        else:
            if delta == 0:
                d = ref_d + timedelta(days=7)
                alts.append((ref_d, "today"))
                issues.append(Issue("weekday_today_ambiguous",
                                    f"'{m.group(0)}' was written on a {WEEKDAY_NAMES[wd]} - assumed a week later "
                                    f"({d:%d %b}), could mean today.", "major"))
            else:
                d = ref_d + timedelta(days=delta)
        add(m, DateMention(d, m.span(), m.group(0), "weekday", issues, alts))

    return mentions


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _check_weekday(d: date, wd: int, year_given: bool, mention: str, issues: list) -> date:
    if d.weekday() == wd:
        return d
    if not year_given:
        alt = _safe_date(d.year + 1, d.month, d.day)
        if alt and alt.weekday() == wd:
            issues.append(Issue("year_from_weekday",
                                f"'{mention}' matches {alt:%A %d %b %Y}, so the year {alt.year} was chosen.", "minor"))
            return alt
    issues.append(Issue("weekday_conflict",
                        f"'{mention}': {d:%d %b %Y} is a {d:%A}, not a {WEEKDAY_NAMES[wd]}. Please verify.", "major"))
    return d


def _numeric_date(a, b, y, ref_d, date_order, text):
    issues = []
    year = None
    if y:
        year = int(y) + (2000 if len(y) == 2 else 0)

    def build(day, month):
        if not (1 <= month <= 12):
            return None
        if year:
            return _safe_date(year, month, day)
        d = _safe_date(ref_d.year, month, day)
        if d and d < ref_d - timedelta(days=1):
            d = _safe_date(ref_d.year + 1, month, day) or d
        return d

    dmy, mdy = build(a, b), build(b, a)
    if dmy is None and mdy is None:
        return None
    if dmy and mdy and dmy != mdy:
        preferred, other = (dmy, mdy) if date_order.upper() == "DMY" else (mdy, dmy)
        horizon = ref_d - timedelta(days=1)
        if preferred < horizon <= other:
            preferred, other = other, preferred
            issues.append(Issue("date_order_resolved",
                                f"'{text}' read as {preferred:%d %b %Y} because the other reading is in the past.",
                                "minor"))
        else:
            issues.append(Issue("date_order_ambiguous",
                                f"'{text}' could be {preferred:%d %b %Y} or {other:%d %b %Y}; "
                                f"chose {date_order.upper()} order.", "major"))
        d, alts = preferred, [(other, "other day/month order")]
    else:
        d, alts = (dmy or mdy), []
    if not year:
        issues.append(Issue("year_assumed", f"Year not stated in '{text}' - assumed {d.year}.", "minor"))
    return DateMention(d, (0, 0), text, "absolute", issues, alts)


# ----------------------------------------------------------------------
# Time mentions
# ----------------------------------------------------------------------
def find_time_mentions(text: str, date_spans: list) -> list:
    times, taken = [], list(date_spans)

    def add(m, start, end, issues=None):
        span = m.span()
        if _overlaps(span, taken):
            return
        tz_m = re.match(TZ_RE, text[span[1]:span[1] + 8], re.I)
        tz = None
        if tz_m:
            tz = TZ_OFFSETS[tz_m.group(1).lower()]
            span = (span[0], span[1] + tz_m.end())
        taken.append(span)
        times.append(TimeMention(start, end, span, text[span[0]:span[1]].strip(), issues or [], tz))

    # ranges: 2-3 PM, 10:00 am to 11:30 am, 14:00-15:30
    for m in re.finditer(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(?:{MERIDIEM_RE})?\s*(?:-|–|—|to|till|until)\s*"
                         rf"(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{MERIDIEM_RE}", text, re.I):
        h1, m1, mer1, h2, m2, mer2 = m.groups()
        h1, h2 = int(h1), int(h2)
        if h1 > 12 or h2 > 12:
            continue
        start_h = _parse_meridiem_hour(h1, mer1 or mer2)
        end_h = _parse_meridiem_hour(h2, mer2)
        if mer1 is None and start_h > end_h:
            start_h = h1  # "11-1 pm" -> 11 am to 1 pm
        try:
            add(m, time(start_h, int(m1 or 0)), time(end_h, int(m2 or 0)))
        except ValueError:
            continue
    for m in re.finditer(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:-|–|to)\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b(?!\s*[ap]\.?m)",
                         text, re.I):
        try:
            add(m, time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4))))
        except ValueError:
            continue

    # 12-hour clock: 10 AM, 10:30 p.m.
    for m in re.finditer(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{MERIDIEM_RE}", text, re.I):
        h = int(m.group(1))
        if not 1 <= h <= 12:
            continue
        try:
            add(m, time(_parse_meridiem_hour(h, m.group(3)), int(m.group(2) or 0)), None)
        except ValueError:
            continue

    # noon / midnight
    for m in re.finditer(r"\b(noon|midday|midnight)\b", text, re.I):
        word = m.group(1).lower()
        add(m, time(23, 59) if word == "midnight" else time(12, 0), None)

    # 24-hour clock with a colon: 14:00, 09:30 hrs, 2:30 (no meridiem)
    for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s*(?:hrs|hours|h)\b)?", text, re.I):
        h, mi = int(m.group(1)), int(m.group(2))
        issues = []
        if 1 <= h <= 6 and not m.group(1).startswith("0"):
            issues.append(Issue("meridiem_guess",
                                f"'{m.group(0)}' has no AM/PM - assumed {h + 12}:{mi:02d} (business hours).", "major"))
            h += 12
        add(m, time(h, mi), None, issues)

    return times


# ----------------------------------------------------------------------
# Binding, scoring and selection
# ----------------------------------------------------------------------
def _context_score(text_lower: str, span: tuple, in_subject: bool):
    before = text_lower[max(0, span[0] - 60):span[0]]
    after = text_lower[span[1]:span[1] + 25]
    score, reasons = 0.3, []
    best_cue = 0
    for pattern, weight in CUES:
        m = None
        for m in re.finditer(pattern, before):
            pass
        if m:
            # nearer cues count fully, distant cues partially
            dist = len(before) - m.end()
            w = weight * (1.0 if dist <= 25 else 0.6)
            if w > best_cue:
                best_cue = w
                cue_word = m.group(0)
    if best_cue:
        score += best_cue
        reasons.append(f"cue '{cue_word}' before the date (+{best_cue:.2f})")
    for pattern in NEGATIVE_CUES:
        if re.search(pattern, before[-35:]) or re.search(pattern, after[:15]):
            score -= 0.35
            reasons.append("historical/bookkeeping context nearby (-0.35)")
            break
    if in_subject:
        score += 0.15
        reasons.append("appears in the subject line (+0.15)")
    return score, reasons


def _bind_and_score(text: str, subject_len: int, ref: datetime, date_mentions, time_mentions, local_tz):
    text_lower = text.lower()
    candidates, used_times = [], set()

    def nearest_time(span):
        best, best_d = None, 61
        for i, t in enumerate(time_mentions):
            if i in used_times:
                continue
            gap = t.span[0] - span[1] if t.span[0] >= span[1] else span[0] - t.span[1]
            between = text[min(span[1], t.span[1]):max(span[0], t.span[0])]
            if gap < best_d and "\n\n" not in between and not re.search(r"[.!?]\s+[A-Z]", between):
                best, best_d = i, gap
        return best

    for dm in date_mentions:
        ti = nearest_time(dm.span)
        issues = list(dm.issues)
        reasons = []
        has_time = False
        end = None
        if ti is not None:
            used_times.add(ti)
            tm = time_mentions[ti]
            start = datetime.combine(dm.day, tm.start)
            if tm.end:
                end = datetime.combine(dm.day, tm.end)
                if end <= start:
                    end += timedelta(days=1)
            has_time = True
            issues += tm.issues
            start, end = _convert_tz(start, end, tm, local_tz, issues)
            text_span = (min(dm.span[0], tm.span[0]), max(dm.span[1], tm.span[1]))
            reasons.append(f"time '{tm.text}' bound to date '{dm.text}'")
        elif dm.default_time:
            start = datetime.combine(dm.day, dm.default_time)
            has_time = True
            text_span = dm.span
        else:
            start = datetime.combine(dm.day, time(0, 0))
            text_span = dm.span
        in_subject = dm.span[0] < subject_len
        score, ctx = _context_score(text_lower, dm.span, in_subject)
        reasons += ctx
        if has_time:
            score += 0.1
            reasons.append("has an explicit time (+0.10)")
        if dm.kind == "absolute":
            score += 0.05
        if dm.vague:
            score -= 0.1
        cmp_ref = ref if has_time else datetime.combine(ref.date(), time(0, 0))
        if start < cmp_ref - timedelta(minutes=1):
            score -= 0.5
            issues.append(Issue("in_past", f"'{dm.text}' ({start:%d %b %Y}) is before the email was received.", "blocking"))
        elif (start - ref).days > 400:
            score -= 0.2
            reasons.append("more than a year away (-0.20)")
        candidates.append(Candidate(start, end, has_time, text_span, text[text_span[0]:text_span[1]], dm.kind,
                                    round(score, 3), reasons, issues, dm.alternates))

    # Times with no date at all ("join at 4 PM") -> assume the received day.
    for i, tm in enumerate(time_mentions):
        if i in used_times:
            continue
        day = ref.date()
        start = datetime.combine(day, tm.start)
        issues = [Issue("date_missing", f"'{tm.text}' has no date - assumed the day the email arrived.", "major")]
        if start < ref:
            start += timedelta(days=1)
            issues[0] = Issue("date_missing", f"'{tm.text}' has no date - assumed the next day ({start:%d %b}).", "major")
        end = datetime.combine(start.date(), tm.end) if tm.end else None
        issues += tm.issues
        start, end = _convert_tz(start, end, tm, local_tz, issues)
        score, ctx = _context_score(text_lower, tm.span, tm.span[0] < subject_len)
        candidates.append(Candidate(start, end, True, tm.span, tm.text, "time-only", round(score - 0.1, 3),
                                    ["time without a date (-0.10)"] + ctx, issues, []))
    return candidates


def _convert_tz(start, end, tm: TimeMention, local_tz, issues):
    if tm.tz_offset_hours is None or local_tz is None:
        return start, end
    src = timezone(timedelta(hours=tm.tz_offset_hours))
    local_offset = local_tz.utcoffset(start)
    if local_offset == timedelta(hours=tm.tz_offset_hours):
        return start, end
    conv = lambda d: d.replace(tzinfo=src).astimezone(local_tz).replace(tzinfo=None) if d else None
    new_start, new_end = conv(start), conv(end)
    issues.append(Issue("timezone_converted",
                        f"'{tm.text}' converted to your local time: {new_start:%d %b %H:%M}.", "minor"))
    return new_start, new_end


# ----------------------------------------------------------------------
# Commitment typing
# ----------------------------------------------------------------------
def classify_commitment(subject: str, body: str, anchor_span=None, full_text: str = ""):
    """Weighted keyword evidence for each commitment type. Subject matches
    count double; matches within 120 chars of the chosen date count double
    again. Returns (type, confidence 0-1, matched keywords, all scores)."""
    subj, bod = (subject or "").lower(), (body or "").lower()
    near = ""
    if anchor_span and full_text:
        near = full_text[max(0, anchor_span[0] - 120):anchor_span[1] + 120].lower()
    scores, hits = {}, {}
    for etype, (_, _, keywords) in EVENT_TYPES.items():
        s, h = 0.0, []
        for pattern, weight in keywords:
            found = False
            if re.search(pattern, subj):
                s += weight * 2
                found = True
            if re.search(pattern, bod):
                s += weight
                found = True
            if near and re.search(pattern, near):
                s += weight
            if found:
                h.append(re.search(pattern, subj + " " + bod).group(0))
        scores[etype], hits[etype] = s, h
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if scores[best] <= 0:
        return None, 0.0, [], scores
    confidence = min(1.0, 0.35 + 0.65 * (scores[best] / total) * min(1.0, scores[best] / 6))
    return best, round(confidence, 3), hits[best], scores


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def extract_event(subject: str, body: str, received: datetime = None, date_order: str = "DMY",
                  local_tz=None, sender: str = "") -> dict:
    """Analyse one email. Returns a dict with is_event=False when no
    time-bound commitment is found, otherwise a full event record."""
    received = (received or datetime.now()).replace(second=0, microsecond=0)
    if received.tzinfo is not None:
        received = received.astimezone(local_tz).replace(tzinfo=None) if local_tz else received.replace(tzinfo=None)
    if local_tz is None:
        local_tz = datetime.now().astimezone().tzinfo

    clean_body = strip_noise(body or "")
    subject = subject or ""
    text = subject + "\n" + clean_body
    subject_len = len(subject)

    dates = find_date_mentions(text, received, date_order)
    times = find_time_mentions(text, [d.span for d in dates])
    candidates = _bind_and_score(text, subject_len, received, dates, times, local_tz)

    result = {"is_event": False, "reason": "", "subject": subject, "received": received.isoformat(timespec="minutes")}
    if not candidates:
        result["reason"] = "no date or time expression found"
        return result

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]

    etype, type_conf, type_hits, type_scores = classify_commitment(subject, clean_body, best.span, text)
    cancelled = bool(CANCEL_RE.search(text))
    reschedule = bool(RESCHEDULE_RE.search(text))

    strong_cue = best.score >= 0.55
    if etype is None and PROMO_RE.search(text):
        result["reason"] = "promotional time pressure (sale/offer ending), not a personal commitment"
        return result
    if etype is None and not strong_cue:
        result["reason"] = "dates found, but no commitment language around them"
        return result
    if best.score < 0.25 and not cancelled:
        result["reason"] = f"best date '{best.text}' looks historical/incidental (score {best.score:.2f})"
        return result
    if etype is None:
        etype, type_conf, type_hits = "submission_deadline", 0.4, []

    label, default_minutes, _ = EVENT_TYPES[etype]
    issues = list(best.issues)

    # Competing dates with similar scores -> the reader should double-check.
    rivals = [c for c in candidates[1:] if c.score >= best.score - 0.1 and c.start.date() != best.start.date()
              and not any(i.code == "in_past" for i in c.issues)]
    if rivals:
        issues.append(Issue("multiple_dates", "Several dates are mentioned: " +
                            ", ".join(f"{c.text} ({c.start:%d %b})" for c in rivals[:3]) +
                            f". Chose '{best.text}'.", "major" if len(rivals) > 1 or rivals[0].score >= best.score - 0.03 else "minor"))

    timed_types = {"interview", "meeting", "appointment", "exam", "event"}
    all_day = not best.has_time
    if all_day and etype in timed_types:
        issues.append(Issue("time_missing", f"No time given for this {label.lower()} - added as an all-day entry.", "major"))

    start = best.start
    end = best.end
    if not all_day and end is None:
        end = start + timedelta(minutes=default_minutes or 15)
    if all_day:
        start = datetime.combine(start.date(), time(0, 0))
        end = start + timedelta(days=1)

    # Confidence: anchor strength + type evidence + completeness - ambiguity.
    anchor = max(0.0, min(1.0, best.score))
    completeness = 1.0 if (best.has_time or etype in ("payment_due", "submission_deadline")) else 0.5
    confidence = 0.45 * anchor + 0.35 * type_conf + 0.2 * completeness
    penalty = {"minor": 0.03, "major": 0.12, "blocking": 0.35}
    for iss in issues:
        confidence -= penalty.get(iss.severity, 0.05)
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    explanation = [f"Commitment type '{label}' from keywords: {', '.join(type_hits) or '(date cue only)'}",
                   f"Chosen date/time '{best.text}' -> {start:%a %d %b %Y}" + ("" if all_day else f" {start:%H:%M}")]
    explanation += best.reasons
    if len(candidates) > 1:
        explanation.append(f"{len(candidates) - 1} other date/time mention(s) scored lower")

    alternates = [{"start": d.isoformat(), "why": why} for d, why in best.alternates]
    alternates += [{"start": c.start.isoformat(timespec="minutes"), "why": f"other mention '{c.text}'"}
                   for c in candidates[1:4]]

    amount = None
    am = AMOUNT_RE.search(text)
    if am and etype == "payment_due":
        raw = am.group(1) or am.group(2)
        amount = ("Rs." if am.group(1) else "$") + raw
    meeting_link = None
    ml = MEETING_LINK_RE.search(body or "")
    if ml:
        meeting_link = ml.group(0).rstrip(").,>")
    location = None
    lm = LOCATION_RE.search(clean_body)
    if lm:
        location = lm.group(1).strip()

    base_title = clean_subject(subject) or (clean_body[:60] + "...")
    already_named = any(h.lower() in base_title.lower() for h in type_hits + [label.split()[0]])
    if already_named and not amount:
        title = base_title
    else:
        title = f"{label}{' ' + amount if amount else ''}: {base_title}"
    if cancelled:
        title = f"[Cancelled] {title}"

    result.update({
        "is_event": True,
        "event_type": etype,
        "type_label": label,
        "type_confidence": type_conf,
        "type_keywords": type_hits,
        "title": title[:120],
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "all_day": all_day,
        "has_time": best.has_time,
        "date_text": best.text,
        "anchor_score": round(best.score, 3),
        "confidence": confidence,
        "issues": [i.to_dict() for i in issues],
        "alternates": alternates,
        "explanation": explanation,
        "amount": amount,
        "meeting_link": meeting_link,
        "location": location or meeting_link,
        "reschedule": reschedule,
        "cancelled": cancelled,
        "clean_body": clean_body,
    })
    return result


def blocking_issues(event: dict) -> list:
    return [i for i in event.get("issues", []) if i["severity"] == "blocking"]


def review_issues(event: dict) -> list:
    return [i for i in event.get("issues", []) if i["severity"] in ("major", "blocking")]
