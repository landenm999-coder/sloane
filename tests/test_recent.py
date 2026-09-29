"""The recent skill: what he just captured, newest first, and nothing changed by asking.

DESTRUCTIVE: truncates workshop_items, list_items, commitments, reminders, expenses and the follow-ups.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.recent import Recent, window

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- pure ----------------------------------------------------------------------
check("default is the latest one", window("what did I just capture?"), (1, False))
check("last N", window("last 5 captures"), (5, False))
check("last N in words", window("show my last three captures"), (3, False))
check("today", window("what did I capture today"), (50, True))
check("N is capped", window("last 99 captures"), (20, False))
check("bare N (from /recent 5)", window("7"), (7, False))


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = datetime(2026, 9, 29, 15, 0, tzinfo=DEN)
    async with Store(config) as store:
        await store._exec("truncate workshop_items, list_items, commitments, reminders, expenses cascade")
        await store._exec("delete from working_set where kind = 'follow_up'")
        await store._exec("delete from state where key like %s", ("learned.%",))
        ctx = SkillContext(store=store, config=config, clock=lambda: now)
        recent = Recent(ctx)
        reg = Registry([recent], ctx)

        # One save per source, staggered: the newest is the /spent, 2 minutes ago.
        idea = await store.add_workshop_item("sticker pack for DECA booth", "sticker pack for DECA booth")
        await store.put_state("learned.note.locker", "my locker is 214", category="learned", confidence=1.0,
                              source="told 2026-09-29", pin=True)
        await store.put_state("learned.note.nightly", "likes tea", category="learned", confidence=0.8,
                              source="learned 2026-09-29", pin=True)
        follow = await store.add_follow_up("call the dentist", None)
        added = await store.add_list_items("grocery", ["milk"])
        promise = await store.add_commitment("send Keegan the outline")
        remind = await store.add_reminder(text="take out the trash", due_at=now + timedelta(hours=3))
        await store.add_reminder(text="⏲️ Time's up", due_at=now + timedelta(minutes=5), source="timer")
        expense = await store.add_expense(cents=1200, what="lunch", category="food", spent_on=now.date())
        stamps = [
            ("workshop_items", "created_at", idea["id"], 50),
            ("state", "updated_at", None, 40),
            ("working_set", "opened_at", follow["id"], 30),
            ("list_items", "added_at", added[0]["id"], 20),
            ("commitments", "promised_at", promise["id"], 15),
            ("reminders", "created_at", remind["id"], 10),
            ("expenses", "created_at", expense["id"], 2),
        ]
        for table, column, row_id, minutes in stamps:
            at = now - timedelta(minutes=minutes)
            if row_id is None:
                await store._exec(f"update {table} set {column} = %s where key = 'learned.note.locker'", (at,))
            else:
                await store._exec(f"update {table} set {column} = %s where id = %s", (at, row_id))
        await store._exec("update state set updated_at = %s where key = 'learned.note.nightly'",
                          (now - timedelta(minutes=1),))
        await store._exec("update reminders set created_at = %s where source = 'timer'",
                          (now - timedelta(minutes=1),))

        async def counts() -> list[int]:
            out = []
            for table in ("workshop_items", "state", "working_set", "list_items", "commitments", "reminders",
                          "expenses"):
                out.append((await store._one(f"select count(*) as n from {table}"))["n"])
            return out

        before = await counts()

        latest = await reg.route("what did I just capture?")
        check("latest is the /spent, not a timer or a nightly fact", latest.speech,
              'Latest: 2 min ago · /spent → food $12: "lunch"')
        check("check what I just wrote down on capture", (await reg.route("check what I just wrote down on capture")).speech,
              latest.speech)
        check("/recent is the same", (await reg.command("recent", "")).speech, latest.speech)

        five = await reg.route("last 5 captures")
        check("last 5: newest first, one per source", five.detail.splitlines(), [
            '1. 2 min ago · /spent → food $12: "lunch"',
            '2. 10 min ago · /remind → reminders: "take out the trash"',
            '3. 15 min ago · /promise → promises: "send Keegan the outline"',
            '4. 20 min ago · /list → grocery list: "milk"',
            '5. 30 min ago · /followup → loose ends: "call the dentist"',
        ])
        check("last 5 speech", five.speech.split(".")[0], "Your last 5 captures")
        seven = await reg.command("recent", "7")
        check("7 reach idea and memory, with the source label", seven.detail.splitlines()[5:], [
            '6. 40 min ago · /remember → memory: "my locker is 214"',
            '7. 50 min ago · /idea → workshop: "sticker pack for DECA booth"',
        ])

        # Yesterday's save is out of "today", and today's midnight is his, not UTC's.
        await store._exec("update expenses set created_at = %s", (now.replace(hour=0, minute=0) - timedelta(minutes=1),))
        await store._exec("update workshop_items set created_at = %s", (now.replace(hour=0, minute=0) + timedelta(minutes=1),))
        today = await reg.route("what did I capture today?")
        check("today stops at local midnight", ("lunch" in today.detail, "sticker" in today.detail), (False, True))
        check("today: the count", today.speech.split(".")[0], "6 captures today")
        check("last night's save reads as yesterday", (await reg.command("recent", "7")).detail.splitlines()[-1],
              '7. yesterday 11:59 PM · /spent → food $12: "lunch"')

        check("asking changed nothing", await counts(), before)

        # Rules that must not over-reach.
        for text in ["last 5", "what did I say yesterday?", "capture this", "what did I save on car insurance?",
                     "add milk to my grocery list", "the last time I captured a rare pokemon"]:
            check(f"not ours: {text!r}", await reg.route(text), None)

        await store._exec("truncate workshop_items, list_items, commitments, reminders, expenses cascade")
        await store._exec("delete from working_set where kind = 'follow_up'")
        await store._exec("delete from state where key like %s", ("learned.%",))
        check("nothing yet", (await reg.route("what did I just capture?")).speech,
              "You haven't captured anything yet.")
        check("nothing today", (await reg.route("what did I capture today?")).speech, "Nothing captured today.")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("recent: latest, last N, today, newest first, source labels; reads only")
