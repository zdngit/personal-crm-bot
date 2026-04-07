"""
Personal CRM Bot (Telegram)
Reads new messages from Telegram Saved Messages, extracts people, links,
tasks, ideas, and venture deals via Claude. Links get fetched and summarized
automatically so the sheet always shows a 2-3 sentence summary.
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

# --- Config ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"
CONFIDENCE_THRESHOLD = 0.7
FETCH_TIMEOUT = 15  # seconds
FETCH_MAX_CHARS = 20000  # Cap page content sent to Claude

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
    }

# --- Claude ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note the user wrote to themselves. The user is an investor. They use this notes channel for FIVE purposes:

A) ADDING A PERSON to their CRM. Signals: "add X to people", "met X", introducing a new contact.

B) SAVING A LINK to read later.

C) CAPTURING A TASK. Signals: "email X", "book Y", "remind me to Z", "need to", "follow up", "draft".

D) CAPTURING AN IDEA - speculative thought, hypothesis, observation, musing. Signals: "what if", "idea:", "could be cool", "interesting that", "wonder whether".

E) CAPTURING A VENTURE DEAL they want to track. Any company raise, investment opportunity, secondary, acquisition. Capture freely - if the user is writing about a deal in any form, extract it. No filtering by source or direction.

HARD TASK MARKERS: "todo:", "remind me to", "need to", "have to", "must", "don't forget to", "follow up" - these are tasks.

DISAMBIGUATION:
- A note that ADDS A PERSON should not also produce a task or idea.
- A note that captures a TASK should not also produce an idea about the subject.
- A DEAL and a PERSON can co-occur (e.g., "Sarah pitched Acme Series A, $15m" -> 1 person Sarah + 1 deal Acme).
- An IDEA stands alone - not directed at anyone, not an action, not adding a contact.
- A DEAL has a specific company and concrete signals. A pure musing like "AI infra is hot" is an idea, not a deal.

NAME FIDELITY: Names must appear verbatim in the source. Never invent.

CONFIDENCE: 0.9-1.0 clear, 0.7-0.89 confident-with-ambiguity, 0.5-0.69 route to inbox, below 0.5 skip.

Extract:

1. PEOPLE: name (verbatim), context, types (lowercase from: investor, vc, angel, deal source, entrepreneur, founder, family office, lp, operator, advisor, lawyer, banker, family, friend, journalist, recruiter), confidence.

2. LINKS: url, title, confidence.

3. TASKS: task (imperative), due, confidence.

4. IDEAS: idea (clear statement), confidence.

5. DEALS: company, terms, direction ("looking"/"offering"/"tracking"), timeline, deal_type (seed/series_a/series_b/series_c/series_d/growth/secondary/bridge/safe/convertible/m_and_a/non_venture/unknown), mentioned_by (or empty), confidence.

Return ONLY valid JSON, no prose:
{"people":[],"links":[],"tasks":[],"ideas":[],"deals":[]}

Be conservative.

The note:
---
%s
---"""


SUMMARY_PROMPT = """You will be given the text content of a web page. Write a 2-3 sentence summary that tells the reader what the article/post/page is about and the key point or takeaway. Be concrete. Don't use phrases like "this article discusses" - just state the substance directly.

If the page content is unreadable, a login wall, an error page, or doesn't contain an actual article, respond with exactly: UNREADABLE

Page URL: %s
Page title: %s

Page content:
---
%s
---

Summary:"""


def extract(message_text):
    resp = client_ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=1536,
        messages=[{"role": "user", "content": EXTRACTION_PROMPT % message_text}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        print(f"  ! JSON parse failed. Raw response: {text[:300]}")
        return {"people": [], "links": [], "tasks": [], "ideas": [], "deals": []}


def fetch_page(url):
    """Fetch a page and return (title, cleaned_text). Returns (None, None) on failure."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
            headers=headers,
        ) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                return None, None
            html = resp.text
    except Exception as e:
        print(f"    fetch error: {e}")
        return None, None

    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""
    title = re.sub(r"\s+", " ", title)[:200]

    # Strip script and style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)

    # Strip all other HTML tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Decode common HTML entities
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 100:
        return title, None

    return title, text[:FETCH_MAX_CHARS]


def summarize_link(url, claude_title):
    """Fetch page and generate a 2-3 sentence summary."""
    print(f"    fetching: {url[:80]}")
    page_title, content = fetch_page(url)
    if not content:
        return "Unable to fetch page for summary."

    try:
        resp = client_ai.messages.create(
            model="claude-opus-4-5",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": SUMMARY_PROMPT % (url, page_title or claude_title or "", content),
            }],
        )
        summary = resp.content[0].text.strip()
        if summary == "UNREADABLE" or not summary:
            return "Page content not readable."
        return summary
    except Exception as e:
        print(f"    summary error: {e}")
        return "Summary generation failed."


# --- State ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_id": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# --- Main ---
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
            result = extract(msg.text)
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
                    print(f"  ? Inbox person: {name} [conf={conf:.2f}]")
                else:
                    sheets["people"].append_row([name, context, types_str, "", now, preview, "FALSE"])
                    existing_names.add(name.lower())
                    print(f"  + Person: {name} [{types_str}]")

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
                    summary = summarize_link(url, title)
                    # Links: URL | Title | Summary | Captured At | Source Message | Sent in Digest
                    sheets["links"].append_row([url, title, summary, now, preview, "FALSE"])
                    existing_urls.add(url)
                    print(f"  + Link: {url[:50]} -> {summary[:60]}")

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
