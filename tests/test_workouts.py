"""Workouts: logged from his words by rule, the week against his goal, FACTS and the panel.

DESTRUCTIVE: truncates workouts and the workouts skill's settings.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.workouts import Workouts, amounts, spoken_length

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


check("miles and minutes", amounts("3 miles in 28 minutes"), (28, 4828.0))
check("an hour and a half", amounts("an hour and a half of yoga"), (90, None))
check("hours and minutes add up", amounts("1 hour 15 min"), (75, None))
check("a 5k", amounts("did a 5k"), (None, 5000.0))
check("no amount", amounts("went for a run"), (None, None))
check("said aloud", [spoken_length(m) for m in (1, 45, 60, 90, 120, 75)],
      ["1 minute", "45 minutes", "an hour", "an hour and a half", "2 hours", "1 hour 15 minutes"])


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver", weather_units="fahrenheit")
    now = {"at": datetime(2026, 9, 30, 18, 0, tzinfo=DEN)}  # a Wednesday
    async with Store(config) as store:
        await store._exec("truncate workouts")
        await store._exec("delete from skill_settings where skill = 'workouts'")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        skill = Workouts(ctx)
        reg = Registry([skill], ctx)

        # A minute passes between his messages, as it would.
        async def say(text):
            now["at"] += timedelta(minutes=1)
            answer = await reg.route(text)
            return None if answer is None else answer.speech

        async def cmd(rest="", name="workout"):
            now["at"] += timedelta(minutes=1)
            return (await reg.command(name, rest)).speech

        check("nothing yet: no panel, no FACTS", (await skill.panel(), await skill.facts()), (None, []))
        check("nothing yet, asked", await say("did I work out this week?"), "No workouts yet this week.")

        # -- his words, by rule -------------------------------------------------------------
        check("a run with a distance", await say("I ran 3 miles"), "Logged a 3 mile run. That's 1 this week.")
        check("minutes of something", await say("just did 45 minutes of lifting"), "Logged a 45 minute lift. That's 2 this week.")
        check("the gym, for an hour", await say("went to the gym for an hour"), "Logged an hour-long lift. That's 3 this week.")
        check("for a run, no amount", await say("I went for a run this morning"), "Logged a run. That's 4 this week.")
        check("yesterday", await say("did an hour and a half of yoga yesterday"),
              "Logged an hour and a half of yoga yesterday. That's 5 this week.")
        check("worked out", await say("worked out for 30 min"), "Logged a 30 minute workout. That's 6 this week.")
        check("log a workout: whatever he calls it", await say("log a workout: pickleball 60"),
              "Logged an hour of pickleball. That's 7 this week.")

        # -- and not these -------------------------------------------------------------------
        for text in ("I ran into Keegan at lunch", "I ran 10 minutes late", "did 20 minutes of homework",
                     "did the dishes", "should I run 3 miles today?", "I'm going to run 3 miles", "went on a date",
                     "went for a ride with Keegan", "remind me to run at 6", "did you log my run?",
                     "I walked the dog", "had a 30 minute call"):
            check(f"not a workout: {text!r}", await say(text), None)

        # -- the goal and the week -----------------------------------------------------------------
        check("a goal", await cmd("goal 4"), "Goal: 4 workouts a week. 7 of 4 this week.")
        check("a silly goal", await cmd("goal 40"), "How many a week? Try /workout goal 4.")
        answer = await reg.command("workouts", "")
        check("the week, said", answer.speech, "7 workouts this week, of 4, 4 hours 45 minutes in all.")
        check("and listed, by day", answer.detail.splitlines()[:2], ["- Tue: yoga 1h 30m", "- Wed: run 3 mi"])
        check("the command, his way", await cmd("run 3.1 mi 26 min"), "Logged a 3.1 mile run. 8 of 4 this week.")
        check("undo takes back the last", await cmd("undo"), "Took back the run 3.1 mi · 26 min.")
        check("nothing gibberish", await cmd("???"), "What did you do? Try /workout run 3 mi, or /workout lift 45.")
        check("not a day long", await cmd("run 900 minutes"), "15h is longer than I can believe. Say it again with the minutes?")

        facts = await skill.facts()
        check("FACTS: the week, with the goal", facts[0].split(": ")[0:2],
              ["- WORKOUTS this week (Mon-Sun)", "7 of a 4 goal"])
        check("FACTS: yesterday's yoga is on Tuesday", "Tue yoga 1h 30m" in facts[0], True)

        panel = await skill.panel()
        check("panel: count, goal, today", (panel["count"], panel["goal"], [d["today"] for d in panel["days"]].index(True)),
              (7, 4, 2))
        check("panel: Monday to Sunday", [d["day"] for d in panel["days"]], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        check("panel: minutes by day", [d["minutes"] for d in panel["days"]][:3], [0, 90, 195])
        check("panel: four weeks, this one last", panel["weeks"], [0, 0, 0, 7])
        check("panel: distance in miles", panel["distance"], "3 mi")

        # -- next week: FACTS says none yet, and when the last was ---------------------------
        now["at"] = datetime(2026, 10, 6, 9, 0, tzinfo=DEN)
        check("a new week", await say("did I work out this week?"),
              "No workouts yet this week (goal 4). The last one was Wed Sep 30.")
        check("FACTS: none yet, and the last", (await skill.facts())[0].startswith(
            "- WORKOUTS this week (Mon-Sun): none yet (goal 4); the last was Wed Sep 30"), True)

        # -- Whoop's copy is logged once, however often it syncs -------------------------------
        first = await store.add_workout(kind="run", minutes=40, distance_m=6000, done_on=date(2026, 10, 5),
                                        logged_at=now["at"], source="whoop", source_id="w-1")
        again = await store.add_workout(kind="run", minutes=40, distance_m=6000, done_on=date(2026, 10, 5),
                                        logged_at=now["at"], source="whoop", source_id="w-1")
        check("a Whoop workout once", (first is not None, again), (True, None))
        check("and undo never takes back Whoop's", await cmd("undo"), "There's nothing you logged today to take back.")
        rows = await store.workouts_since(date(2026, 10, 5) - timedelta(days=0))
        check("it counts this week", len(rows), 1)
        rescored = await store.add_workout(kind="run", minutes=41, distance_m=6100, done_on=date(2026, 10, 5),
                                           logged_at=now["at"], source="whoop", source_id="w-1", strain=12.3,
                                           heart_rate=152)
        row = (await store.workouts_since(date(2026, 10, 5)))[0]
        check("Whoop rescores it: the numbers catch up, still one workout, still not new",
              (rescored, row["minutes"], float(row["strain"]), row["heart_rate"],
               len(await store.workouts_since(date(2026, 10, 5)))), (None, 41, 12.3, 152, 1))
        panel = await skill.panel()
        check("the panel says where it came from, its strain, and the latest three, newest first",
              (panel["from_whoop"], panel["strain"], panel["last"]["source"], panel["last"]["strain"],
               panel["last"]["heart_rate"], [r["text"] for r in panel["recent"]]),
              (1, 12.3, "whoop", 12.3, 152, ["run 3.8 mi · 41 min", "pickleball 1h", "workout 30 min"]))


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("workouts: logged by rule, the week, the goal, FACTS and the panel")
