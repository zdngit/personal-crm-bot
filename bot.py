"""
Personal CRM Bot (Telegram)
Extracts people, links, tasks, ideas, deals. Summarizes links.
Logs every Claude API call to the Costs tab.
"""
import os
import re
import json
import asyncio
from datetime import datetime, timezone

import httpx

from telethon import TelegramClient
from telethon.sessions import StringSession

import gspread
from google.oauth2.service_account import Credentials

from anthropic import Anthropic

from cost_tracker import log_cost

# --- Config ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"
CONFIDENCE_THRESHOLD = 0.7
FETCH_TIMEOUT = 15
FETCH_MAX_CHARS = 20000
MODEL = "claude-opus-4-5"

# --- Sheets ---
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
        "tasks": sh.worksheet("Tasks"),
        "ideas": sh.worksheet("Ideas"),
        "deals": sh.worksheet("Deals"),
        "inbox": sh.worksheet("Inbox"),
        "costs": sh.worksheet("Costs"),
    }

# --- Claude ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note the user wrote to themselves. The user is an investor. They use this notes channel for FIVE purposes:

A) ADDING A PERSON to their CRM. Signals: "add X to people", "met X", introducing a new contact.

B) SAVING A LINK to read later.

C) CAPTURING A TASK. Signals: "email X", "book Y", "remind me to Z", "need to", "follow up", "draft".

D) CAPTURING AN IDEA - speculative thought, hypothesis, observation, musing. Signals: "what if", "idea:", "could be cool", "interesting that", "wonder whether".

E) CAPTURING A VENTURE DEAL. Any company raise, investment opportunity, secondary, acquisition.

HARD TASK MARKERS: "todo:", "remind me to", "need to", "have to", "must", "don't forget to", "follow up" - these are tasks.

DISAMBIGUATION:
- A note that ADDS A PERSON should not also produce a task or idea.
- A DEAL and a PERSON can co-occur (e.g., "Sarah pitched Acme Series A" -> 1 person + 1 deal).
- An IDEA stands alone - not directed at anyone.
- A DEAL has a specific company and concrete signals.

NAME FIDELITY: Names must appear verbatim. Never invent.

CONFIDENCE: 0.9-1.0 clear, 0.7-0.89 confident, 0.5-0.69 route to inbox, below 0.5 skip.

Extract:
1. PEOPLE: name, context, types (from: investor, vc, angel, deal source, entrepreneur, founder, family office, lp, operator, advisor, lawyer, banker, family, friend, journalist, recruiter), confidence.
2. LINKS: url, title, confidence.
3. TASKS: task, due, confidence.
4. IDEAS: idea, confidence.
5. DEALS: company, terms, direction (looking/offering/tracking), timeline, deal_type (seed/series_a/series_b/series_c/series_d/growth/secondary/bridge/safe/convertible/m_and_a/non_venture/unknown), mentioned_by, confidence.

Return ONLY valid JSON:
{"people":[],"links":[],"tasks":[],"ideas":[],"deals":[]}

The note:
---
%s
---"""


SUMMARY_PROMPT = """You will be given the text content of a web page. Write a 2-3 sentence summary of what the article/post is about and the key takeaway. Be concrete. Don't use phrases like "this article discusses" - just state the substance.

If unreadable/login wall/error, respond with exactly: UNREADABLE

Page URL: %s
Page title: %s

Content:
---
%s
---

