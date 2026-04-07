"""
Personal CRM Bot
Reads new messages from Telegram Saved Messages, extracts people, links,
and tasks via Claude. Low-confidence or ambiguous items go to an Inbox tab
for manual review in the evening digest.
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
CONFIDENCE_THRESHOLD = 0.7  # Below this -> Inbox for manual review

# --- Google Sheets setup ---
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
        "inbox": sh.worksheet("Inbox"),
    }

# --- Claude extraction ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note someone wrote to themselves. They use this notes channel for THREE distinct purposes:

A) ADDING A PERSON to their CRM. Signals: "add X to people", "met X", introducing a new contact, describing who someone is or what they do. The note is ABOUT a person.

B) SAVING A LINK to read later. Signals: a URL is present, optionally with a few words of context.

C) CAPTURING A TASK they need to do. Signals: imperative phrasing directed at themselves like "email X", "book Y", "remind me to Z", "need to ...", "follow up", "draft the memo", "call the lawyer".

HARD TASK MARKERS: If the note begins with or contains any of these markers, it is DEFINITELY a task and you must extract it as a task, not a person:
- "todo:", "TODO:", "todo "
- "remind me to"
- "need to", "have to", "must"
- "don't forget to"
- "follow up"
A note like "Todo: email Gerard" is a task ("Email Gerard"), NOT a person named Gerard. The person mentioned in a task is the OBJECT of the action, not a CRM entry.

DISAMBIGUATION RULE: If a note is primarily ADDING A PERSON, do NOT also extract a task from it. Examples:
- "Add Sarah Chen to people - she can intro me to founders at Sequoia" -> ONE person, ZERO tasks.
- "Email Sarah about the deck tomorrow" -> ZERO people, ONE task.
- "Todo: email Gerard" -> ZERO people, ONE task ("Email Gerard").
- "Met Sarah Chen at Sequoia. Need to email her the deck by Friday." -> ONE person AND ONE task.

NAME FIDELITY RULE: When extracting a person's name, copy it verbatim from the source text. Do not correct spelling, do not guess at full names, do not substitute similar-sounding words. If the source says "Gerard", the name is "Gerard" - never "Harare" or "Gerald". If you cannot find an exact name, do not extract a person.

CONFIDENCE: For each extracted item, include a confidence score from 0.0 to 1.0:
- 0.9-1.0: Clear, unambiguous. The note explicitly states this is a person/task/link.
- 0.7-0.89: Confident but some ambiguity (e.g., person mentioned without much context, task without clear due date).
- 0.5-0.69: Uncertain. Could plausibly be classified differently. Include the item but flag it for review.
- Below 0.5: Do NOT extract. Skip the item entirely.

Items below 0.7 confidence will be routed to an Inbox tab for the user to manually sort. Use this when you are genuinely unsure, NOT to be lazy - if a note is clearly a task, give it 0.9+, don't hedge.

Extract:

1. PEOPLE - real humans named in the note (only when the note is ABOUT them). For each:
   - name: copied verbatim from source
   - context: one sentence on who they are
   - types: lowercase tags. Common: "investor", "vc", "angel", "deal source", "entrepreneur", "founder", "family office", "lp", "operator", "advisor", "lawyer", "banker", "family", "friend", "journalist", "recruiter". Multiple allowed. Empty list if no signal.
   - confidence: 0.0 to 1.0

2. LINKS - URLs in the note. For each:
   - url: the full URL
   - title: short guess at what it is
   - confidence: 0.0 to 1.0 (URLs are almost always 0.95+)

3. TASKS - action items the writer needs to do. For each:
   - task: rewrite as a clear imperative starting with a verb
   - due: any date/time in natural language, or empty string
   - confidence: 0.0 to 1.0

Return ONLY valid JSON, no prose:
{"people": [{"name": "...", "context": "...", "types": ["..."], "confidence": 0.95}], "links": [{"url": "...", "title": "...", "confidence": 0.99}], "tasks": [{"task": "...", "due": "...", "confidence": 0.9}]}

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
        print(f"  ! JSON parse failed. Raw response: {text[:300]}")
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
                    # Inbox: Type | Content | Reason | Confidence | Source Message | Created | Status
                    sheets["inbox"].append_row([
                        "person", f"{name} - {context} [{types_str}]",
                        f"Low confidence ({conf:.2f})", str(conf), preview, now, "pending"
                    ])
                    print(f"  ? Inbox (person): {name} [conf={conf:.2f}]")
                else:
                    # People: Name | Context | Type | Notes | First Mentioned | Source Message | Sent in Digest
                    sheets["people"].append_row([
                        name, context, types_str, "", now, preview, "FALSE"
                    ])
                    existing_names.add(name.lower())
                    print(f"  + Person: {name} [{types_str}] [conf={conf:.2f}]")

            for link in result.get("links", []):
                url = link.get("url", "").strip()
                if not url or url in existing_urls:
                    continue
                conf = float(link.get("confidence", 1.0))
                title = link.get("title", "")

                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "link", f"{url} - {title}",
                        f"Low confidence ({conf:.2f})", str(conf), preview, now, "pending"
                    ])
                    print(f"  ? Inbox (link): {url} [conf={conf:.2f}]")
                else:
                    sheets["links"].append_row([url, title, now, preview, "FALSE"])
                    existing_urls.add(url)
                    print(f"  + Link: {url} [conf={conf:.2f}]")

            for t in result.get("tasks", []):
                task_text = t.get("task", "").strip()
                if not task_text:
                    continue
                conf = float(t.get("confidence", 1.0))
                due = t.get("due", "").strip()

                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "task", f"{task_text}" + (f" (due {due})" if due else ""),
                        f"Low confidence ({conf:.2f})", str(conf), preview, now, "pending"
                    ])
                    print(f"  ? Inbox (task): {task_text} [conf={conf:.2f}]")
                else:
                    sheets["tasks"].append_row([task_text, now, due, "pending", preview])
                    print(f"  + Task: {task_text} (due: {due or 'none'}) [conf={conf:.2f}]")

            state["last_id"] = max(state["last_id"], msg.id)

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
