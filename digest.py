"""
Morning Digest
Sends a consolidated morning email with:
- Links to read (unsent)
- New people added since last morning digest
- Upcoming reminders (next 7 days) - placeholder for future Reminders tab
Marks links and people as sent after delivery.
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
        "people": sh.worksheet("People"),
        "links": sh.worksheet("Links"),
    }

def collect_pending_links(sheet):
    """Links columns: URL | Title | Captured At | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 5 or row[4].strip().upper() != "TRUE":
            pending.append(row)
            indices.append(i)
    return pending, indices

def collect_new_people(sheet):
    """People columns: Name | Context | Type | Notes | First Mentioned | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    new_people = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 7 or row[6].strip().upper() != "TRUE":
            new_people.append(row)
            indices.append(i)
    return new_people, indices

def build_html(today, links, people):
    sections = []

    if people:
        people_html = "\n".join(
            f'<li><strong>{p[0]}</strong>'
            + (f' <span style="color:#666;font-size:12px;">[{p[2]}]</span>' if len(p) > 2 and p[2] else '')
            + (f'<br><span style="color:#444;font-size:13px;">{p[1]}</span>' if len(p) > 1 and p[1] else '')
            + '</li>'
            for p in people
        )
        sections.append(f"""
        <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">New people ({len(people)})</h3>
        <ul style="padding-left:20px;">{people_html}</ul>
        """)

    if links:
        links_html = "\n".join(
            f'<li><a href="{r[0]}" style="color:#0066cc;">{r[1] or r[0]}</a>'
            + (f'<br><span style="color:#666;font-size:12px;">{r[3][:120]}</span>' if len(r) > 3 and r[3] else '')
            + '</li>'
            for r in links
        )
        sections.append(f"""
        <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Reading list ({len(links)})</h3>
        <ul style="padding-left:20px;">{links_html}</ul>
        """)

    sections.append("""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;color:#999;">Reminders</h3>
    <p style="color:#999;font-size:13px;">No reminders set up yet. Add a Reminders tab when ready.</p>
    """)

    body = "\n".join(sections) if sections else "<p>Nothing new this morning.</p>"

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;margin:auto;color:#222;">
    <h2 style="color:#111;">Morning brief - {today}</h2>
    {body}
    <p style="color:#999;font-size:11px;margin-top:30px;">Sent by your personal CRM bot.</p>
    </body></html>
    """

def build_text(today, links, people):
    parts = [f"Morning brief - {today}\n"]
    if people:
        parts.append(f"NEW PEOPLE ({len(people)}):")
        for p in people:
            line = f"- {p[0]}"
            if len(p) > 2 and p[2]:
                line += f" [{p[2]}]"
            if len(p) > 1 and p[1]:
                line += f": {p[1]}"
            parts.append(line)
        parts.append("")
    if links:
        parts.append(f"READING LIST ({len(links)}):")
        for r in links:
            parts.append(f"- {r[1] or 'Link'}: {r[0]}")
        parts.append("")
    if not people and not links:
        parts.append("Nothing new this morning.")
    return "\n".join(parts)

def main():
    sheets = get_sheets()
    links, link_indices = collect_pending_links(sheets["links"])
    people, people_indices = collect_new_people(sheets["people"])

    if not links and not people:
        print("Nothing to send.")
        return

    today = datetime.now().strftime("%A, %B %d")
    html = build_html(today, links, people)
    text = build_text(today, links, people)

    total = len(links) + len(people)
    msg = EmailMessage()
    msg["Subject"] = f"Morning brief - {today} ({total} item{'s' if total != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent morning digest: {len(people)} people, {len(links)} links.")

    # Mark as sent
    for i in link_indices:
        sheets["links"].update_cell(i, 5, "TRUE")
    for i in people_indices:
        sheets["people"].update_cell(i, 7, "TRUE")
    print("Marked rows as sent.")

if __name__ == "__main__":
    main()
