"""Timed reminders: the parser, claim-once delivery, quiet hours, and the bot paths.

DESTRUCTIVE: truncates the reminders table.
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

from sloane.jobs.briefs import JobContext, reminders as reminders_job
from sloane.jobs.governor import Governor
from sloane.memory.store import Store
from sloane.reminders import parse, spoken

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=DEN)  # a Tuesday, 10 AM


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- the parser: (text) -> (local "Day MM-DD HH:MM", what) ---------------------
CASES = {
    "5pm call Keegan": ("Tue 09-22 17:00", "call Keegan"),
    "call Keegan at 5pm": ("Tue 09-22 17:00", "call Keegan"),
    "at 5 to call keegan": ("Tue 09-22 17:00", "call keegan"),
    "at 9 check the oven": ("Tue 09-22 21:00", "check the oven"),
    "tomorrow 7am bring the lab": ("Wed 09-23 07:00", "bring the lab"),
    "bring the lab tomorrow at 7": ("Wed 09-23 07:00", "bring the lab"),
    "in 20 minutes take out laundry": ("Tue 09-22 10:20", "take out laundry"),
    "in an hour stretch": ("Tue 09-22 11:00", "stretch"),
    "in half an hour eat": ("Tue 09-22 10:30", "eat"),
    "friday at 3:30pm DECA forms": ("Fri 09-25 15:30", "DECA forms"),
    "fri DECA forms": ("Fri 09-25 07:00", "DECA forms"),
    "next tuesday pay dues": ("Tue 09-29 07:00", "pay dues"),
    "tonight submit essay": ("Tue 09-22 20:00", "submit essay"),
    "submit essay tonight at 9": ("Tue 09-22 21:00", "submit essay"),
    "tomorrow morning ask Mr Smith": ("Wed 09-23 07:00", "ask Mr Smith"),
    "tomorrow evening practice": ("Wed 09-23 19:30", "practice"),
    "at five call mom": ("Tue 09-22 17:00", "call mom"),
    "at five thirty pm run": ("Tue 09-22 17:30", "run"),
    "in ten minutes leave": ("Tue 09-22 10:10", "leave"),
    "noon lunch": ("Tue 09-22 12:00", "lunch"),
    "midnight post": ("Wed 09-23 00:00", "post"),
    "at 5 o'clock gym": ("Tue 09-22 17:00", "gym"),
    "at 17:45 bus": ("Tue 09-22 17:45", "bus"),
    "6:30 dinner": ("Tue 09-22 18:30", "dinner"),
    "wed at 9 test": ("Wed 09-23 09:00", "test"),
    "in 2 days renew": ("Thu 09-24 10:00", "renew"),
}
for text, want in CASES.items():
    got = parse(text, NOW)
    check(f"parse {text!r}", (got.due.strftime("%a %m-%d %H:%M"), got.text) if got else None, want)

for text in ("read chapter 5", "today at 8am x", "remind about 20 pages", "at 25 go", "in 0 minutes x"):
    check(f"no guess for {text!r}", parse(text, NOW), None)

# Across the November DST change the wall clock holds: "tomorrow 7am" is 7 AM.
dst_eve = datetime(2026, 10, 31, 22, 0, tzinfo=DEN)
check("DST: tomorrow 7am stays 7 AM local",
      parse("tomorrow 7am run", dst_eve).due.strftime("%H:%M %Z"), "07:00 MST")

check("spoken today", spoken(NOW.replace(hour=17), NOW), "at 5:00 PM")
check("spoken tomorrow", spoken(NOW + timedelta(days=1), NOW), "tomorrow at 10:00 AM")
check("spoken this week", spoken(NOW + timedelta(days=3), NOW), "Friday at 10:00 AM")


async def integration() -> None:
    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      telegram_chat_id=42, telegram_bot_token="x")
    async with Store(config) as store:
        await store._exec("truncate reminders")
        said: list[str] = []
        fail = {"on": False}

        async def say(text):
            if fail["on"]:
                raise RuntimeError("telegram down")
            said.append(text)

        ctx = JobContext(store=store, agent=None, governor=Governor(store, config),
                         config=config, say=say)

        await store.add_reminder(text="call Keegan", due_at=NOW.replace(hour=17))
        await store.add_reminder(text="early", due_at=NOW - timedelta(minutes=1))

        # -- due ones go, future ones wait, each goes once ---------------------
        first = await reminders_job(ctx, NOW)
        check("the due reminder is delivered", said, ["⏰ early"])
        check("and reported", first.reason, "1 delivered")
        await reminders_job(ctx, NOW + timedelta(minutes=1))
        check("and never twice", said, ["⏰ early"])

        # -- racing ticks claim once -----------------------------------------------
        await store.add_reminder(text="race", due_at=NOW - timedelta(minutes=1))
        claims = await asyncio.gather(*[store.claim_due_reminders(NOW) for _ in range(5)])
        check("five racing claims deliver it once", sum(len(c) for c in claims), 1)

        # -- quiet hours hold it; morning delivers it, marked late -----------------
        await store.add_reminder(text="take meds", due_at=NOW.replace(day=23, hour=1))
        night = await reminders_job(ctx, datetime(2026, 9, 23, 2, 0, tzinfo=DEN))
        check("quiet hours hold a reminder", night.ran, False)
        morning = datetime(2026, 9, 23, 6, 31, tzinfo=DEN)
        await reminders_job(ctx, morning)
        check("morning delivers everything held, in due order, saying when each was for",
              said[-2:], ["⏰ call Keegan (this was for Tuesday at 5:00 PM)",
                          "⏰ take meds (this was for 1:00 AM)"])

        # -- a failed send is retried, not lost --------------------------------------
        await store.add_reminder(text="retry me", due_at=morning)
        fail["on"] = True
        failed = await reminders_job(ctx, morning + timedelta(minutes=1))
        check("a failed send says it will retry", "retrying" in failed.reason, True)
        fail["on"] = False
        await reminders_job(ctx, morning + timedelta(minutes=2))
        check("and the retry delivers it", said[-1], "⏰ retry me")

        # -- the bot: "remind me …", /remind, /reminders, /unremind -----------------
        from sloane.telegram import Bot

        class Agent:
            asked: list = []

            async def answer(self, question, **k):
                Agent.asked.append(question)

        bot = Bot(store, Agent(), config)
        bot._now = lambda: NOW
        sent = []

        async def capture(chat_id, reply):
            sent.append(reply)

        bot.send = capture
        await store._exec("truncate reminders")
        # Update ids are deduped against the messages table; start past anything
        # an earlier run left there.
        n = int(datetime.now().timestamp() * 1000) % 2_000_000_000

        async def message(text):
            nonlocal n
            n += 1
            await bot._handle({"update_id": n, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                                           "text": text}})
            return sent[-1]

        r = await message("Remind me at 5 to call Keegan")
        check("a plain 'remind me' sets it", r.speech, "Okay, I'll remind you at 5:00 PM: call Keegan.")
        check("without asking a model", Agent.asked, [])
        r = await message("/remind tomorrow 7am bring the lab")
        check("/remind sets it", r.speech, "Okay, I'll remind you tomorrow at 7:00 AM: bring the lab.")
        r = await message("remind me to read chapter 5")
        check("no time: she asks rather than guessing", r.speech, "I couldn't tell when you want that reminder.")
        r = await message("/reminders")
        check("/reminders lists them in order", r.detail.splitlines()[:2],
              ["1. at 5:00 PM: call Keegan", "2. tomorrow at 7:00 AM: bring the lab"])
        r = await message("/unremind 1")
        check("/unremind cancels by number", r.speech, "Cancelled: call Keegan.")
        check("and it is gone", [x["text"] for x in await store.upcoming_reminders()], ["bring the lab"])
        r = await message("/unremind 9")
        check("a bad number is refused", r.speech, "Use /unremind with a number from /reminders.")


asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("reminders: parser, claim-once, quiet hours, retry and the bot paths all pass")
