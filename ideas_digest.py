"""
Weekly Ideas Digest
Sunday evening email with ideas captured since the last Sunday digest.
Uses batched sheet updates.
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
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"].strip()
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace("\xa0", "").replace(" ", "").strip()
RECIPIENT = os.environ["DIGEST_RECIPIENT"].strip()


def get_ideas_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet("Ideas")


def batch_mark_sent(sheet, row_indices, col_letter):
    if not row_indices:
        return
    updates = [
        {"range": f"{col_letter}{i}", "values": [["TRUE"]]}
        for i in row_indices
    ]
    sheet.batch_update(updates, value_input_option="USER_ENTERED")


def main():
    sheet = get_ideas_sheet()
    rows = sheet.get_all_values()
    if len(rows) < 2:
        print("No ideas yet.")
        return

    # Ideas: Idea | Created | Source Message | Sent in Digest
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4 or row[3].strip().upper() != "TRUE":
            pending.append(row)
            indices.append(i)

    if not pending:
        print("No new ideas this week.")
        return

    today = datetime.now().strftime("%A, %B %d")

    items_html = "\n".join(
        f'<li style="margin-bottom:14px;">{r[0]}'
        f'<br><span style="color:#999;font-size:11px;">From: {r[2][:140]}</span></li>'
        for r in pending
    )
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;margin:auto;color:#222;">
    <h2 style="color:#111;">Weekly ideas review - {today}</h2>
    <p style="color:#666;">{len(pending)} idea{'s' if len(pending) != 1 else ''} captured this week. Take a few minutes to revisit:</p>
    <ul style="padding-left:20px;">{items_html}</ul>
    <p style="color:#999;font-size:11px;margin-top:30px;">Sent by your personal CRM bot.</p>
    </body></html>
    """

    text = f"Weekly ideas review - {today}\n\n"
    text += f"{len(pending)} idea{'s' if len(pending) != 1 else ''} captured this week:\n\n"
    for r in pending:
        text += f"- {r[0]}\n"

    msg = EmailMessage()
    msg["Subject"] = f"Weekly ideas review - {today} ({len(pending)} idea{'s' if len(pending) != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent ideas digest with {len(pending)} ideas.")

    print("Marking rows as sent...")
    batch_mark_sent(sheet, indices, "D")  # Ideas col 4 = D
    print("Done.")


if __name__ == "__main__":
    main()
