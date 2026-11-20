"""
demo.py
-------
Smart Inbox-to-Calendar Assistant - offline end-to-end demo.

Runs the full pipeline on example emails the model has never seen:

    rule engine -> ML model -> explainability -> priority scoring
      -> commitment extraction -> adaptive reminder plan -> review decision

and prints the calendar reminder that WOULD be created for each one. No
Gmail or Google Calendar connection is needed and nothing is written.
"""

from datetime import datetime, timedelta

from pipeline import EmailClassifierPipeline
from reminder_engine import ReminderEngine, describe
from reminder_store import ReminderStore

NOW = datetime.now().replace(second=0, microsecond=0)
D = lambda days: (NOW + timedelta(days=days)).strftime("%d %b %Y")  # noqa: E731

TEST_EMAILS = [
    dict(sender="notifications@linkedin.com", subject="You appeared in 9 searches this week",
         body="See who's been looking at your profile and grow your network."),
    dict(sender="offers@myntra.com", subject="Flat 60% OFF Ends Tonight",
         body="Hurry! Use code SAVE60 to get flat 60% off on all fashion brands, sale ends tonight."),
    dict(sender="alerts@hdfcbank.com", subject="Credit Card Statement",
         body=f"Your statement dated {D(-3)} is ready. Total amount due Rs. 18,450.00. "
              f"Payment due date: {D(12)}. Please pay before the due date to avoid a late fee."),
    dict(sender="win@prize-mail.net", subject="You Have Won!!!",
         body="Congratulations! You have WON a lottery of $1,000,000. Claim before tomorrow!!!"),
    dict(sender="careers@google.com", subject="Interview Invitation - AI Intern",
         body="You have been shortlisted for the AI Intern role. Please join the interview tomorrow at 10 AM "
              "on https://meet.google.com/abc-defg-hij. This is time sensitive."),
    dict(sender="priya.sharma@gmail.com", subject="Dinner this weekend?",
         body="Hey! Are you free for dinner this Saturday? Let me know, would love to catch up."),
    dict(sender="professor@university.edu", subject="Assignment 3 submission",
         body=f"Dear students, please submit Assignment 3 on the portal no later than {D(9)} 11:59 PM. "
              "Late submissions will not be accepted."),
    dict(sender="exam-cell@university.edu", subject="Semester Exam Schedule",
         body="The end-semester examination for Artificial Intelligence will be held on 04/12 "
              "from 10:00 to 13:00. Carry your hall ticket."),
]


def main():
    pipeline = EmailClassifierPipeline()
    engine = ReminderEngine(ReminderStore(":memory:"), None, {})
    print(f"\nSmart Inbox-to-Calendar Assistant - offline demo (model: {pipeline.model_name})\n")
    print("=" * 100)
    for i, email in enumerate(TEST_EMAILS):
        result = pipeline.classify_email(**email)
        print(f"From      : {email['sender']}")
        print(f"Subject   : {email['subject']}")
        print(f"Category  : {result['predicted_category']}   (source: {result['decision_source']})")
        print(f"Priority  : {result['priority_band']}  (score {result['priority_score']}/100)")
        print(f"Why       : {result['explanation_text']}")

        cand = engine.analyse(dict(email, id=f"demo{i}", date=NOW), result)
        if cand.get("is_event"):
            print("Calendar  : WOULD CREATE ->")
            print("    " + describe(cand).replace("\n", "\n    "))
            print(f"    Reminder plan: {cand['reminder_rationale']}")
        else:
            print(f"Calendar  : no reminder ({cand.get('reason')})")
        print("-" * 100)


if __name__ == "__main__":
    main()
