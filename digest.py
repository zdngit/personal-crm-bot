"""
Morning Digest
Consolidated morning email:
- New deals (WhatsApp and Telegram, grouped by source)
- New people added since last digest
- Reading list (unsent links)
Uses batched sheet updates to avoid Google Sheets rate limits.
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
        "deals": sh.worksheet("Deals"),
    }


def collect_pending_links(sheet):
    """Links: URL | Title | Captured At | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 5 or row[4].strip().upper() != "TRUE":
            pending.append(row)
            indices.append(i)
    return pending, indices


def collect_new_people(sheet):
    """People: Name | Context | Type | Notes | First Mentioned | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    new_people = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 7 or row[6].strip().upper() != "TRUE":
            new_people.append(row)
            indices.append(i)
    return new_people, indices


def collect_new_deals(sheet):
    """Deals: Company | Terms | Direction | Timeline | Deal Type | Mentioned By | Source | Captured | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    new_deals = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 10 or row[9].strip().upper() != "TRUE":
            new_deals.append(row)
            indices.append(i)
    return new_deals, indices


def batch_mark_sent(sheet, row_indices, col_letter):
    """
    Mark many rows as sent in a single batched API call.
    col_letter is the column letter of the 'Sent in Digest' column (e.g., 'E', 'G', 'J').
    """
    if not row_indices:
        return
    updates = [
        {"range": f"{col_letter}{i}", "values": [["TRUE"]]}
        for i in row_indices
    ]
    sheet.batch_update(updates, value_input_option="USER_ENTERED")


def build_html(today, links, people, deals):
    sections = []

    if deals:
        wa_deals = [d for d in deals if len(d) > 6 and d[6].lower() == "whatsapp"]
        tg_deals = [d for d in deals if len(d) > 6 and d[6].lower() == "telegram"]

        deal_html_parts = []
        if wa_deals:
            deal_html_parts.append(f'<h4 style="margin:12px 0 6px 0;color:#555;">From WhatsApp ({len(wa_deals)})</h4>')
            deal_html_parts.append("<ul style='padding-left:20px;'>")
            for d in wa_deals:
                deal_html_parts.append(_format_deal(d))
            deal_html_parts.append("</ul>")
        if tg_deals:
            deal_html_parts.append(f'<h4 style="margin:12px 0 6px 0;color:#555;">From Telegram notes ({len(tg_deals)})</h4>')
            deal_html_parts.append("<ul style='padding-left:20px;'>")
            for d in tg_deals:
                deal_html_parts.append(_format_deal(d))
            deal_html_parts.append("</ul>")

        sections.append(f"""
        <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Deals ({len(deals)})</h3>
        {''.join(deal_html_parts)}
        """)

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

    body = "\n".join(sections) if sections else "<p>Nothing new this morning.</p>"

    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;margin:auto;color:#222;">
    <h2 style="color:#111;">Morning brief - {today}</h2>
    {body}
    <p style="color:#999;font-size:11px;margin-top:30px;">Sent by your personal CRM bot.</p>
    </body></html>
    """


def _format_deal(d):
    company = d[0] if len(d) > 0 else ""
    terms = d[1] if len(d) > 1 else ""
    direction = d[2] if len(d) > 2 else ""
    timeline = d[3] if len(d) > 3 else ""
    deal_type = d[4] if len(d) > 4 else ""
    mentioned_by = d[5] if len(d) > 5 else ""

    parts = [f'<strong>{company}</strong>']
    meta = []
    if deal_type and deal_type != "unknown":
        meta.append(deal_type)
    if direction:
        meta.append(direction)
    if meta:
        parts.append(f' <span style="color:#666;font-size:12px;">[{", ".join(meta)}]</span>')

    details = []
    if terms:
        details.append(terms)
    if timeline:
        details.append(f"timeline: {timeline}")
    if mentioned_by:
        details.append(f"via {mentioned_by}")
    if details:
        parts.append(f'<br><span style="color:#444;font-size:13px;">{" &middot; ".join(details)}</span>')

    return f'<li style="margin-bottom:8px;">{"".join(parts)}</li>'


def build_text(today, links, people, deals):
    parts = [f"Morning brief - {today}\n"]
    if deals:
        parts.append(f"DEALS ({len(deals)}):")
        for d in deals:
            line = f"- {d[0]}"
            if len(d) > 4 and d[4] and d[4] != "unknown":
                line += f" [{d[4]}]"
            if len(d) > 1 and d[1]:
                line += f" - {d[1]}"
            if len(d) > 5 and d[5]:
                line += f" via {d[5]}"
            if len(d) > 6 and d[6]:
                line += f" ({d[6]})"
            parts.append(line)
        parts.append("")
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
    if not people and not links and not deals:
        parts.append("Nothing new this morning.")
    return "\n".join(parts)


def main():
    sheets = get_sheets()
    links, link_indices = collect_pending_links(sheets["links"])
    people, people_indices = collect_new_people(sheets["people"])
    deals, deal_indices = collect_new_deals(sheets["deals"])

    if not links and not people and not deals:
        print("Nothing to send.")
        return

    today = datetime.now().strftime("%A, %B %d")
    html = build_html(today, links, people, deals)
    text = build_text(today, links, people, deals)

    total = len(links) + len(people) + len(deals)
    msg = EmailMessage()
    msg["Subject"] = f"Morning brief - {today} ({total} item{'s' if total != 1 else ''})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent morning digest: {len(deals)} deals, {len(people)} people, {len(links)} links.")

    # Batched mark-as-sent - single API call per sheet, regardless of row count
    print("Marking rows as sent...")
    batch_mark_sent(sheets["links"], link_indices, "E")       # Links col 5 = E
    batch_mark_sent(sheets["people"], people_indices, "G")    # People col 7 = G
    batch_mark_sent(sheets["deals"], deal_indices, "J")       # Deals col 10 = J
    print("Done.")


if __name__ == "__main__":
    main()
