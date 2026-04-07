"""
Health Check
Runs weekly (Sunday) and produces a status report covering:
  - Python version against end-of-life schedule
  - Python library versions against latest PyPI releases
  - GitHub Actions versions
  - Anthropic model availability (minimal API call)
  - Silent-failure detection: recent activity in the Costs, People, Deals tabs

Exports:
  run_health_checks() -> list of dicts with fields:
    {name, status, message, details}
  status is one of: "ok", "warning", "critical"
  
  build_health_html(checks) -> HTML string for inclusion in the cost report email
  build_health_text(checks) -> plain text version
"""
import os
import sys
import json
import re
import httpx
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Libraries we care about. Keys are PyPI names, values are what we currently pin to in requirements.
TRACKED_LIBRARIES = ["telethon", "gspread", "google-auth", "anthropic", "httpx", "matplotlib"]

# Python EOL dates (major.minor -> EOL date). Source: devguide.python.org/versions/
PYTHON_EOL = {
    "3.9": datetime(2025, 10, 31, tzinfo=timezone.utc),
    "3.10": datetime(2026, 10, 31, tzinfo=timezone.utc),
    "3.11": datetime(2027, 10, 31, tzinfo=timezone.utc),
    "3.12": datetime(2028, 10, 31, tzinfo=timezone.utc),
    "3.13": datetime(2029, 10, 31, tzinfo=timezone.utc),
}

# Anthropic model we expect to work
EXPECTED_MODEL = "claude-opus-4-5"


def get_costs_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON) if not os.path.isfile(SA_JSON) else json.loads(open(SA_JSON).read()),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return {
        "costs": sh.worksheet("Costs"),
        "people": sh.worksheet("People"),
        "deals": sh.worksheet("Deals"),
    }


# --- Individual checks ---

def check_python_version():
    major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    eol = PYTHON_EOL.get(major_minor)
    now = datetime.now(timezone.utc)

    if not eol:
        return {
            "name": "Python version",
            "status": "warning",
            "message": f"Python {major_minor} (unknown EOL status)",
            "details": "Not in our EOL tracking table - update the script.",
        }

    days_until_eol = (eol - now).days
    if days_until_eol < 0:
        return {
            "name": "Python version",
            "status": "critical",
            "message": f"Python {major_minor} is {-days_until_eol} days past EOL",
            "details": f"Upgrade to a supported version. Latest stable: 3.13",
        }
    elif days_until_eol < 90:
        return {
            "name": "Python version",
            "status": "warning",
            "message": f"Python {major_minor} EOL in {days_until_eol} days",
            "details": f"Plan upgrade before {eol.strftime('%b %d, %Y')}",
        }
    else:
        return {
            "name": "Python version",
            "status": "ok",
            "message": f"Python {major_minor} (EOL in {days_until_eol} days)",
            "details": "",
        }


def _get_installed_version(pkg_name):
    """Get the currently installed version of a package."""
    try:
        # importlib.metadata is in stdlib since 3.8
        from importlib.metadata import version as get_version, PackageNotFoundError
        try:
            return get_version(pkg_name)
        except PackageNotFoundError:
            return None
    except ImportError:
        return None


