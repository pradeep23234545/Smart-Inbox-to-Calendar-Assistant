# Smart Inbox-to-Calendar Assistant

**Never miss a commitment that arrives by email.**

Smart Inbox-to-Calendar Assistant is a pure-Python, autonomous background
agent for Gmail. It reads new mail, works out which emails contain a
**time-bound commitment** (a meeting, interview, appointment, exam, payment
due date, submission deadline or event), pulls out the event details, and
puts them on your **Google Calendar** with reminders timed for that kind of
commitment, a short summary of the email and a link back to it. It also
keeps the original smart-labelling engine: each email gets a category, a
priority and a plain-language explanation.

```
                ┌──────────────────────────── Smart Inbox-to-Calendar Assistant ────────────────────────────┐
 New Gmail ───► │ Classify (rules → ML) ─► Priority score ─► Commitment extraction ─► Adaptive reminder plan │
 message        │                                                     │                                    │
                │                     Ledger: duplicate / thread / reschedule / cancel check               │
                │                                                     │                                    │
                │              confident? ── yes ─► Google Calendar event (confirmed)                      │
                │                        └── no ──► "[Review]" tentative event + review queue              │
                └───────────────────────────────────────────────────────────────────────────────────────────┘
                   Gmail labels: AI/<Category>, AI/Priority-<Band>, AI/Reminder-Set, AI/Reminder-Review
```

## 1. Problem

Important commitments get buried in email: an interview slot in a recruiter's
third paragraph, a credit-card due date under a statement date, an exam
timetable written as `04/12`, a meeting quietly moved in a reply. Calendar
apps can only add events from structured invitations. Mail that is just
written in plain language, which covers most bills, college notices, HR
mail and personal mail, never reaches the calendar unless the user copies
it by hand.

## 2. What the assistant does

| # | Capability | Where |
|---|---|---|
| 1 | Classifies mail into 6 categories (Primary, Social, Promotions, Spam, **Finance_Bills**, **Job_Academic**) with an explanation | `pipeline.py`, `rule_filter.py`, `classifiers.py`, `explainability.py` |
| 2 | Scores **priority** (Low/Medium/High/Critical) independent of category | `priority_model.py` |
| 3 | **Detects time-bound commitments** and their type: interview, meeting, appointment, exam, payment due, submission deadline, event | `event_extractor.py` |
| 4 | **Extracts date, time, duration, time zone, location / meeting link, amount**, and resolves relative dates ("tomorrow", "next Friday", "within 5 working days") against the time the email was **received** | `event_extractor.py` |
| 5 | **Reasons about ambiguity**: DD/MM vs MM/DD, missing year, weekday-date conflicts, "next Friday", missing AM/PM, missing time, vague dates, competing dates. Each issue is typed and graded by severity | `event_extractor.py` |
| 6 | **Adaptive reminder lead times** by commitment type, urgency and available lead time, kept out of quiet hours | `reminder_planner.py` |
| 7 | Creates the **Google Calendar event** with custom notifications, an urgency colour, the email summary, the reasoning, and a **Gmail link** (as a clickable source chip and in the description) | `calendar_client.py`, `email_summarizer.py` |
| 8 | **Four-layer duplicate detection**, plus **reschedule and cancellation** handling that updates the existing event | `reminder_store.py`, `reminder_engine.py` |
| 9 | **Confidence-gated autonomy**: confident extractions are scheduled; uncertain ones become `[Review]` tentative events in a review queue | `reminder_engine.py` |
| 10 | **Review, edit, dismiss, restore**, from the command line or the desktop app. Dismissed reminders are never re-created | `review.py`, `gui_app.py` |
| 11 | **Learns from your review decisions** per sender and commitment type: senders you keep approving get more autonomy, and senders you keep dismissing go to review | `reminder_store.py` |
| 12 | Runs **unattended** on a schedule; dry-run by default; every action logged | `agent.py`, installers |

## 3. Project structure

