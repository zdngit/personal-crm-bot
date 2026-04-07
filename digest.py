"""
Morning Digest
Consolidated morning email with hybrid table layout:
- Deals (Company | Type | Terms | Mentioned By columns, sub-line for timeline/direction)
- New people (Name | Type columns, sub-line for context)
- Reading list (Title column linked to URL, sub-line for summary)
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
    """Links: URL | Title | Summary | Captured At | Source Message | Sent in Digest"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 6 or row[5].strip().upper() != "TRUE":
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
    if not row_indices:
        return
    updates = [
        {"range": f"{col_letter}{i}", "values": [["TRUE"]]}
        for i in row_indices
    ]
    sheet.batch_update(updates, value_input_option="USER_ENTERED")


# --- HTML building blocks ---

# Styles used across all tables for consistency
TABLE_STYLE = (
    "width:100%;border-collapse:collapse;margin:8px 0 20px 0;"
    "font-size:13px;"
)
TH_STYLE = (
    "text-align:left;padding:8px 10px;background:#f5f7fa;"
    "color:#555;font-weight:600;font-size:11px;text-transform:uppercase;"
    "letter-spacing:0.5px;border-bottom:1px solid #e0e4ea;"
)
TD_STYLE = (
    "padding:10px;border-bottom:1px solid #f0f0f0;vertical-align:top;"
)
SUBLINE_STYLE = (
    "color:#888;font-size:12px;margin-top:3px;display:block;"
)


def esc(s):
    """HTML-escape a value, returning empty string for None."""
    if s is None:
        return ""
    return htmllib.escape(str(s))


def build_deals_table(deals):
    """
    Deals columns shown: Company | Type | Terms | Mentioned By
    Sub-line per row: direction + timeline + source
    """
    if not deals:
        return ""

    rows_html = []
    for d in deals:
        # Deals cols: Company | Terms | Direction | Timeline | Deal Type | Mentioned By | Source | Captured | Source Message | Sent
        company = d[0] if len(d) > 0 else ""
        terms = d[1] if len(d) > 1 else ""
        direction = d[2] if len(d) > 2 else ""
        timeline = d[3] if len(d) > 3 else ""
        deal_type = d[4] if len(d) > 4 else ""
        mentioned_by = d[5] if len(d) > 5 else ""
        source = d[6] if len(d) > 6 else ""

        subline_parts = []
        if direction:
            subline_parts.append(esc(direction))
        if timeline:
            subline_parts.append(f"timeline: {esc(timeline)}")
        if source:
            subline_parts.append(f"via {esc(source)}")
        subline = " &middot; ".join(subline_parts)

        dt_display = deal_type if deal_type and deal_type != "unknown" else "-"

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}font-weight:600;color:#111;">{esc(company)}</td>
          <td style="{TD_STYLE}color:#555;">{esc(dt_display)}</td>
          <td style="{TD_STYLE}color:#333;">{esc(terms) or "-"}</td>
          <td style="{TD_STYLE}color:#555;">{esc(mentioned_by) or "-"}
            {f'<span style="{SUBLINE_STYLE}">{subline}</span>' if subline else ''}
          </td>
        </tr>""")

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px;">Deals ({len(deals)})</h3>
    <table style="{TABLE_STYLE}">
      <thead>
        <tr>
          <th style="{TH_STYLE}">Company</th>
          <th style="{TH_STYLE}">Type</th>
          <th style="{TH_STYLE}">Terms</th>
          <th style="{TH_STYLE}">Source</th>
        </tr>
      </thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_people_table(people):
    """
    People columns shown: Name | Type
    Sub-line per row: context
    """
    if not people:
        return ""

    rows_html = []
    for p in people:
        # People cols: Name | Context | Type | Notes | First Mentioned | Source Message | Sent
        name = p[0] if len(p) > 0 else ""
        context = p[1] if len(p) > 1 else ""
        ptype = p[2] if len(p) > 2 else ""

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}font-weight:600;color:#111;width:30%;">{esc(name)}</td>
          <td style="{TD_STYLE}color:#555;">{esc(ptype) or "-"}
            {f'<span style="{SUBLINE_STYLE}">{esc(context)}</span>' if context else ''}
          </td>
        </tr>""")

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px;">New people ({len(people)})</h3>
    <table style="{TABLE_STYLE}">
      <thead>
        <tr>
          <th style="{TH_STYLE}">Name</th>
          <th style="{TH_STYLE}">Type &amp; context</th>
        </tr>
      </thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_links_table(links):
    """
    Links columns shown: Title (linked)
    Sub-line per row: summary
    Single column because links work best with full-width titles.
    """
    if not links:
        return ""

    rows_html = []
    for r in links:
        # Links cols: URL | Title | Summary | Captured At | Source Message | Sent
        url = r[0] if len(r) > 0 else ""
        title = r[1] if len(r) > 1 else ""
        summary = r[2] if len(r) > 2 else ""

        display_title = title if title else url

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}">
            <a href="{esc(url)}" style="color:#0066cc;font-weight:500;text-decoration:none;">{esc(display_title)}</a>
            {f'<span style="{SUBLINE_STYLE}">{esc(summary)}</span>' if summary else ''}
          </td>
        </tr>""")

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px;">Reading list ({len(links)})</h3>
    <table style="{TABLE_STYLE}">
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_html(today, links, people, deals):
    sections = []
    if deals:
        sections.append(build_deals_table(deals))
    if people:
        sections.append(build_people_table(people))
    if links:
        sections.append(build_links_table(links))

    body = "\n".join(sections) if sections else '<p style="color:#999;">Nothing new this morning.</p>'

    return f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:auto;color:#222;padding:20px;">
    <h2 style="color:#111;margin-bottom:8px;">Morning brief</h2>
    <p style="color:#888;font-size:13px;margin-top:0;">{today}</p>
    {body}
    <p style="color:#999;font-size:11px;margin-top:40px;border-top:1px solid #eee;padding-top:12px;">Sent by your personal CRM bot.</p>
    </body></html>"""


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
            if len(r) > 2 and r[2]:
                parts.append(f"  {r[2]}")
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

    print("Marking rows as sent...")
    batch_mark_sent(sheets["links"], link_indices, "F")       # Links col 6 = F
    batch_mark_sent(sheets["people"], people_indices, "G")    # People col 7 = G
    batch_mark_sent(sheets["deals"], deal_indices, "J")       # Deals col 10 = J
    print("Done.")


if __name__ == "__main__":
    main()
