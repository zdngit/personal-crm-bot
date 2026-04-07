"""
Evening Tasks Digest
Evening email with hybrid table layout:
- Tasks table (Task | Due columns)
- Inbox table (Type | Content columns, sub-line for source)
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
        "tasks": sh.worksheet("Tasks"),
        "inbox": sh.worksheet("Inbox"),
    }


def collect_pending_tasks(sheet):
    """Tasks: Task | Created | Due | Status | Source Message"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 4 or row[3].strip().lower() == "pending":
            pending.append(row)
            indices.append(i)
    return pending, indices


def collect_pending_inbox(sheet):
    """Inbox: Type | Content | Reason | Confidence | Source Message | Created | Status"""
    rows = sheet.get_all_values()
    pending = []
    indices = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 7 or row[6].strip().lower() == "pending":
            pending.append(row)
            indices.append(i)
    return pending, indices


def batch_mark_status(sheet, row_indices, col_letter, new_value):
    if not row_indices:
        return
    updates = [
        {"range": f"{col_letter}{i}", "values": [[new_value]]}
        for i in row_indices
    ]
    sheet.batch_update(updates, value_input_option="USER_ENTERED")


# --- HTML building blocks (same styling as morning digest) ---

TABLE_STYLE = "width:100%;border-collapse:collapse;margin:8px 0 20px 0;font-size:13px;"
TH_STYLE = (
    "text-align:left;padding:8px 10px;background:#f5f7fa;"
    "color:#555;font-weight:600;font-size:11px;text-transform:uppercase;"
    "letter-spacing:0.5px;border-bottom:1px solid #e0e4ea;"
)
TD_STYLE = "padding:10px;border-bottom:1px solid #f0f0f0;vertical-align:top;"
SUBLINE_STYLE = "color:#888;font-size:12px;margin-top:3px;display:block;"


def esc(s):
    if s is None:
        return ""
    return htmllib.escape(str(s))


def build_tasks_table(tasks):
    """
    Tasks columns: Task | Due
    Simple two-column layout; tasks are usually short so no sub-lines needed.
    """
    if not tasks:
        return ""

    rows_html = []
    for t in tasks:
        task_text = t[0] if len(t) > 0 else ""
        due = t[2] if len(t) > 2 else ""

        due_display = f'<span style="color:#c00;font-weight:500;">{esc(due)}</span>' if due else '<span style="color:#999;">-</span>'

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}color:#222;">{esc(task_text)}</td>
          <td style="{TD_STYLE}white-space:nowrap;">{due_display}</td>
        </tr>""")

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px;">To-do ({len(tasks)})</h3>
    <table style="{TABLE_STYLE}">
      <thead>
        <tr>
          <th style="{TH_STYLE}">Task</th>
          <th style="{TH_STYLE}">Due</th>
        </tr>
      </thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_inbox_table(inbox):
    """
    Inbox columns: Type | Content
    Sub-line per row: source preview (where it came from)
    """
    if not inbox:
        return ""

    rows_html = []
    for r in inbox:
        item_type = r[0] if len(r) > 0 else ""
        content = r[1] if len(r) > 1 else ""
        source = r[4] if len(r) > 4 else ""

        type_display = (
            f'<span style="background:#eef1f5;color:#666;padding:2px 8px;'
            f'border-radius:10px;font-size:11px;text-transform:uppercase;'
            f'letter-spacing:0.5px;">{esc(item_type)}</span>'
        )

        rows_html.append(f"""
        <tr>
          <td style="{TD_STYLE}width:80px;">{type_display}</td>
          <td style="{TD_STYLE}color:#222;">{esc(content)}
            {f'<span style="{SUBLINE_STYLE}">from: {esc(source[:140])}</span>' if source else ''}
          </td>
        </tr>""")

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px;">Needs your input ({len(inbox)})</h3>
    <p style="color:#666;font-size:13px;margin-top:0;">Claude wasn't sure how to classify these. Open the Inbox tab to sort them.</p>
    <table style="{TABLE_STYLE}">
      <thead>
        <tr>
          <th style="{TH_STYLE}">Type</th>
          <th style="{TH_STYLE}">Content</th>
        </tr>
      </thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_html(today, tasks, inbox):
    sections = []
    if tasks:
        sections.append(build_tasks_table(tasks))
    if inbox:
        sections.append(build_inbox_table(inbox))

    body = "\n".join(sections) if sections else '<p style="color:#999;">Nothing pending tonight.</p>'

    return f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:auto;color:#222;padding:20px;">
    <h2 style="color:#111;margin-bottom:8px;">Evening review</h2>
    <p style="color:#888;font-size:13px;margin-top:0;">{today}</p>
    {body}
    <p style="color:#999;font-size:11px;margin-top:40px;border-top:1px solid #eee;padding-top:12px;">Sent by your personal CRM bot. Mark items done in the sheet to remove them from future digests.</p>
    </body></html>"""


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

    print("Marking rows as sent...")
    batch_mark_status(sheets["tasks"], task_indices, "D", "sent")
    batch_mark_status(sheets["inbox"], inbox_indices, "G", "sent")
    print("Done.")


if __name__ == "__main__":
    main()
