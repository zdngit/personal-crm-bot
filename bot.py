"""
Personal CRM Bot
Reads new messages from Telegram Saved Messages, extracts people, links,
and tasks via Claude, writes them to a Google Sheet.
"""
import os
import json
import asyncio
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

import gspread
from google.oauth2.service_account import Credentials

from anthropic import Anthropic

# --- Config from environment ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"

# --- Google Sheets setup ---
def get_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return (
        sh.worksheet("People"),
        sh.worksheet("Links"),
        sh.worksheet("Tasks"),
    )

# --- Claude extraction ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note someone wrote to themselves. They use this notes channel for THREE distinct purposes, and you must classify carefully. A single note usually serves ONE purpose, occasionally two, rarely all three.

The three purposes are:

A) ADDING A PERSON to their CRM. Signals: "add X to people", "met X", "X is interesting", introducing a new contact, describing who someone is or what they do. The note is ABOUT a person.

B) SAVING A LINK to read later. Signals: a URL is present, optionally with a few words of context about what it is.

C) CAPTURING A TASK they need to do. Signals: imperative phrasing directed at themselves like "email X", "book Y", "remind me to Z", "todo: ...", "need to ...", "follow up with X about Y", "draft the memo", "call the lawyer". The note is about an ACTION the writer needs to take.

CRITICAL DISAMBIGUATION RULE: If a note is primarily ADDING A PERSON (purpose A), do NOT also extract a task from it, even if the description of the person sounds action-adjacent. For example:
- "Add Sarah Chen to people - she can intro me to founders at Sequoia" -> ONE person (Sarah), ZERO tasks. The intro is a description of Sarah's usefulness, not a task.
- "Met Marcus at dinner, he runs a family office and wants to invest in AI" -> ONE person, ZERO tasks.
- "Email Sarah about the deck tomorrow" -> ZERO people, ONE task.
- "Met Sarah Chen at Sequoia. Need to email her the deck by Friday." -> ONE person AND ONE task (these are clearly separate clauses).

Extract:

1. PEOPLE - real humans named in the note. For each:
   - name: as written
   - context: one sentence on who they are or why they're being added
   - types: lowercase tags inferred from context. Common: "investor", "vc", "angel", "deal source", "entrepreneur", "founder", "family office", "lp", "operator", "advisor", "lawyer", "banker", "family", "friend", "journalist", "recruiter". Multiple allowed. Empty list if no signal.

2. LINKS - URLs in the note. For each:
   - url: the full URL
   - title: short guess at what it is, based on surrounding context

3. TASKS - action items the writer needs to do themselves. For each:
   - task: rewrite the action as a clear imperative starting with a verb (e.g., "Email Sarah the deck")
   - due: any date/time mentioned in natural language (e.g., "Friday", "tomorrow", "next week"), or empty string if none

Return ONLY valid JSON, no prose:
{"people": [{"name": "...", "context": "...", "types": ["..."]}], "links": [{"url": "...", "title": "..."}], "tasks": [{"task": "...", "due": "..."}]}

Use [] for any category with no items. Be conservative - do not invent.

The note:
---
%s
---"""

def extract(message_text):
    resp = client_ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
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
        return {"people": [], "links": [], "tasks": []}

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
    people_sheet, links_sheet, tasks_sheet = get_sheets()
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

        existing_names = {row[0].strip().lower() for row in people_sheet.get_all_values()[1:] if row}
        existing_urls = {row[0].strip() for row in links_sheet.get_all_values()[1:] if row}

        for msg in new_messages:
            print(f"Processing message {msg.id}: {msg.text[:60]}...")
            result = extract(msg.text)
            now = datetime.now(timezone.utc).isoformat()
            preview = msg.text[:200]

            for p in result.get("people", []):
                name = p.get("name", "").strip()
                if not name or name.lower() in existing_names:
                    continue
                types_list = p.get("types", []) or []
                types_str = ", ".join(x.strip().lower() for x in types_list if x.strip())
                people_sheet.append_row([name, p.get("context", ""), types_str, "", now, preview])
                existing_names.add(name.lower())
                print(f"  + Person: {name} [{types_str}]")

            for link in result.get("links", []):
                url = link.get("url", "").strip()
                if not url or url in existing_urls:
                    continue
                links_sheet.append_row([url, link.get("title", ""), now, preview, "FALSE"])
                existing_urls.add(url)
                print(f"  + Link: {url}")

            for t in result.get("tasks", []):
                task_text = t.get("task", "").strip()
                if not task_text:
                    continue
                due = t.get("due", "").strip()
                tasks_sheet.append_row([task_text, now, due, "pending", preview])
                print(f"  + Task: {task_text} (due: {due or 'none'})")

            state["last_id"] = max(state["last_id"], msg.id)

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