```
src/
├── agent.py               # >>> the background assistant - this is what you run <<<
├── review.py              # review / approve / edit / dismiss / restore reminders (CLI)
├── gui_app.py             # optional desktop app (incl. "Calendar Reminders" review tab)
│
├── event_extractor.py     # temporal commitment extraction + ambiguity reasoning
├── reminder_planner.py    # adaptive reminder lead-time planner
├── email_summarizer.py    # commitment-focused extractive summary
├── reminder_store.py      # SQLite ledger, 4-layer dedup, sender feedback memory
├── reminder_engine.py     # decision engine: gate → extract → plan → dedup → decide → act
├── calendar_client.py     # Google Calendar API wrapper
├── google_auth.py         # shared OAuth (gmail.modify + calendar.events), safe scope upgrade
├── gmail_client.py        # Gmail API wrapper (fetch, labels, deep links)
│
├── pipeline.py            # rules + ML + explainability + priority
├── rule_filter.py         # rule-based reasoning layer
├── classifiers.py         # trains & compares Naive Bayes / Linear SVM / Decision Tree
├── explainability.py      # why a category was predicted
├── priority_model.py      # transparent priority scoring
├── feedback.py            # learns from category corrections
├── preprocessing.py, generate_dataset.py, bootstrap.py, paths.py
├── demo.py                # offline demo, no Google account needed
└── main_live.py           # manual one-shot labelling run
tests/test_reminders.py    # 29 offline tests (fake calendar)
```

Runtime data (models, dataset, OAuth token, `reminders.db`, config, logs)
lives in a per-user folder, never in the project:

```
Windows      C:\Users\<you>\.smart_inbox_calendar_assistant\
macOS/Linux  ~/.smart_inbox_calendar_assistant/
```

If you used an earlier build of this project, its data folder is **copied**
here automatically on first run (the old folder is left untouched).

## 4. One-time setup

```bash
pip install -r requirements.txt
```

1. In **Google Cloud Console**, create a project (or reuse your existing one).
2. Enable the **Gmail API** and the **Google Calendar API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Desktop app**. Download the JSON, rename it `credentials.json` and put it in
   the data folder shown above. If you already had one, it was copied over.
4. If the OAuth consent screen is in *Testing* mode, add your Gmail address as
   a test user, and add the `.../auth/calendar.events` scope to the consent screen.
5. Grant access once (a browser window opens):
   ```bash
   python src/agent.py --authorize
   ```
   Only two scopes are requested: `gmail.modify` (read + label, never send or
   delete) and `calendar.events` (manage events only, with no access to
   calendar settings or sharing).

## 5. Try it

```bash
python src/demo.py                         # offline: classification + the reminder each email would get
python src/review.py try "Interview Invitation" "Please join the interview tomorrow at 10 AM IST."
python -m unittest discover -s tests -v    # 29 offline tests
python src/agent.py --once                 # live, DRY-RUN: logs what it would label/schedule
python src/agent.py --scan-reminders --days 14   # dry-run look-back over the last 2 weeks
```

When the log looks right, turn on real changes (saved permanently):

```bash
python src/agent.py --once --enable
```

## 6. How a reminder is decided

1. **Gate**: Spam and Promotions are never turned into reminders. Social mail is used only when it shows strong meeting or event evidence.
2. **Clean**: quoted reply history, forwarded headers and footers are removed, so old dates in a thread are ignored.
3. **Find** every date expression (absolute, numeric, relative, weekday) and every time expression (12h/24h, ranges, noon/midnight, time-zone suffixes), then **bind** each time to the nearest date in the same clause.
4. **Score** each candidate by the cue words just before it (*due, deadline, scheduled, by, before, no later than…*), negative context (*dated, debited on, ordered on, ago…*), whether it appears in the subject, whether it has a time, and whether it is in the future.
5. **Type** the commitment with weighted keyword evidence. Subject words count double, and words near the chosen date count again.
6. **Grade ambiguity**: each issue is `minor`, `major` or `blocking`. Major and blocking issues lower confidence and send the reminder to review.
7. **Plan reminders**:

   | Type | Reminders (timed event) | All-day (no time given) |
   |---|---|---|
   | Interview | prep (25% of lead, 1–3 days) · 1 day · 2 h | 2 days & 1 day before, 09:00 |
   | Exam | prep (30% of lead, 2–7 days) · 1 day · 2 h | 7, 2, 1 days before |
   | Payment due | prep (20%, 3–5 days) · 1 day · 3 h | 3 & 1 days before (+1 week if High/Critical) |
   | Submission deadline | prep (30%, 1–10 days) · 1 day · 3 h | 5, 2, 1 days before |
   | Meeting | 1 day · 15 min | 1 day before |
   | Appointment | 1 day · 1 h | 1 day before |
   | Event | prep (10%) · 1 h | 1 day before |

   The early "prep" reminder is scaled by urgency (Critical ×1.5, High ×1.25,
   Low ×0.75). Reminders that would fire between 22:00 and 07:00 move to
   21:00, reminders already in the past are dropped, and Critical items also
   get an email notification.
