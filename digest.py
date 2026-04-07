"""
Daily Digest
Reads links from the Google Sheet that haven't been sent yet,
emails them as a morning reading list, marks them as sent.
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

def get_links_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet("Links")

def main():
    sheet = get_links_sheet()
    rows = sheet.get_all_values()
    if len(rows) < 2:
        print("No links yet.")
        return

    # Columns: URL | Title | Captured At | Source Message | Sent in Digest
    pending = []
    pending_row_indices = []
    for i, row in enumerate(rows[1:], start=2):  # row 1 is header
        if len(row) < 5 or row[4].strip().upper() != "TRUE":
            pending.append(row)
            pending_row_indices.append(i)

    if not pending:
        print("Nothing new to send.")
        return

    # Build email
    today = datetime.now().strftime("%A, %B %d")
    html_items = "\n".join(
        f'<li><a href="{r[0]}">{r[1] or r[0]}</a><br><small style="color:#666">{r[3][:120]}</small></li>'
        for r in pending
    )
    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;max-width:600px;margin:auto;">
    <h2>Your reading list — {today}</h2>
    <p>{len(pending)} link{'s' if len(pending) != 1 else ''} captured since the last digest:</p>
    <ul>{html_items}</ul>
    <p style="color:#999;font-size:12px;">Sent by your personal CRM bot.</p>
    </body></html>
    """

    msg = EmailMessage()
    msg["Subject"] = f"Reading list — {today} ({len(pending)} link{'s' if len(pending) != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(f"Your reading list for {today}:\n\n" + "\n".join(f"- {r[1] or 'Link'}: {r[0]}" for r in pending))
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent digest with {len(pending)} links.")

    # Mark as sent
    for i in pending_row_indices:
        sheet.update_cell(i, 5, "TRUE")
    print("Marked rows as sent.")

if __name__ == "__main__":
    main()
