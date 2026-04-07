"""
Evening Tasks Digest
Sends an evening email with:
- Tasks captured today (pending)
- Inbox: items Claude was uncertain about and wants you to manually sort
Marks both as sent after delivery.
"""
import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT = os.environ["DIGEST_RECIPIENT"]

def get_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return {
        "tasks": sh.worksheet("Tasks"),
        "inbox": sh.worksheet("Inbox"),
    }

def collect_pending_tasks(sheet):
    """Tasks columns: Task | Created | Due | Status | Source Message"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4 or row[3].strip().lower() == "pending":
            pending.append(row)
            indices.append(i)
    return pending, indices

def collect_pending_inbox(sheet):
    """Inbox columns: Type | Content | Reason | Confidence | Source Message | Created | Status"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 7 or row[6].strip().lower() == "pending":
            pending.append(row)
            indices.append(i)
    return pending, indices

def build_html(today, tasks, inbox):
    sections = []

    if tasks:
        task_html = "\n".join(
            f'<li>{r[0]}'
            + (f' <span style="color:#c00;font-size:12px;">(due {r[2]})</span>' if len(r) > 2 and r[2] else '')
            + '</li>'
            for r in tasks
        )
        sections.append(f"""
        <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">To-do ({len(tasks)})</h3>
        <ul style="padding-left:20px;">{task_html}</ul>
        """)

    if inbox:
        inbox_html = "\n".join(
            f'<li><span style="color:#888;font-size:11px;text-transform:uppercase;">{r[0]}</span> '
            f'{r[1]}'
            f'<br><span style="color:#999;font-size:11px;">From: {r[4][:120]}</span></li>'
            for r in inbox
        )
        sections.append(f"""
        <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Needs your input ({len(inbox)})</h3>
        <p style="color:#666;font-size:13px;">Claude wasn't sure how to classify these. Open the Inbox tab in your sheet to sort them.</p>
        <ul style="padding-left:20px;">{inbox_html}</ul>
        """)

    body = "\n".join(sections) if sections else "<p>Nothing pending tonight.</p>"

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;margin:auto;color:#222;">
    <h2 style="color:#111;">Evening review - {today}</h2>
    {body}
    <p style="color:#999;font-size:11px;margin-top:30px;">Sent by your personal CRM bot. Mark items done in the sheet to remove them from future digests.</p>
    </body></html>
    """

def build_text(today, tasks, inbox):
    parts = [f"Evening review - {today}\n"]
    if tasks:
        parts.append(f"TO-DO ({len(tasks)}):")
        for r in tasks:
            line = f"- {r[0]}"
            if len(r) > 2 and r[2]:
                line += f" (due {r[2]})"
            parts.append(line)
        parts.append("")
    if inbox:
        parts.append(f"NEEDS YOUR INPUT ({len(inbox)}):")
        for r in inbox:
            parts.append(f"- [{r[0]}] {r[1]}")
        parts.append("")
    if not tasks and not inbox:
        parts.append("Nothing pending tonight.")
    return "\n".join(parts)

def main():
    sheets = get_sheets()
    tasks, task_indices = collect_pending_tasks(sheets["tasks"])
    inbox, inbox_indices = collect_pending_inbox(sheets["inbox"])

    if not tasks and not inbox:
        print("Nothing to send.")
        return

    today = datetime.now().strftime("%A, %B %d")
    html = build_html(today, tasks, inbox)
    text = build_text(today, tasks, inbox)

    total = len(tasks) + len(inbox)
    msg = EmailMessage()
    msg["Subject"] = f"Evening review - {today} ({total} item{'s' if total != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent evening digest: {len(tasks)} tasks, {len(inbox)} inbox items.")

    # Mark as sent
    for i in task_indices:
        sheets["tasks"].update_cell(i, 4, "sent")
    for i in inbox_indices:
        sheets["inbox"].update_cell(i, 7, "sent")
    print("Marked rows as sent.")

if __name__ == "__main__":
    main()
