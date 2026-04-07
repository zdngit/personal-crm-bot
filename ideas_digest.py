"""
Weekly Ideas Digest
Sunday evening email with hybrid table layout:
- Single-column ideas table with sub-line showing source
Uses batched sheet updates.
"""
import os
import json
import smtplib
import html as htmllib
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


# --- Styling (matches the other two digests) ---

TABLE_STYLE = "width:100%;border-collapse:collapse;margin:8px 0 20px 0;font-size:13px;"
TD_STYLE = "padding:12px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top;"
SUBLINE_STYLE = "color:#888;font-size:12px;margin-top:4px;display:block;"


def esc(s):
    if s is None:
        return ""
    return htmllib.escape(str(s))


def build_ideas_table(ideas):
    """
    Ideas are free-form text, so single column with sub-line for source.
    No header row - the section heading above is sufficient.
    """
    if not ideas:
        return ""

    rows_html = []
    for r in ideas:
        # Ideas cols: Idea | Created | Source Message | Sent
        idea_text = r[0] if len(r) > 0 else ""
        source = r[2] if len(r) > 2 else ""

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}color:#222;line-height:1.5;">{esc(idea_text)}
            {f'<span style="{SUBLINE_STYLE}">from: {esc(source[:140])}</span>' if source else ''}
          </td>
        </tr>""")

    return f"""
    <table style="{TABLE_STYLE}">
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


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

    table_html = build_ideas_table(pending)

    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:auto;color:#222;padding:20px;">
    <h2 style="color:#111;margin-bottom:8px;">Weekly ideas review</h2>
    <p style="color:#888;font-size:13px;margin-top:0;">{today}</p>

    <p style="color:#555;font-size:14px;">{len(pending)} idea{'s' if len(pending) != 1 else ''} captured this week. Take a few minutes to revisit:</p>

    {table_html}

    <p style="color:#999;font-size:11px;margin-top:40px;border-top:1px solid #eee;padding-top:12px;">Sent by your personal CRM bot. Browse the Ideas tab in your sheet for the full archive.</p>
    </body></html>"""

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
    batch_mark_sent(sheet, indices, "D")
    print("Done.")


if __name__ == "__main__":
    main()
