"""
Personal CRM Bot
Reads new messages from Telegram Saved Messages, extracts people and links
via Claude, writes them to a Google Sheet.
"""
import os
import json
import asyncio
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser

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

STATE_FILE = "state.json"  # Tracks last processed message ID

# --- Google Sheets setup ---
def get_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("People"), sh.worksheet("Links")

# --- Claude extraction ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note someone wrote to themselves. Extract:

1. Any PEOPLE mentioned by name (real humans, not companies). For each, give:
   - name: their name as written
   - context: a one-sentence description of who they are or why mentioned, based on the note
   - types: a list of role/category tags inferred from context. Use lowercase short tags.
     Common examples: "investor", "vc", "angel", "deal source", "entrepreneur", "founder",
     "family office", "lp", "operator", "advisor", "lawyer", "banker", "family", "friend",
     "journalist", "recruiter". Invent new tags if none fit. A person can have multiple types
     (e.g. ["founder", "deal source"]). If you genuinely cannot infer any type from the
     context, use an empty list [].

2. Any LINKS (URLs) in the message. For each, give:
   - url: the full URL
   - title: a short guess at what it is (article, video, tool, etc.) based on context

Return ONLY valid JSON in this exact shape, no prose:
{"people": [{"name": "...", "context": "...", "types": ["...", "..."]}], "links": [{"url": "...", "title": "..."}]}

If there are no people, use []. Same for links. Be conservative — don't invent people who
aren't clearly named, and don't guess types you have no evidence for.

The note:
---
%s
---"""

def extract(message_text: str) -> dict:
    resp = client_ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": EXTRACTION_PROMPT % message_text}],
    )
    text = resp.content[0].text.strip()
    # Strip code fences if Claude added them
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {"people": [], "links": []}

# --- State (last seen message ID) ---
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
    people_sheet, links_sheet = get_sheets()
    state = load_state()
    last_id = state.get("last_id", 0)

    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as tg:
        me = await tg.get_me()
        new_messages = []
        async for msg in tg.iter_messages(me, min_id=last_id):
            if msg.text:
                new_messages.append(msg)

        new_messages.reverse()  # Process oldest first
        print(f"Found {len(new_messages)} new messages")

        # Dedupe people and links by checking what's already in the sheets
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
                types_str = ", ".join(t.strip().lower() for t in types_list if t.strip())
                # Columns: Name | Context | Type | Notes | First Mentioned | Source Message
                people_sheet.append_row([name, p.get("context", ""), types_str, "", now, preview])
                existing_names.add(name.lower())
                print(f"  + Person: {name} [{types_str}]")

            for l in result.get("links", []):
                url = l.get("url", "").strip()
                if not url or url in existing_urls:
                    continue
                links_sheet.append_row([url, l.get("title", ""), now, preview, "FALSE"])
                existing_urls.add(url)
                print(f"  + Link: {url}")

            state["last_id"] = max(state["last_id"], msg.id)

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