8. **Deduplicate**: L1 same message; L2 same Gmail thread (a new date plus wording like "rescheduled" means a reschedule); L3 semantic fingerprint; L4 fuzzy title match within ±1 day, widened to ±14 days for reschedules. Cancellations mark the event `[Cancelled]`.
9. **Decide**: confidence ≥ `auto_create_threshold` (0.70) with no major issues gives a confirmed event. Anything else gets a `[Review]` tentative event, or with `review_policy: "hold"` it stays in the queue only.

## 7. Review, edit, dismiss

```bash
python src/review.py list --pending
python src/review.py show 7
python src/review.py approve 7                       # or: approve --all-pending
python src/review.py edit 7 --date 2026-12-05 --time 14:30
python src/review.py edit 7 --all-day --remind 2880,600
python src/review.py dismiss 7                       # removes the calendar event; never re-created
python src/review.py restore 7
python src/review.py --offline dismiss 7             # change the local ledger only
```

The desktop app (`python src/gui_app.py`) has the same actions on its
**Calendar Reminders** tab. Its **Classify Email** tab also previews the
reminder any pasted email would produce.

## 8. Configuration

`agent_config.json` in the data folder:

| Key | Default | Meaning |
|---|---|---|
| `auto_apply` | `false` | dry-run until `--enable` |
| `calendar_reminders` | `true` | `--no-calendar` / `--calendar` toggle |
| `calendar_id` | `"primary"` | which calendar receives events |
| `timezone` | `""` | IANA name (e.g. `Asia/Kolkata`); empty = this computer's zone |
| `date_order` | `"DMY"` | how to read `04/05`: DMY = 4 May, MDY = April 5 |
| `auto_create_threshold` | `0.70` | confidence needed to schedule without review |
| `min_event_confidence` | `0.35` | below this nothing is created |
| `review_policy` | `"tentative"` | `"tentative"` = `[Review]` events on calendar; `"hold"` = queue only |
| `skip_categories` | `["Spam","Promotions"]` | never create reminders from these |
| `email_notification_bands` | `["Critical"]` | priority bands that also get an email reminder |
| `max_per_run`, `interval_minutes`, `archive_low_priority` | | as before |

## 9. Running in the background

- **Windows**: double-click `install_windows_task.bat`. It registers the task `SmartInboxCalendarAssistant`, which runs every 15 minutes, and replaces the task left by older versions. Remove it with `schtasks /delete /tn "SmartInboxCalendarAssistant" /f`.
- **macOS/Linux**: `./install_cron.sh`.
- **Any OS**: `python src/agent.py --loop --interval-minutes 15`.

Background runs never open a browser. If Calendar access hasn't been granted
yet, reminders wait in the review queue and the log tells you to run
`--authorize`.

Audit log: `~/.smart_inbox_calendar_assistant/logs/assistant.log`

## 10. Classification engine (unchanged core)

Rules first (sender domains, keyword triggers), then the best of Naive
Bayes, Linear SVM and Decision Tree chosen by macro-F1, then a word-level
explanation and a transparent priority score. On the bundled 270-email
synthetic dataset, Naive Bayes reaches about 100% macro-F1, Linear SVM about
98.5% and the Decision Tree about 97%. Run `python src/classifiers.py` to
reproduce this.

## 11. Privacy and safety

- All analysis runs **locally**. Email text is never sent to any third-party AI service.
- Least-privilege scopes: `gmail.modify` + `calendar.events`. No send, no delete of mail.
- Dry-run by default; one explicit `--enable` to act.
- Uncertain extractions are **never silently guessed**. They are marked `[Review]` with the reason shown.
- Every created event links back to its source email, and every decision is explained and logged.
- Dismissals are respected permanently, so the assistant never re-adds what you removed.
