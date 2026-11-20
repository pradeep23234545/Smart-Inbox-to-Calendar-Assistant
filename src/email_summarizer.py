"""
email_summarizer.py
-------------------
Smart Inbox-to-Calendar Assistant - commitment-focused extractive summary.

The calendar event carries a 1-3 sentence summary of the original email so
the user knows what the reminder is about without opening Gmail. Instead of
a generic "first N characters", sentences are ranked by how much they say
about the commitment itself:

  +3.0  contains the chosen date/time expression
  +1.5  contains a commitment keyword (interview, due, submit, venue ...)
  +1.0  contains an instruction ("please", "kindly", "must", "required")
  +0.8  contains a link, amount or location marker
  +0.5  is one of the first two sentences (emails front-load the point)
  -1.0  greeting / sign-off / boilerplate

The top sentences are returned in their original order, capped in length.
Runs fully offline - no email text leaves the machine.
"""

import re

COMMITMENT_WORDS = re.compile(
    r"\b(interview|meeting|appointment|exam|deadline|due|submit|submission|payment|pay|venue|location|"
    r"schedule[sd]?|join|attend|report|register|registration|renew|expires?|last date|slot|round)\b", re.I)
INSTRUCTION_WORDS = re.compile(r"\b(please|kindly|must|required|mandatory|ensure|make sure|do not forget|carry|bring)\b", re.I)
DETAIL_MARKERS = re.compile(r"(https?://|rs\.?\s?\d|inr|\$\d|₹|venue|room|address|link)", re.I)
ABBREVIATION_END = re.compile(r"\b(?:dr|mr|mrs|ms|prof|st|no|vs|sr|jr|approx|dept|ref)\.$", re.I)
BOILERPLATE = re.compile(r"^(hi|hello|dear|hey|greetings|thanks|thank you|regards|best|cheers|sincerely|warm regards)\b[\s,!.]*",
                         re.I)


def split_sentences(text: str) -> list:
    text = re.sub(r"[ \t]+", " ", text or "")
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+", text)
    merged = []
    for p in parts:
        # re-join splits that happened after an abbreviation ("Dr. Mehta", "No. 5")
        if merged and ABBREVIATION_END.search(merged[-1]):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return [p.strip() for p in merged if len(p.strip()) > 3]


def summarize(text: str, date_text: str = "", max_sentences: int = 2, max_chars: int = 320) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return ""
    scored = []
    for i, s in enumerate(sentences):
        score = 0.0
        if date_text and date_text.lower() in s.lower():
            score += 3.0
        if COMMITMENT_WORDS.search(s):
            score += 1.5
        if INSTRUCTION_WORDS.search(s):
            score += 1.0
        if DETAIL_MARKERS.search(s):
            score += 0.8
        if i < 2:
            score += 0.5
        if BOILERPLATE.match(s) and len(s) < 40:
            score -= 1.0
        scored.append((score, i, s))
    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:max_sentences]
    summary = " ".join(s for _, _, s in sorted(top, key=lambda t: t[1]))
    if len(summary) > max_chars:
        summary = summary[:max_chars - 3].rsplit(" ", 1)[0] + "..."
    return summary