def _get_pypi_latest(pkg_name):
    """Fetch the latest version from PyPI."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"https://pypi.org/pypi/{pkg_name}/json")
            if resp.status_code == 200:
                data = resp.json()
                return data["info"]["version"]
    except Exception as e:
        print(f"  PyPI fetch failed for {pkg_name}: {e}")
    return None


def _version_tuple(v):
    """Parse a version string into a comparable tuple. Ignores pre-release suffixes."""
    if not v:
        return (0,)
    parts = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", v)
    if not parts:
        return (0,)
    return tuple(int(x) if x else 0 for x in parts.groups())


def check_library_versions():
    """Compare installed library versions against PyPI latest."""
    outdated = []
    major_behind = []
    could_not_check = []

    for lib in TRACKED_LIBRARIES:
        installed = _get_installed_version(lib)
        latest = _get_pypi_latest(lib)

        if not installed:
            could_not_check.append(lib)
            continue
        if not latest:
            could_not_check.append(lib)
            continue

        inst_tuple = _version_tuple(installed)
        latest_tuple = _version_tuple(latest)

        if inst_tuple < latest_tuple:
            # How far behind?
            if inst_tuple[0] < latest_tuple[0]:
                major_behind.append(f"{lib} {installed} -> {latest} (major)")
            else:
                outdated.append(f"{lib} {installed} -> {latest}")

    if major_behind:
        return {
            "name": "Python libraries",
            "status": "warning",
            "message": f"{len(major_behind)} libraries major-version behind",
            "details": "; ".join(major_behind + outdated),
        }
    elif outdated:
        return {
            "name": "Python libraries",
            "status": "ok",
            "message": f"{len(outdated)} minor updates available",
            "details": "; ".join(outdated),
        }
    elif could_not_check:
        return {
            "name": "Python libraries",
            "status": "warning",
            "message": f"Could not check {len(could_not_check)} libraries",
            "details": "; ".join(could_not_check),
        }
    else:
        return {
            "name": "Python libraries",
            "status": "ok",
            "message": "All tracked libraries up to date",
            "details": "",
        }


def check_anthropic_model():
    """Make a tiny API call to confirm the model still works."""
    if not ANTHROPIC_KEY:
        return {
            "name": "Anthropic model",
            "status": "warning",
            "message": "No API key available to check",
            "details": "",
        }

    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=EXPECTED_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {
            "name": "Anthropic model",
            "status": "ok",
            "message": f"{EXPECTED_MODEL} responding normally",
            "details": "",
        }
    except Exception as e:
        error_str = str(e)
        if "not_found" in error_str.lower() or "404" in error_str:
            return {
                "name": "Anthropic model",
                "status": "critical",
                "message": f"{EXPECTED_MODEL} no longer exists",
                "details": "Model may have been deprecated. Update MODEL constant in bot.py and whatsapp_reader.py.",
            }
        return {
            "name": "Anthropic model",
            "status": "warning",
            "message": "API call failed",
            "details": error_str[:200],
        }


def check_telegram_activity(costs_sheet):
    """Check whether the Telegram bot has been producing cost rows recently."""
    try:
        rows = costs_sheet.get_all_values()
    except Exception as e:
        return {
            "name": "Telegram ingestion",
            "status": "warning",
            "message": "Could not read Costs tab",
            "details": str(e)[:200],
        }

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=2)
    recent_telegram = 0

    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff and row[1] == "bot.py":
                recent_telegram += 1
        except (ValueError, IndexError):
            continue

    if recent_telegram == 0:
        return {
            "name": "Telegram ingestion",
            "status": "critical",
            "message": "Zero cost rows from bot.py in past 2 days",
            "details": "Telegram bot may be broken. Check GitHub Actions logs for failures.",
        }
    else:
        return {
            "name": "Telegram ingestion",
            "status": "ok",
            "message": f"{recent_telegram} Claude calls in past 2 days",
            "details": "",
        }


def check_whatsapp_activity(costs_sheet):
    """Check whether the WhatsApp reader has been producing cost rows recently (longer window because Mac may sleep)."""
    try:
        rows = costs_sheet.get_all_values()
    except Exception as e:
        return {
            "name": "WhatsApp ingestion",
            "status": "warning",
            "message": "Could not read Costs tab",
            "details": str(e)[:200],
        }

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=3)
    recent_whatsapp = 0

    for row in rows[1:]:
        if len(row) < 2:
            continue
        try:
            ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff and row[1] == "whatsapp_reader.py":
                recent_whatsapp += 1
        except (ValueError, IndexError):
            continue

    if recent_whatsapp == 0:
        return {
            "name": "WhatsApp ingestion",
            "status": "critical",
            "message": "Zero cost rows from whatsapp_reader.py in past 3 days",
            "details": "WhatsApp reader may be broken, Mac may have been offline, or WhatsApp DB schema may have changed. Check reader.log on Mac.",
        }
    else:
        return {
            "name": "WhatsApp ingestion",
            "status": "ok",
            "message": f"{recent_whatsapp} Claude calls in past 3 days",
            "details": "",
        }


def check_extraction_output(people_sheet, deals_sheet):
    """Check whether extraction is actually producing items, not just making API calls."""
    try:
        people_rows = people_sheet.get_all_values()
        deal_rows = deals_sheet.get_all_values()
    except Exception as e:
        return {
            "name": "Extraction output",
            "status": "warning",
            "message": "Could not read People or Deals tab",
            "details": str(e)[:200],
        }

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    recent_people = 0
    for row in people_rows[1:]:
        if len(row) < 5:
            continue
        try:
            ts = datetime.fromisoformat(row[4].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent_people += 1
        except (ValueError, IndexError):
            continue

    recent_deals = 0
    for row in deal_rows[1:]:
        if len(row) < 8:
            continue
        try:
            ts = datetime.fromisoformat(row[7].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent_deals += 1
        except (ValueError, IndexError):
            continue

    if recent_people == 0 and recent_deals == 0:
        return {
            "name": "Extraction output",
            "status": "warning",
            "message": "Zero new people or deals added in past 7 days",
            "details": "Either no relevant content this week, or extraction prompts may need tuning.",
        }
    else:
        return {
            "name": "Extraction output",
            "status": "ok",
            "message": f"{recent_people} people, {recent_deals} deals added in past 7 days",
            "details": "",
        }


# --- Runner and formatters ---

def run_health_checks():
    print("Running health checks...")
    checks = []

    checks.append(check_python_version())
    print(f"  Python: {checks[-1]['status']}")

    checks.append(check_library_versions())
    print(f"  Libraries: {checks[-1]['status']}")

    checks.append(check_anthropic_model())
    print(f"  Model: {checks[-1]['status']}")

    try:
        sheets = get_costs_sheet()
        checks.append(check_telegram_activity(sheets["costs"]))
        print(f"  Telegram: {checks[-1]['status']}")

        checks.append(check_whatsapp_activity(sheets["costs"]))
        print(f"  WhatsApp: {checks[-1]['status']}")

        checks.append(check_extraction_output(sheets["people"], sheets["deals"]))
        print(f"  Extraction: {checks[-1]['status']}")
    except Exception as e:
        checks.append({
            "name": "Sheet connectivity",
            "status": "critical",
            "message": "Could not connect to Google Sheet",
            "details": str(e)[:200],
        })
        print(f"  Sheet: critical")

    return checks


STATUS_COLORS = {
    "ok": "#4caf50",
    "warning": "#f57c00",
    "critical": "#d32f2f",
}

STATUS_ICONS = {
    "ok": "&#10003;",        # checkmark
    "warning": "&#9888;",     # warning sign
    "critical": "&#10005;",   # cross
}


def build_health_html(checks):
    # Overall status is the worst individual status
    order = {"ok": 0, "warning": 1, "critical": 2}
    overall = max(checks, key=lambda c: order.get(c["status"], 0))["status"]
    overall_label = {
        "ok": "All systems healthy",
        "warning": "Attention recommended",
        "critical": "Action needed",
    }[overall]

    rows_html = "\n".join(
        f'''<tr>
          <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0;">
            <span style="color:{STATUS_COLORS[c['status']]};font-weight:bold;font-size:14px;">{STATUS_ICONS[c['status']]}</span>
          </td>
          <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0;font-weight:500;">{c['name']}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #f0f0f0;color:#555;font-size:13px;">{c['message']}
            {f'<br><span style="color:#888;font-size:11px;">{c["details"]}</span>' if c['details'] else ''}
          </td>
        </tr>'''
        for c in checks
    )

    return f"""
    <h3 style="border-bottom:1px solid #eee;padding-bottom:6px;">System health</h3>
    <div style="background:#f5f7fa;padding:12px 16px;border-radius:6px;margin:10px 0;border-left:4px solid {STATUS_COLORS[overall]};">
      <strong style="color:{STATUS_COLORS[overall]};">{overall_label}</strong>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      {rows_html}
    </table>
    """


def build_health_text(checks):
    lines = ["SYSTEM HEALTH", ""]
    for c in checks:
        icon = {"ok": "[OK]", "warning": "[WARN]", "critical": "[CRIT]"}[c["status"]]
        lines.append(f"{icon} {c['name']}: {c['message']}")
        if c["details"]:
            lines.append(f"       {c['details']}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    # Allow running standalone for testing
    checks = run_health_checks()
    print("\n" + build_health_text(checks))
