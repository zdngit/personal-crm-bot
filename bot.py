"""
Personal CRM Bot
Reads new messages from Telegram Saved Messages, extracts people, links,
tasks, and ideas via Claude. Low-confidence items go to an Inbox tab.
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

# --- Config ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"
CONFIDENCE_THRESHOLD = 0.7

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
        "inbox": sh.worksheet("Inbox"),
    }

# --- Claude ---
client_ai = Anthropic(api_key=ANTHROPIC_KEY)

EXTRACTION_PROMPT = """You are parsing a personal note someone wrote to themselves. They use this notes channel for FOUR distinct purposes:

A) ADDING A PERSON to their CRM. Signals: "add X to people", "met X", introducing a new contact, describing who someone is or what they do. The note is ABOUT a person.

B) SAVING A LINK to read later. Signals: a URL is present, optionally with a few words of context.

C) CAPTURING A TASK they need to do. Signals: imperative phrasing directed at themselves like "email X", "book Y", "remind me to Z", "need to ...", "follow up", "draft the memo".

D) CAPTURING AN IDEA - a speculative thought, hypothesis, observation, or musing they want to revisit later. Signals: "what if X", "idea: X", "could be cool to X", "interesting that X", "wonder whether X", "X might be a good business", or any reflective thought that isn't a task or a person description.

HARD TASK MARKERS: If the note contains any of: "todo:", "TODO:", "remind me to", "need to", "have to", "must", "don't forget to", "follow up" - it is a TASK, not an idea or person.

DISAMBIGUATION RULES:
- A note that ADDS A PERSON should not also produce a task or idea, even if it describes the person's work. "Met Sarah, she's working on agentic AI" -> ONE person, ZERO ideas.
- A note that captures a TASK should not also produce an idea about the task's subject. "Need to research agentic AI tools" -> ONE task, ZERO ideas.
- An IDEA stands alone: it is not directed at anyone, is not an action item, and is not adding a contact. It is the writer thinking out loud.
- If a note is clearly a musing AND mentions a person tangentially ("interesting how Sarah's approach to fundraising works - might be a model for us"), extract it as ONE idea, ZERO people.

Examples:
- "Add Sarah Chen to people - partner at Sequoia" -> 1 person, 0 ideas, 0 tasks
- "Email Sarah about the deck" -> 0 people, 1 task, 0 ideas
- "What if we restructured the fund to allow for longer hold periods" -> 0 everything, 1 idea
- "Interesting framing from Patrick OShaughnessys podcast - capital is patient when LPs are aligned" -> 0 people, 0 tasks, 1 idea
- "Idea: a tool that auto-summarizes board decks" -> 1 idea
- "Met Marcus, runs a family office. Could be a deal source. Also makes me think we should formalize our intro process." -> 1 person AND 1 idea (the formalization thought is separate from describing Marcus)

NAME FIDELITY: When extracting a person's name, copy it verbatim from the source. Never invent or substitute. If you cannot find an exact name, do not extract a person.

CONFIDENCE: For each extracted item, include a confidence score 0.0-1.0:
- 0.9-1.0: Clear and unambiguous
- 0.7-0.89: Confident but some ambiguity
- 0.5-0.69: Uncertain, route to Inbox for manual review
- Below 0.5: Do not extract

Extract:

1. PEOPLE - real humans the note is ABOUT. Each: name (verbatim), context (one sentence), types (lowercase tags from: investor, vc, angel, deal source, entrepreneur, founder, family office, lp, operator, advisor, lawyer, banker, family, friend, journalist, recruiter; multiple allowed; empty if none), confidence.

2. LINKS - URLs in the note. Each: url, title (short guess), confidence.

3. TASKS - action items the writer needs to do. Each: task (rewrite as imperative), due (natural language date or empty), confidence.

4. IDEAS - speculative thoughts, hypotheses, observations, or musings. Each: idea (rewrite as a clear statement, preserving the original thought), confidence.

Return ONLY valid JSON, no prose:
{"people": [{"name":"...","context":"...","types":["..."],"confidence":0.95}], "links": [{"url":"...","title":"...","confidence":0.99}], "tasks": [{"task":"...","due":"...","confidence":0.9}], "ideas": [{"idea":"...","confidence":0.85}]}

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
        return {"people": [], "links": [], "tasks": [], "ideas": []}

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
                        f"Low confidence ({conf:.2f})", str(conf), preview, now, "pending"
                    ])
                    print(f"  ? Inbox (person): {name} [conf={conf:.2f}]")
                else:
                    sheets["people"].append_row([name, context, types_str, "", now, preview, "FALSE"])
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

            for idea in result.get("ideas", []):
                idea_text = idea.get("idea", "").strip()
                if not idea_text:
                    continue
                conf = float(idea.get("confidence", 1.0))
                if conf < CONFIDENCE_THRESHOLD:
                    sheets["inbox"].append_row([
                        "idea", idea_text,
                        f"Low confidence ({conf:.2f})", str(conf), preview, now, "pending"
                    ])
                    print(f"  ? Inbox (idea): {idea_text[:60]} [conf={conf:.2f}]")
                else:
                    # Ideas: Idea | Created | Source Message | Sent in Digest
                    sheets["ideas"].append_row([idea_text, now, preview, "FALSE"])
                    print(f"  + Idea: {idea_text[:60]} [conf={conf:.2f}]")

            state["last_id"] = max(state["last_id"], msg.id)

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
