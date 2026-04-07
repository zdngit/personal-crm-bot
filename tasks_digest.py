"""
Evening Tasks Digest
Reads pending tasks from the Google Sheet, emails them as an end-of-day
to-do list, marks them as sent.
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

def get_tasks_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet("Tasks")

def main():
    sheet = get_tasks_sheet()
    rows = sheet.get_all_values()
    if len(rows) < 2:
        print("No tasks yet.")
        return

    # Columns: Task | Created | Due | Status | Source Message
    pending = []
    pending_row_indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4 or row[3].strip().lower() == "pending":
            pending.append(row)
            pending_row_indices.append(i)

    if not pending:
        print("Nothing pending to send.")
        return

    today = datetime.now().strftime("%A, %B %d")

    html_items = "\n".join(
        f'<li>{r[0]}'
        + (f' <span style="color:#c00;font-size:12px;">(due {r[2]})</span>' if len(r) > 2 and r[2] else '')
        + '</li>'
        for r in pending
    )
    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;max-width:600px;margin:auto;">
    <h2>Your to-do list - {today}</h2>
    <p>{len(pending)} task{'s' if len(pending) != 1 else ''} captured today:</p>
    <ul>{html_items}</ul>
    <p style="color:#999;font-size:12px;">Sent by your personal CRM bot. Mark items done in the Tasks sheet to remove them from future digests.</p>
    </body></html>
    """

    text_items = "\n".join(
        f"- {r[0]}" + (f" (due {r[2]})" if len(r) > 2 and r[2] else "")
        for r in pending
    )

    msg = EmailMessage()
    msg["Subject"] = f"To-do list - {today} ({len(pending)} task{'s' if len(pending) != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(f"Your to-do list for {today}:\n\n{text_items}")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent task digest with {len(pending)} tasks.")

    # Mark as sent
    for i in pending_row_indices:
        sheet.update_cell(i, 4, "sent")
    print("Marked rows as sent.")

if __name__ == "__main__":
    main()
