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

late = datetime(2026, 9, 22, 21, 0, tzinfo=DEN)
check("'tonight' after 8 PM is later tonight", parse("tonight call mom", late).due.strftime("%H:%M"), "22:00")
check("and at 11:30 PM there is no tonight left to set", parse("tonight call mom", late.replace(hour=23, minute=30)), None)

# Across the November DST change the wall clock holds: "tomorrow 7am" is 7 AM.
dst_eve = datetime(2026, 10, 31, 22, 0, tzinfo=DEN)
check("DST: tomorrow 7am stays 7 AM local",
      parse("tomorrow 7am run", dst_eve).due.strftime("%H:%M %Z"), "07:00 MST")

from sloane.reminders import parse_snooze, snooze_data, snoozed_until

RID = "0f8e3d0e-1c2b-4a5b-9c8d-7e6f5a4b3c2d"
check("snooze data round-trips", parse_snooze(snooze_data(RID, "60")), (RID, "60"))
check("forged snooze data is nothing", [parse_snooze(x) for x in
      ("r:not-a-uuid:10", f"r:{RID}:9999", f"p:{RID}:a", "", f"r:{RID}:10:extra")], [None] * 5)
check("snooze 10", snoozed_until("10", NOW).strftime("%H:%M"), "10:10")
check("snooze tomorrow is 7 AM", snoozed_until("tom", NOW).strftime("%a %H:%M"), "Wed 07:00")
check("done is not a snooze", snoozed_until("ok", NOW), None)

# -- repeating: (text) -> (first "Day MM-DD HH:MM", what, rule) ------------------
REPEATS = {
    "every weekday at 7am take out the trash": ("Wed 09-23 07:00", "take out the trash", "days:0,1,2,3,4"),
    "to take my meds every day at 9pm": ("Tue 09-22 21:00", "take my meds", "days:0,1,2,3,4,5,6"),
    "every monday and thursday at 6 to lift": ("Thu 09-24 18:00", "lift", "days:0,3"),
    "every night at 10 to charge my phone": ("Tue 09-22 22:00", "charge my phone", "days:0,1,2,3,4,5,6"),
    "every 2 hours drink water": ("Tue 09-22 12:00", "drink water", "hours:2"),
    "on the 1st of every month to pay rent": ("Thu 10-01 07:00", "pay rent", "month:1"),
    "on the 30th of every month to invoice": ("Wed 09-30 07:00", "invoice", "month:30"),
    "to stretch daily": ("Wed 09-23 07:00", "stretch", "days:0,1,2,3,4,5,6"),
    "to call grandma sundays at 4": ("Sun 09-27 16:00", "call grandma", "days:6"),
    "every other day at 8pm to water the plants": ("Tue 09-22 20:00", "water the plants", "every:2"),
    "every day at 12 to eat": ("Tue 09-22 12:00", "eat", "days:0,1,2,3,4,5,6"),
    "weeknights at 9 to pack my bag": ("Tue 09-22 21:00", "pack my bag", "days:0,1,2,3,4"),
    "every week to review my goals": ("Tue 09-29 07:00", "review my goals", "days:1"),
}
for text, want in REPEATS.items():
    got = parse(text, NOW)
    check(f"repeat {text!r}", (got.due.strftime("%a %m-%d %H:%M"), got.text, got.repeat) if got else None, want)
for text, want in {"at 5 to do my daily reading": ("Tue 09-22 17:00", "do my daily reading"),
                   "in 20 minutes check every hour": ("Tue 09-22 10:20", "check every hour"),
                   "at 5 to review every week's plan": ("Tue 09-22 17:00", "review every week's plan")}.items():
    got = parse(text, NOW)
    check(f"not a repeat: {text!r}", (got.due.strftime("%a %m-%d %H:%M"), got.text, got.repeat) if got else None,
          (*want, None))

from datetime import time as clock_time  # noqa: E402

from sloane.reminders import next_due, parse_timer, repeat_spoken  # noqa: E402