Summary:"""


def extract(message_text, costs_sheet):
    resp = client_ai.messages.create(
        model=MODEL,
        max_tokens=1536,
        messages=[{"role": "user", "content": EXTRACTION_PROMPT % message_text}],
    )
    log_cost(costs_sheet, script="bot.py", category="extraction", response=resp, model=MODEL)

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        print(f"  ! JSON parse failed: {text[:300]}")
        return {"people": [], "links": [], "tasks": [], "ideas": [], "deals": []}


def fetch_page(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=FETCH_TIMEOUT, headers=headers) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                return None, None
            html = resp.text
    except Exception as e:
        print(f"    fetch error: {e}")
        return None, None

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""
    title = re.sub(r"\s+", " ", title)[:200]

    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)

    for entity, char in {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
                          "&quot;": '"', "&#39;": "'", "&apos;": "'"}.items():
        text = text.replace(entity, char)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 100:
        return title, None
    return title, text[:FETCH_MAX_CHARS]


def summarize_link(url, claude_title, costs_sheet):
    print(f"    fetching: {url[:80]}")
    page_title, content = fetch_page(url)
    if not content:
        return "Unable to fetch page for summary."

    try:
        resp = client_ai.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": SUMMARY_PROMPT % (url, page_title or claude_title or "", content),
            }],
        )
        log_cost(costs_sheet, script="bot.py", category="link_summary", response=resp, model=MODEL)
        summary = resp.content[0].text.strip()
        if summary == "UNREADABLE" or not summary:
            return "Page content not readable."
        return summary
    except Exception as e:
        print(f"    summary error: {e}")
        return "Summary generation failed."


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_id": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


async def main():
    sheets = get_sheets()
    state = load_state()
    last_id = state.get("last_id", 0)

    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as tg:
        me = await tg.get_me()
        new_messages = []
        async for msg in tg.iter_messages(me, min_id=last_id):
            if msg.text:
                new_messages.append(msg)

        new_messages.reverse()
        print(f"Found {len(new_messages)} new messages")

        existing_names = {row[0].strip().lower() for row in sheets["people"].get_all_values()[1:] if row}
        existing_urls = {row[0].strip() for row in sheets["links"].get_all_values()[1:] if row}

        for msg in new_messages:
            print(f"Processing message {msg.id}: {msg.text[:60]}...")
            result = extract(msg.text, sheets["costs"])
            now = datetime.now(timezone.utc).isoformat()
            preview = msg.text[:200]

            for p in result.get("people", []):
                name = p.get("name", "").strip()
                if not name or name.lower() in existing_names:
                    continue
                conf = float(p.get("confidence", 1.0))
                types_list = p.get("types", []) or []
                types_str = ", ".join(x.strip().lower() for x in types_list if x.strip())
                context = p.get("context", "")
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "person", f"{name} - {context} [{types_str}]",
                        f"Low confidence ({conf:.2f})", f"{conf:.2f}", preview, now, "pending"
                    ])
                    print(f"  ? Inbox person: {name}")
                else:
                    sheets["people"].append_row([name, context, types_str, "", now, preview, "FALSE"])
                    existing_names.add(name.lower())
                    print(f"  + Person: {name}")

            for link in result.get("links", []):
                url = link.get("url", "").strip()
                if not url or url in existing_urls:
                    continue
                conf = float(link.get("confidence", 1.0))
                title = link.get("title", "")
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "link", f"{url} - {title}",
                        f"Low confidence ({conf:.2f})", f"{conf:.2f}", preview, now, "pending"
                    ])
                else:
                    summary = summarize_link(url, title, sheets["costs"])
                    sheets["links"].append_row([url, title, summary, now, preview, "FALSE"])
                    existing_urls.add(url)
                    print(f"  + Link: {url[:50]}")

            for t in result.get("tasks", []):
                task_text = t.get("task", "").strip()
                if not task_text:
                    continue
                conf = float(t.get("confidence", 1.0))
                due = t.get("due", "").strip()
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "task", task_text + (f" (due {due})" if due else ""),
                        f"Low confidence ({conf:.2f})", f"{conf:.2f}", preview, now, "pending"
                    ])
                else:
                    sheets["tasks"].append_row([task_text, now, due, "pending", preview])
                    print(f"  + Task: {task_text[:50]}")

            for idea in result.get("ideas", []):
                idea_text = idea.get("idea", "").strip()
                if not idea_text:
                    continue
                conf = float(idea.get("confidence", 1.0))
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "idea", idea_text,
                        f"Low confidence ({conf:.2f})", f"{conf:.2f}", preview, now, "pending"
                    ])
                else:
                    sheets["ideas"].append_row([idea_text, now, preview, "FALSE"])
                    print(f"  + Idea: {idea_text[:50]}")

            for d in result.get("deals", []):
                company = d.get("company", "").strip()
                if not company:
                    continue
                conf = float(d.get("confidence", 1.0))
                terms = d.get("terms", "")
                direction = d.get("direction", "tracking")
                timeline = d.get("timeline", "")
                deal_type = d.get("deal_type", "unknown")
                mentioned_by = d.get("mentioned_by", "")
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "deal", f"{company} [{deal_type}] - {terms} ({direction})",
                        f"Low confidence ({conf:.2f})", f"{conf:.2f}", preview, now, "pending"
                    ])
                    print(f"  ? Inbox deal: {company}")
                else:
                    sheets["deals"].append_row([
                        company, terms, direction, timeline, deal_type,
                        mentioned_by, "telegram", now, preview, "FALSE"
                    ])
                    print(f"  + Deal: {company} ({deal_type}, {direction})")

            state["last_id"] = max(state["last_id"], msg.id)

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
