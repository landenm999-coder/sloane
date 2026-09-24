"""The colleges skill: applications, plans, deadlines, checklists, FACTS, nudges.

DESTRUCTIVE: truncates colleges and college_tasks.
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

from sloane.skills import Registry, SkillContext
from sloane.skills.colleges import Colleges, _initials, _runs, find_plan

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- words, no database ---------------------------------------------------------------
check("plans", [(find_plan(t) or ("-",))[0] for t in (
    "CU Boulder EA nov 1", "early decision II jan 1", "ED2 jan 15", "ED 2/1", "restrictive early action",
    "regular decision", "rolling admission", "priority deadline", "Colorado School of Mines", "Reed College")],
    ["EA", "ED2", "ED2", "ED", "REA", "RD", "rolling", "priority", "-", "-"])
check("a school named by any run of its words", [_runs("University of Colorado Boulder", w, partial=False)
                                                  for w in (["boulder"], ["colorado", "boulder"], ["denver"])],
      [True, True, False])
check("a command may cut the last word short, not to two letters",
      [_runs("CU Boulder", ["boul"], partial=True), _runs("CU Boulder", ["bo"], partial=True),
       _runs("CU Boulder", ["boul"], partial=False)], [True, False, False])
check("initials skip the small words", [_initials("Colorado State University"), _initials("Colorado School of Mines")],
      ["csu", "csm"])


def item(task: str, state: str = "open", n: int = 0) -> dict:
    return {"id": n, "task": task, "state": state, "due_on": None}


checklist = [item(t, n=i) for i, t in enumerate(
    ("Application form", "Essays", "Recommendations", "Transcript", "Test scores", "Fee or waiver", "Portfolio"), 1)]
check("items by the words he'd use", [(Colleges._task(checklist, w)[0] or {}).get("task") for w in (
    "essays", "supplements", "recs", "letters of rec", "SAT", "fee", "port", "2", "the transcript")],
    ["Essays", "Essays", "Recommendations", "Recommendations", "Test scores", "Fee or waiver", "Portfolio",
     "Essays", "Transcript"])
check("an item that isn't there", Colleges._task(checklist, "interview"), (None, False))


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 9, 0, tzinfo=DEN)}  # Thursday
    async with Store(config) as store:
        await store._exec("truncate colleges, college_tasks cascade")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        colleges = Colleges(ctx)
        reg = Registry([colleges], ctx)

        async def cmd(rest, name="college"):
            return await reg.command(name, rest)

        check("nothing yet", (await cmd("", "colleges")).speech, "No college applications yet.")
        added = await cmd("add CU Boulder EA nov 1")
        check("add with plan and deadline", added.speech, "Added CU Boulder, Early Action, due Sun Nov 1, in 38 days.")
        check("the usual checklist comes with it", "Application form, Essays, Recommendations, Transcript, "
              "Test scores, Fee or waiver" in added.detail, True)
        check("a nickname in brackets", (await cmd("add Colorado State University (CSU) RD feb 1")).speech,
              "Added Colorado State University, Regular Decision, due Mon Feb 1, 2027, in 130 days.")
        check("a slash date", (await cmd("add Colorado School of Mines ED 11/15")).speech,
              "Added Colorado School of Mines, Early Decision, due Sun Nov 15, in 52 days.")
        check("no deadline yet: she asks", (await cmd("add Reed College")).speech,
              "Added Reed College. When's the deadline?")
        check("a duplicate", (await cmd("add cu boulder")).speech, "cu boulder is already on your list.")
        check("a deadline already gone", (await cmd("add Tulane EA 2026-09-01")).speech,
              "Tue Sep 1 has already passed.")

        listing = await cmd("", "colleges")
        check("the list speech", listing.speech, "4 applications; next up is CU Boulder EA, due Sun Nov 1.")
        check("numbered by deadline", listing.detail.splitlines(), [
            "1. CU Boulder — EA, due Sun Nov 1 (in 38 days) · 0 of 6 done",
            "2. Colorado School of Mines — ED, due Sun Nov 15 (in 52 days) · 0 of 6 done",
            "3. Colorado State University (CSU) — RD, due Mon Feb 1, 2027 (in 130 days) · 0 of 6 done",
            "4. Reed College — no deadline set · 0 of 6 done",
        ])

        # -- the checklist --------------------------------------------------------------
        check("tick by its words", (await cmd("boulder done essays")).speech,
              "Essays done for CU Boulder; left: application form, recommendations, transcript, test scores "
              "and fee or waiver.")
        check("the verb after the item", (await cmd("boulder recs done")).speech.split(";")[0],
              "Recommendations done for CU Boulder")
        check("'sent' an item is that item, not the application", (await cmd("boulder sent transcript")).speech
              .split(";")[0], "Transcript done for CU Boulder")
        check("skip what a school doesn't want", (await cmd("boulder skip SAT")).speech,
              "CU Boulder doesn't need test scores; off the checklist.")
        check("undo by number", (await cmd("boulder undo 2")).speech, "Essays is open again for CU Boulder.")
        check("an extra item with its own date", (await cmd("boulder add portfolio by oct 20")).speech,
              "Added portfolio to CU Boulder, due Tue Oct 20.")
        check("an item that isn't on it", (await cmd("boulder done interview")).speech, "Which item?")
        shown = await cmd("1")
        check("one school, by number", shown.speech,
              "CU Boulder is due Sun Nov 1; left: application form, essays, fee or waiver and portfolio.")
        check("its checklist", shown.detail.splitlines(), [
            "**CU Boulder** — Early Action, due Sun Nov 1 (in 38 days)",
            "1. ⬜ Application form", "2. ⬜ Essays", "3. ✅ Recommendations", "4. ✅ Transcript",
            "5. ➖ Test scores (not needed)", "6. ⬜ Fee or waiver", "7. ⬜ Portfolio (due Tue Oct 20)",
        ])

        # -- naming a school ------------------------------------------------------------------
        check("by nickname", (await cmd("csu")).speech.split(";")[0], "Colorado State University is due Mon Feb 1, 2027")
        check("by initials", (await cmd("csm")).speech.split(";")[0], "Colorado School of Mines is due Sun Nov 15")
        check("two schools match: she asks", (await cmd("colorado done essays")).speech,
              "Which one? More than one school matches.")
        check("no such school", (await cmd("harvard done essays")).speech,
              "Which school? Use the number from /colleges or part of its name.")

        # -- plan and deadline ------------------------------------------------------------------
        check("plan and deadline together", (await cmd("csu ED2 jan 15")).speech,
              "Colorado State University: Early Decision II, due Fri Jan 15, 2027, in 113 days.")
        check("just the deadline", (await cmd("mines deadline nov 1")).speech,
              "Colorado School of Mines: due Sun Nov 1, in 38 days.")
        check("reed gets a deadline", (await cmd("reed RD jan 15")).speech,
              "Reed College: Regular Decision, due Fri Jan 15, 2027, in 113 days.")

        # -- asked in words ------------------------------------------------------------------------
        check("what's left for a school", (await reg.route("what's left for Boulder?")).speech,
              "CU Boulder is due Sun Nov 1; left: application form, essays, fee or waiver and portfolio.")
        check("what's left on all of them", (await reg.route("What's left on my college apps?")).speech.split(";")[0],
              "4 applications")
        check("not a school: the agent's", await reg.route("what's left for my chem homework?"), None)

        # -- FACTS -------------------------------------------------------------------------------------
        facts = await colleges.facts()
        check("FACTS: exact dates, what's left", facts, [
            "- COLLEGES 4 applications: 4 in progress",
            "- COLLEGE CU Boulder (Early Action): due Sun Nov 1, 2026 (in 38 days); left: Application form, "
            "Essays, Fee or waiver, Portfolio (due Tue Oct 20, 2026)",
            "- COLLEGE Colorado School of Mines (Early Decision): due Sun Nov 1, 2026 (in 38 days); left: "
            "Application form, Essays, Recommendations, Transcript, Test scores, Fee or waiver",
            "- COLLEGE Colorado State University (CSU) (Early Decision II): due Fri Jan 15, 2027 (in 113 days); "
            "left: Application form, Essays, Recommendations, Transcript, Test scores, Fee or waiver",
            "- COLLEGE Reed College (Regular Decision): due Fri Jan 15, 2027 (in 113 days); left: Application "
            "form, Essays, Recommendations, Transcript, Test scores, Fee or waiver",
        ])

        # -- nudges: two weeks out in the evening, never in the morning --------------------------------
        now["at"] = datetime(2026, 10, 18, 9, 0, tzinfo=DEN)
        check("not in the morning", [n.key for n in await colleges.nudges()], [])
        now["at"] = datetime(2026, 10, 18, 19, 5, tzinfo=DEN)
        two_weeks = await colleges.nudges()
        check("two weeks out, both Nov 1 schools", len(two_weeks), 2)
        check("says what's left", two_weeks[0].text,
              "🎓 CU Boulder (Early Action) is due in 14 days, Sun Nov 1. Left: application form, essays, "
              "fee or waiver and portfolio.")
        now["at"] = datetime(2026, 10, 19, 19, 5, tzinfo=DEN)
        check("an item's own date, the day before", [n.text for n in await colleges.nudges()],
              ["🎓 Portfolio for CU Boulder is due tomorrow."])
        now["at"] = datetime(2026, 11, 1, 7, 5, tzinfo=DEN)
        check("the morning of", [n.text for n in await colleges.nudges()][0].startswith(
            "🎓 CU Boulder (Early Action) is due today."), True)

        # -- submitted, and after -----------------------------------------------------------------------
        now["at"] = datetime(2026, 10, 30, 20, 0, tzinfo=DEN)
        check("submitted, with a date, and what's still open", (await cmd("boulder submitted yesterday")).speech,
              "CU Boulder is in, submitted yesterday. Still open: essays, fee or waiver and portfolio.")
        tasks = [t for t in await store.active_college_tasks() if t["task"] == "Application form"]
        check("submitting ticks the application form", sorted(t["state"] for t in tasks), ["done", "open", "open", "open"])
        now["at"] = datetime(2026, 11, 1, 7, 5, tzinfo=DEN)
        check("no deadline nudge once it's in", [n.key for n in await colleges.nudges() if "boulder" in n.text.lower()], [])
        now["at"] = datetime(2026, 11, 2, 7, 5, tzinfo=DEN)
        late = [n.text for n in await colleges.nudges()]
        check("a deadline that passed unsubmitted: said once, the morning after", late,
              ["🎓 Colorado School of Mines's deadline was yesterday and it isn't marked submitted. "
               "If it went in: /college mines submitted"])
        check("'application sent' is the application going in", (await cmd("mines application sent")).speech,
              "Colorado School of Mines is in, submitted today. Still open: essays, recommendations, transcript, "
              "test scores and fee or waiver.")
        check("got in", (await cmd("boulder got in")).speech, "Admitted to CU Boulder. Congratulations.")
        check("got deferred", (await cmd("mines got deferred")).speech,
              "Colorado School of Mines deferred you. That isn't a no; it goes to the next round.")
        check("reopen", (await cmd("mines reopen")).speech, "Colorado School of Mines is back to in progress.")
        check("drop needs to be the whole instruction", (await cmd("reed drop it now")).speech, "I didn't follow that.")
        check("drop", (await cmd("reed drop")).speech, "Took Reed College off your list.")
        final = await cmd("", "colleges")
        check("after all that", final.detail.splitlines(), [
            "1. Colorado School of Mines — ED, deadline was Sun Nov 1 (yesterday), not marked submitted · 1 of 6 done",
            "2. Colorado State University (CSU) — ED II, due Fri Jan 15, 2027 (in 74 days) · 0 of 6 done",
            "3. CU Boulder — admitted",
        ])
        panel = await colleges.panel()
        check("the TV panel", panel, {"title": "Applications", "lines": [
            "Colorado School of Mines · ED · Nov 1 (late) · 1 of 6 done",
            "CSU · ED II · Jan 15 (74 days) · 0 of 6 done",
            "CU Boulder · admitted",
        ]})
        await store._exec("truncate colleges, college_tasks cascade")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("colleges: plans, deadlines, checklists by words or number, statuses, FACTS, nudges, panel")
