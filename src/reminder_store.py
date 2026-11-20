"""
reminder_store.py
-----------------
Smart Inbox-to-Calendar Assistant - reminder ledger, duplicate detection
and feedback memory (SQLite, stored in the per-user app-data folder).

Multi-layer duplicate / update detection
========================================
Before any calendar event is created, a new candidate is compared with the
ledger in this order:

  L1  same Gmail message id               -> already handled, skip
  L2  same Gmail thread + same type       -> follow-up in the same thread:
                                             same date = duplicate,
                                             different date = RESCHEDULE
                                             (update the existing event)
  L3  identical semantic fingerprint      -> duplicate (e.g. the same notice
      sha1(type | date | title tokens)       sent twice, or from two senders)
  L4  fuzzy match: same type, title-token -> "Gentle reminder: ..." follow-up
      Jaccard >= 0.5, start within 1 day     mails = duplicate; if the new
      (14 days when the mail says             mail says "rescheduled /
      rescheduled/postponed)                  postponed" = RESCHEDULE

A reminder the user DISMISSED stays in the ledger, so a later duplicate of
the same commitment is never re-created behind the user's back.

Feedback memory
===============
Every approve / edit / dismiss is counted per (sender domain, commitment
type). The engine uses this to auto-trust senders whose reminders the user
keeps approving and to route senders whose reminders the user keeps
dismissing into the review queue instead.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta

import paths

STOP = set("""a an the to of in on for and or with at by from your you is are be this that it re fwd fw
reminder gentle friendly please kindly important urgent update updated new our we will has have""".split())

STATUS_SCHEDULED = "scheduled"        # on the calendar, confirmed
STATUS_REVIEW = "needs_review"        # on the calendar as tentative (or held), waiting for the user
STATUS_DISMISSED = "dismissed"        # user rejected it; never re-create
STATUS_CANCELLED = "cancelled"        # the sender cancelled the commitment
ACTIVE_STATUSES = (STATUS_SCHEDULED, STATUS_REVIEW)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_msg_id      TEXT UNIQUE,
    thread_id         TEXT,
    fingerprint       TEXT,
    event_type        TEXT,
    title             TEXT,
    start             TEXT,
    "end"             TEXT,
    all_day           INTEGER,
    location          TEXT,
    reminders_json    TEXT,
    reminder_rationale TEXT,
    summary           TEXT,
    gmail_link        TEXT,
    sender            TEXT,
    sender_domain     TEXT,
    subject           TEXT,
    priority_band     TEXT,
    category          TEXT,
    confidence        REAL,
    status            TEXT,
    calendar_event_id TEXT,
    issues_json       TEXT,
    explanation_json  TEXT,
    history_json      TEXT,
    created_at        TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_rem_thread ON reminders(thread_id);
CREATE INDEX IF NOT EXISTS idx_rem_fp ON reminders(fingerprint);
CREATE TABLE IF NOT EXISTS sender_feedback (
    sender_domain TEXT,
    event_type    TEXT,
    approvals     INTEGER DEFAULT 0,
    edits         INTEGER DEFAULT 0,
    dismissals    INTEGER DEFAULT 0,
    PRIMARY KEY (sender_domain, event_type)
);
CREATE TABLE IF NOT EXISTS seen_messages (
    gmail_msg_id TEXT PRIMARY KEY,
    outcome      TEXT,
    seen_at      TEXT
);
"""

JSON_FIELDS = ("reminders_json", "issues_json", "explanation_json", "history_json")


def title_tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in STOP and len(w) > 1}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def make_fingerprint(event_type: str, start_iso: str, title: str) -> str:
    tokens = sorted(title_tokens(title))[:8]
    raw = f"{event_type}|{start_iso[:10]}|{' '.join(tokens)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def sender_domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    return (m.group(1).lower() if m else (sender or "").lower()).strip(">")