check("spoken: weekdays", repeat_spoken("days:0,1,2,3,4", NOW.replace(hour=7)), "every weekday at 7:00 AM")
check("spoken: some days", repeat_spoken("days:0,3", NOW.replace(hour=18)), "every Monday and Thursday at 6:00 PM")
check("spoken: hours", repeat_spoken("hours:2", NOW), "every 2 hours")
check("spoken: monthly", repeat_spoken("month:1", NOW.replace(hour=7)), "on the 1st of every month at 7:00 AM")
check("spoken: fortnightly", repeat_spoken("every:14", NOW.replace(hour=7)), "every 2 weeks at 7:00 AM")
before_dst = datetime(2026, 10, 31, 7, 0, tzinfo=DEN)
check("daily across the DST change keeps 7 AM", next_due("days:0,1,2,3,4,5,6", before_dst).strftime("%m-%d %H:%M %Z"),
      "11-01 07:00 MST")
check("every 2 hours skips quiet hours", next_due("hours:2", NOW.replace(hour=23)).strftime("%a %H:%M"), "Wed 06:30")
check("and a wrapped quiet window", next_due("hours:2", NOW.replace(hour=21), quiet=(clock_time(22), clock_time(7)))
      .strftime("%a %H:%M"), "Wed 07:00")
jan31 = datetime(2027, 1, 31, 9, 0, tzinfo=DEN)
check("the 31st in February is the 28th", next_due("month:31", jan31).strftime("%m-%d"), "02-28")
check("and back to the 31st in March", next_due("month:31", next_due("month:31", jan31)).strftime("%m-%d"), "03-31")
check("weekdays skip the weekend", next_due("days:0,1,2,3,4", datetime(2026, 9, 25, 7, 0, tzinfo=DEN))
      .strftime("%a %m-%d"), "Mon 09-28")

# -- timers ------------------------------------------------------------------------
TIMERS = {
    "set a timer for 10 minutes": ("10:10:00", ""),
    "timer 25 min": ("10:25:00", ""),
    "10 minute timer for the pasta": ("10:10:00", "the pasta"),
    "set a timer for ten minutes": ("10:10:00", ""),
    "start a timer for an hour": ("11:00:00", ""),
    "timer for 90 seconds": ("10:01:30", ""),
    "Sloane, set a timer for 5 min to flip the chicken": ("10:05:00", "flip the chicken"),
}
for text, want in TIMERS.items():
    got = parse_timer(text, NOW)
    check(f"timer {text!r}", (got.due.strftime("%H:%M:%S"), got.text) if got else None, want)
