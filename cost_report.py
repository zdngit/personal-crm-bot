"""
Weekly Cost Report
Reads the Costs tab, generates charts with matplotlib, and emails a
weekly analysis every Sunday evening.

Charts:
  1. Daily spend for the past 7 days (bar chart)
  2. Cost breakdown by category for the past 7 days (pie chart)
  3. Cost by script for the past 7 days (horizontal bar)
  4. Cumulative monthly run-rate (line chart of spend-to-date vs days)
"""
import os
import io
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")  # Headless
import matplotlib.pyplot as plt

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"].strip()
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace("\xa0", "").replace(" ", "").strip()
RECIPIENT = os.environ["DIGEST_RECIPIENT"].strip()

# Matplotlib style
plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#cccccc",
    "axes.labelcolor": "#333333",
    "xtick.color": "#666666",
    "ytick.color": "#666666",
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
})


def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet("Costs")


def load_cost_rows(sheet):
    """Costs: Timestamp | Script | Category | Model | Input | Output | Cost USD"""
    rows = sheet.get_all_values()
    records = []
    for row in rows[1:]:
        if len(row) < 7 or not row[0]:
            continue
        try:
            ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            records.append({
                "timestamp": ts,
                "script": row[1],
                "category": row[2],
                "model": row[3],
                "input_tokens": int(row[4]) if row[4] else 0,
                "output_tokens": int(row[5]) if row[5] else 0,
                "cost": float(row[6]) if row[6] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return records


def fig_to_png_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def chart_daily_spend(records, week_start):
    """Bar chart: cost per day for the past 7 days."""
    by_day = defaultdict(float)
    for r in records:
        day = r["timestamp"].date()
        by_day[day] += r["cost"]

    days = [(week_start + timedelta(days=i)).date() for i in range(7)]
    values = [by_day.get(d, 0.0) for d in days]
    labels = [d.strftime("%a %d") for d in days]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    bars = ax.bar(labels, values, color="#4a7abc", edgecolor="none")
    ax.set_title("Daily API spend (past 7 days)")
    ax.set_ylabel("USD")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for bar, val in zip(bars, values):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, val, f"${val:.2f}",
                    ha="center", va="bottom", fontsize=9, color="#444")
    ax.set_ylim(0, max(values) * 1.2 if any(values) else 1)
    return fig_to_png_bytes(fig)


def chart_category_breakdown(records):
    """Pie chart: cost by category."""
    by_cat = defaultdict(float)
    for r in records:
        by_cat[r["category"] or "uncategorized"] += r["cost"]

    if not by_cat or sum(by_cat.values()) == 0:
        return None

    categories = sorted(by_cat.keys(), key=lambda k: by_cat[k], reverse=True)
    values = [by_cat[c] for c in categories]
    colors = ["#4a7abc", "#6cb04a", "#e8a33d", "#c85450", "#8566a8", "#4aa8b0", "#b07a4a"]

    fig, ax = plt.subplots(figsize=(6, 4.5))
    wedges, texts, autotexts = ax.pie(
        values, labels=categories, colors=colors[:len(categories)],
        autopct=lambda p: f"${p * sum(values) / 100:.2f}\n({p:.0f}%)",
        startangle=90, pctdistance=0.75,
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_color("white")
        t.set_weight("bold")
    for t in texts:
        t.set_fontsize(10)
    ax.set_title("Cost by category (past 7 days)")
    return fig_to_png_bytes(fig)


def chart_by_script(records):
    """Horizontal bar: cost by script."""
    by_script = defaultdict(float)
    for r in records:
        by_script[r["script"] or "unknown"] += r["cost"]

    if not by_script:
        return None

    scripts = sorted(by_script.keys(), key=lambda k: by_script[k])
    values = [by_script[s] for s in scripts]

    fig, ax = plt.subplots(figsize=(8, 2.5 + 0.4 * len(scripts)))
    bars = ax.barh(scripts, values, color="#6cb04a", edgecolor="none")
    ax.set_title("Cost by script (past 7 days)")
    ax.set_xlabel("USD")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for bar, val in zip(bars, values):
        ax.text(val, bar.get_y() + bar.get_height() / 2, f" ${val:.2f}",
                va="center", fontsize=9, color="#444")
    ax.set_xlim(0, max(values) * 1.2 if values else 1)
    return fig_to_png_bytes(fig)


def chart_monthly_runrate(all_records):
    """Cumulative spend for the current calendar month with projection."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_records = [r for r in all_records if r["timestamp"] >= month_start]
    if not month_records:
        return None

    # Days in month so far
    days_so_far = (now - month_start).days + 1
    days_in_month = 30  # Rough; good enough for a projection

    by_day = defaultdict(float)
    for r in month_records:
        day_idx = (r["timestamp"] - month_start).days
        by_day[day_idx] += r["cost"]

    x = list(range(days_so_far))
    cumulative = []
    total = 0.0
    for i in x:
        total += by_day.get(i, 0.0)
        cumulative.append(total)

    # Projection line based on current pace
    if days_so_far > 0 and total > 0:
        projected_eom = total * (days_in_month / days_so_far)
        proj_x = [days_so_far - 1, days_in_month - 1]
        proj_y = [total, projected_eom]
    else:
        projected_eom = 0
        proj_x, proj_y = [], []

    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.plot(x, cumulative, color="#4a7abc", linewidth=2, marker="o", markersize=4)
    if proj_x:
        ax.plot(proj_x, proj_y, color="#4a7abc", linewidth=1.5, linestyle="--", alpha=0.5)
        ax.text(days_in_month - 1, projected_eom, f" ${projected_eom:.2f} proj",
                va="center", fontsize=9, color="#666")
    ax.set_title(f"Month-to-date spend ({now.strftime('%B %Y')})")
    ax.set_xlabel("Day of month")
    ax.set_ylabel("Cumulative USD")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_xlim(-0.5, days_in_month - 0.5)
    return fig_to_png_bytes(fig)


def build_email(records, week_start, week_end, all_records):
    """Build the email with embedded chart images."""
    week_records = [r for r in records if week_start <= r["timestamp"] < week_end]

    week_total = sum(r["cost"] for r in week_records)
    week_calls = len(week_records)
    week_input_tokens = sum(r["input_tokens"] for r in week_records)
    week_output_tokens = sum(r["output_tokens"] for r in week_records)

    # Previous week for comparison
    prev_start = week_start - timedelta(days=7)
    prev_records = [r for r in records if prev_start <= r["timestamp"] < week_start]
    prev_total = sum(r["cost"] for r in prev_records)

    if prev_total > 0:
        wow_change = ((week_total - prev_total) / prev_total) * 100
        wow_text = f"{'+' if wow_change >= 0 else ''}{wow_change:.0f}% vs last week (${prev_total:.2f})"
    else:
        wow_text = "no comparison (first week of data)"

    # Month-to-date
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd_total = sum(r["cost"] for r in all_records if r["timestamp"] >= month_start)

    # Generate charts
    print("Generating charts...")
    chart_daily = chart_daily_spend(week_records, week_start)
    chart_cat = chart_category_breakdown(week_records)
    chart_scripts = chart_by_script(week_records)
    chart_month = chart_monthly_runrate(all_records)

    # Build HTML with inline image references
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:720px;margin:auto;color:#222;">
    <h2 style="color:#111;">Weekly cost report - {week_end.strftime('%B %d, %Y')}</h2>

    <div style="background:#f5f7fa;padding:16px 20px;border-radius:8px;margin:16px 0;">
    <div style="font-size:28px;font-weight:600;color:#111;">${week_total:.2f}</div>
    <div style="color:#666;font-size:13px;">past 7 days &middot; {week_calls} API calls &middot; {wow_text}</div>
    <div style="color:#666;font-size:13px;margin-top:4px;">Month-to-date: ${mtd_total:.2f}</div>
    </div>

    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Daily spend</h3>
    <img src="cid:chart_daily" style="width:100%;max-width:680px;">

    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Where it went</h3>
    <table width="100%"><tr>
      <td width="50%" style="vertical-align:top;"><img src="cid:chart_cat" style="width:100%;"></td>
      <td width="50%" style="vertical-align:top;"><img src="cid:chart_scripts" style="width:100%;"></td>
    </tr></table>

    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Month-to-date</h3>
    <img src="cid:chart_month" style="width:100%;max-width:680px;">

    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">Token usage</h3>
    <table style="font-size:13px;color:#444;border-collapse:collapse;width:100%;">
      <tr><td style="padding:4px 0;">Input tokens</td><td style="text-align:right;">{week_input_tokens:,}</td></tr>
      <tr><td style="padding:4px 0;">Output tokens</td><td style="text-align:right;">{week_output_tokens:,}</td></tr>
      <tr><td style="padding:4px 0;">Total tokens</td><td style="text-align:right;">{week_input_tokens + week_output_tokens:,}</td></tr>
    </table>

    <p style="color:#999;font-size:11px;margin-top:30px;">Sent by your personal CRM bot. Raw data in the Costs tab of your sheet.</p>
    </body></html>
    """

    text = f"""Weekly cost report - {week_end.strftime('%B %d, %Y')}

Past 7 days: ${week_total:.2f}
API calls: {week_calls}
{wow_text}

Month-to-date: ${mtd_total:.2f}

Token usage:
  Input: {week_input_tokens:,}
  Output: {week_output_tokens:,}
  Total: {week_input_tokens + week_output_tokens:,}

Raw data in the Costs tab of your sheet.
"""

    msg = EmailMessage()
    msg["Subject"] = f"Cost report - week of {week_end.strftime('%b %d')} (${week_total:.2f})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    # Attach charts inline
    html_part = msg.get_payload()[1]
    if chart_daily:
        html_part.add_related(chart_daily, maintype="image", subtype="png", cid="chart_daily")
    if chart_cat:
        html_part.add_related(chart_cat, maintype="image", subtype="png", cid="chart_cat")
    if chart_scripts:
        html_part.add_related(chart_scripts, maintype="image", subtype="png", cid="chart_scripts")
    if chart_month:
        html_part.add_related(chart_month, maintype="image", subtype="png", cid="chart_month")

    return msg


def main():
    sheet = get_sheet()
    records = load_cost_rows(sheet)

    if not records:
        print("No cost data yet. Skipping report.")
        return

    now = datetime.now(timezone.utc)
    week_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    week_start = (week_end - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

    msg = build_email(records, week_start, week_end, records)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print("Sent weekly cost report.")


if __name__ == "__main__":
    main()