class ReminderStore:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or paths.reminders_db_path()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------
    def _row(self, row):
        if row is None:
            return None
        d = dict(row)
        for f in JSON_FIELDS:
            try:
                d[f[:-5]] = json.loads(d.pop(f) or "null")
            except (TypeError, json.JSONDecodeError):
                d[f[:-5]] = None
        return d

    def get(self, rid: int):
        return self._row(self.conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone())

    def get_by_message(self, msg_id: str):
        return self._row(self.conn.execute("SELECT * FROM reminders WHERE gmail_msg_id=?", (msg_id,)).fetchone())

    def list(self, statuses=None, upcoming_only=False, limit=200):
        q, args = "SELECT * FROM reminders", []
        cond = []
        if statuses:
            cond.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args += list(statuses)
        if upcoming_only:
            cond.append("\"end\" >= ?")
            args.append(datetime.now().isoformat(timespec="minutes"))
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY start ASC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    # ------------------------------------------------------------------
    def insert(self, rec: dict) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        rec = dict(rec)
        rec.setdefault("created_at", now)
        rec["updated_at"] = now
        rec["history"] = rec.get("history") or [{"at": now, "action": "created", "status": rec.get("status")}]
        cols, vals = self._to_columns(rec)
        cur = self.conn.execute(
            f"INSERT INTO reminders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self.conn.commit()
        return cur.lastrowid

    def update(self, rid: int, action: str = "updated", **fields):
        current = self.get(rid)
        if current is None:
            raise KeyError(f"No reminder with id {rid}")
        now = datetime.now().isoformat(timespec="seconds")
        history = current.get("history") or []
        history.append({"at": now, "action": action,
                        "changes": {k: v for k, v in fields.items() if k not in ("history",)}})
        fields["history"] = history
        fields["updated_at"] = now
        cols, vals = self._to_columns(fields)
        sets = ",".join(f"{c}=?" for c in cols)
        self.conn.execute(f"UPDATE reminders SET {sets} WHERE id=?", vals + [rid])
        self.conn.commit()
        return self.get(rid)

    def _to_columns(self, rec: dict):
        cols, vals = [], []
        for k, v in rec.items():
            key = k
            if k in ("reminders", "issues", "explanation", "history"):
                key, v = k + "_json", json.dumps(v, default=str)
            if key == "end":
                key = '"end"'
            cols.append(key)
            vals.append(v)
        return cols, vals

    # ------------------------------------------------------------------
    def mark_seen(self, msg_id: str, outcome: str):
        self.conn.execute("INSERT OR REPLACE INTO seen_messages VALUES (?,?,?)",
                          (msg_id, outcome, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def was_seen(self, msg_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_messages WHERE gmail_msg_id=?", (msg_id,)).fetchone() is not None

    # ------------------------------------------------------------------
    def find_match(self, candidate: dict):
        """Return (match_kind, existing_record) or (None, None).
        match_kind in: same_message, duplicate, reschedule."""
        msg_id = candidate.get("gmail_msg_id")
        if msg_id:
            existing = self.get_by_message(msg_id)
            if existing:
                return "same_message", existing

        start = datetime.fromisoformat(candidate["start"])
        etype = candidate["event_type"]
        says_reschedule = bool(candidate.get("reschedule"))
        new_tokens = title_tokens(candidate.get("title", "")) | title_tokens(candidate.get("subject", ""))

        if candidate.get("thread_id"):
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE thread_id=? AND event_type=? ORDER BY id DESC",
                (candidate["thread_id"], etype)).fetchall()
            for r in rows:
                rec = self._row(r)
                if rec["start"][:16] == candidate["start"][:16] or (
                        rec["all_day"] and rec["start"][:10] == candidate["start"][:10]):
                    return "duplicate", rec
                if says_reschedule:
                    return "reschedule", rec

        fp = candidate.get("fingerprint")
        if fp:
            r = self.conn.execute("SELECT * FROM reminders WHERE fingerprint=? ORDER BY id DESC", (fp,)).fetchone()
            if r:
                return "duplicate", self._row(r)

        window = timedelta(days=14 if says_reschedule else 1)
        lo, hi = (start - window).isoformat(), (start + window).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE event_type=? AND start BETWEEN ? AND ? ORDER BY id DESC",
            (etype, lo, hi)).fetchall()
        best, best_sim = None, 0.0
        for r in rows:
            rec = self._row(r)
            sim = jaccard(new_tokens, title_tokens(rec["title"]) | title_tokens(rec["subject"]))
            if sim > best_sim:
                best, best_sim = rec, sim
        if best and best_sim >= 0.5:
            same_time = abs((datetime.fromisoformat(best["start"]) - start).total_seconds()) < 3600
            if says_reschedule and not same_time:
                return "reschedule", best
            if abs((datetime.fromisoformat(best["start"]) - start).days) <= 1:
                return "duplicate", best
        return None, None

    # ------------------------------------------------------------------
    def record_feedback(self, domain: str, event_type: str, kind: str):
        col = {"approve": "approvals", "edit": "edits", "dismiss": "dismissals"}[kind]
        self.conn.execute(
            "INSERT INTO sender_feedback (sender_domain, event_type) VALUES (?,?) "
            "ON CONFLICT(sender_domain, event_type) DO NOTHING", (domain, event_type))
        self.conn.execute(f"UPDATE sender_feedback SET {col}={col}+1 WHERE sender_domain=? AND event_type=?",
                          (domain, event_type))
        self.conn.commit()

    def sender_trust(self, domain: str, event_type: str) -> dict:
        r = self.conn.execute("SELECT * FROM sender_feedback WHERE sender_domain=? AND event_type=?",
                              (domain, event_type)).fetchone()
        if not r:
            return {"approvals": 0, "edits": 0, "dismissals": 0, "adjustment": 0.0, "force_review": False}
        a, e, d = r["approvals"], r["edits"], r["dismissals"]
        adjustment = 0.0
        if a >= 2 and d == 0:
            adjustment = min(0.15, 0.05 * a)
        if d >= 2 and d > a:
            adjustment = -min(0.3, 0.1 * d)
        return {"approvals": a, "edits": e, "dismissals": d, "adjustment": adjustment,
                "force_review": d >= 2 and d > a}