for text in ("timer", "what's a timer?", "set a timer for 0 minutes", "set a timer for 20 hours", "the timer went off"):
    check(f"not a timer: {text!r}", parse_timer(text, NOW), None)

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
        # -- snooze buttons ---------------------------------------------------------
        calls = []

        async def fake_call(client, method, **kw):
            calls.append((method, kw))
            return {"message_id": 7}

        bot._call = fake_call
        row = await store.add_reminder(text="stretch", due_at=NOW)
        await bot.remind("⏰ stretch", str(row["id"]))
        keyboard = calls[-1][1]["reply_markup"]["inline_keyboard"][0]
        check("a delivered reminder carries snooze buttons",
              [b["text"] for b in keyboard], ["10 min", "1 hour", "Tomorrow 7am", "Done"])

        def press(data, who=42):
            nonlocal n
            n += 1
            return {"update_id": n, "callback_query": {
                "id": "cb", "from": {"id": who}, "data": data,
                "message": {"chat": {"id": 42}, "message_id": 7}}}

        await bot._handle_callback(press(keyboard[1]["callback_data"]))
        check("snooze 1 hour says when", sent[-1].speech, "Snoozed: I'll remind you at 11:00 AM.")
        again = [x for x in await store.upcoming_reminders() if x["source"] == "snooze"]
        check("and it comes back", [x["due_at"].astimezone(DEN).strftime("%H:%M") for x in again], ["11:00"])
        check("the buttons come off", calls[-2][0] if calls[-1][0] == "sendMessage" else calls[-1][0],
              "editMessageReplyMarkup")
        await bot._handle_callback(press(keyboard[0]["callback_data"]))
        check("a double tap snoozes once",
              len([x for x in await store.upcoming_reminders() if x["source"] == "snooze"]), 1)
        before = len(await store.upcoming_reminders())
        await bot._handle_callback(press(keyboard[0]["callback_data"], who=99))
        check("someone else's press does nothing", len(await store.upcoming_reminders()), before)

        r = await message("/unremind 9")
        check("a bad number is refused", r.speech, "Use /unremind with a number from /reminders.")

        # -- repeating: delivering one makes the next; once; missed ones collapse ------
        await store._exec("truncate reminders")
        said.clear()
        r = await message("remind me every weekday at 7am to take out the trash")
        check("a repeating one is confirmed with its rule and first time", r.speech,
              "Okay, every weekday at 7:00 AM: take out the trash. First one tomorrow at 7:00 AM.")
        r = await message("/reminders")
        check("/reminders marks it repeating", r.detail.splitlines()[0],
              "1. tomorrow at 7:00 AM: take out the trash 🔁 every weekday at 7:00 AM")
        wed = datetime(2026, 9, 23, 7, 0, tzinfo=DEN)
        await reminders_job(ctx, wed)
        check("delivered on the day", said, ["⏰ take out the trash"])
        rows = await store.upcoming_reminders()
        check("and the next one is Thursday 7 AM, same series",
              [(x["due_at"].astimezone(DEN).strftime("%a %H:%M"), x["repeat"]) for x in rows],
              [("Thu 07:00", "days:0,1,2,3,4")])
        series = await store._fetch("select distinct series_id from reminders")
        check("one series", len(series), 1)
        # The same row delivered again (a retry after a failed send) doesn't double the next.
        first = await store._one("select * from reminders where sent_at is not null")
        await store.add_next_reminder(first, rows[0]["due_at"])
        check("the next is made once", len(await store.upcoming_reminders()), 1)
        # The box was off Thursday to Monday: one delivery, then the next still ahead.
        said.clear()
        await reminders_job(ctx, datetime(2026, 9, 28, 10, 0, tzinfo=DEN))
        check("a missed run is one late delivery", said, ["⏰ take out the trash (this was for Thursday at 7:00 AM)"])
        check("and the series picks up ahead", [x["due_at"].astimezone(DEN).strftime("%a %m-%d %H:%M")
                                                for x in await store.upcoming_reminders()], ["Tue 09-29 07:00"])
        r = await message("/unremind 1")
        check("/unremind stops the series", r.speech, "Stopped: take out the trash. It won't repeat.")
        said.clear()
        await reminders_job(ctx, datetime(2026, 9, 29, 7, 1, tzinfo=DEN))
        check("and nothing comes after", (said, await store.upcoming_reminders()), ([], []))

        # -- a timer: a reminder woken on the second -------------------------------------
        woke: list[str] = []

        async def run_job(name):
            woke.append(name)

        bot.run_job = run_job
        r = await message("set a timer for 10 minutes for the pasta")
        check("a timer is confirmed", r.speech, "Timer set: 10 minutes for the pasta, done at 10:10 AM.")
        check("and is a reminder", [x["text"] for x in await store.upcoming_reminders()], ["⏲️ Time's up: the pasta"])
        check("with its wake-up scheduled", len(bot._timers), 1)
        for task in list(bot._timers):
            task.cancel()
        bot._now = lambda: datetime.now(DEN)
        await message("timer 1 second")
        await asyncio.sleep(2.0)
        check("a short timer wakes the reminders job on time", woke, ["reminders"])
        bot._now = lambda: NOW


asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("reminders: parser, claim-once, quiet hours, retry and the bot paths all pass")
