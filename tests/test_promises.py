"""Promises (/promise, /promises, /kept) and reminders in FACTS.

DESTRUCTIVE: truncates commitments and reminders.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.memory.tiers import render_facts
from sloane.promises import parse

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=DEN)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def fmt(p):
    return (p.what, p.person, p.due.strftime("%a %H:%M") if p.due else None) if p else None


check("to Name, by a day: end of that day", fmt(parse("send the outline to Keegan by friday", NOW)),
      ("send the outline to Keegan", "Keegan", "Fri 20:00"))
check("Name: form, by a day and time", fmt(parse("Keegan: the DECA slides by thursday 5pm", NOW)),
      ("the DECA slides", "Keegan", "Thu 17:00"))
check("no due date is fine", fmt(parse("call grandma", NOW)), ("call grandma", None, None))
check("by tomorrow morning keeps the morning", fmt(parse("finish the bakery site by tomorrow morning", NOW)),
      ("finish the bakery site", None, "Wed 07:00"))
check("'by the way' is not a deadline", fmt(parse("help with it by the way", NOW)),
      ("help with it by the way", None, None))
check("an unreadable 'by' stays in the text", fmt(parse("done by whenever", NOW)),
      ("done by whenever", None, None))
check("nothing promised", (parse("", NOW), parse("Keegan:", NOW)), (None, None))

facts, _ = render_facts(reminders=[{"text": "call Keegan", "due_at": NOW.replace(hour=17)}],
                        budget=500, tz="America/Denver")
check("upcoming reminders are FACTS the agent can see", "REMINDER set for" in facts and "call Keegan" in facts, True)


async def integration() -> None:
    from sloane.memory.store import Store
    from sloane.telegram import Bot

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      telegram_chat_id=42, telegram_bot_token="x")
    async with Store(config) as store:
        await store._exec("truncate commitments, reminders")
        bot = Bot(store, None, config)
        bot._now = lambda: NOW

        async def cmd(text):
            return await bot._handle_command(text)

        r = await cmd("/promise send the outline to Keegan by friday")
        check("/promise notes it", r.speech, "Noted: send the outline to Keegan, due Friday at 8:00 PM.")
        await cmd("/promise call grandma")
        r = await cmd("/promises")
        check("/promises lists, soonest first",
              r.detail.splitlines()[:2],
              ["1. send the outline to Keegan (to Keegan) — due Friday at 8:00 PM", "2. call grandma"])
        again = await store.person_id("keegan")
        first = await store.person_id("Keegan")
        check("one person per name, case-insensitive", again, first)
        r = await cmd("/kept 1")
        check("/kept closes it", r.speech, "Nice. Marked kept: send the outline to Keegan.")
        check("and it leaves FACTS", [c["what"] for c in await store.open_commitments()], ["call grandma"])
        check("a bad number is refused", (await cmd("/kept 5")).speech, "Use /kept with a number from /promises.")
        check("an empty promise asks", (await cmd("/promise")).speech, "What did you promise?")


asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("promises: parser, /promise, /promises, /kept, and reminders in FACTS")
