"""Money: spending by rule, the weekly budget, estimated earnings from shifts.

DESTRUCTIVE: truncates expenses, skill_settings and shifts.
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
from sloane.skills.money import Money, category, dollars

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


check("categories", [category(w) for w in ["lunch at chipotle", "gas", "DECA fee", "domain renewal", "a gift"]],
      ["food", "gas", "school", "business", "other"])
check("dollars", [dollars(1200), dollars(450), dollars(123456)], ["$12", "$4.50", "$1,234.56"])


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver", pay_rate=15.0)
    now = {"at": datetime(2026, 9, 24, 20, 0, tzinfo=DEN)}  # Thursday evening
    async with Store(config) as store:
        await store._exec("truncate expenses, skill_settings, shifts, bank_accounts cascade")
        for day in (21, 22, 23, 24, 25):  # Mon-Fri 3-7
            await store.add_shift(datetime(2026, 9, day, 15, tzinfo=DEN), datetime(2026, 9, day, 19, tzinfo=DEN))
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        money = Money(ctx)
        reg = Registry([money], ctx)

        async def say(text):
            answer = await reg.route(text)
            return None if answer is None else answer.speech

        check("spent", await say("spent 12 on lunch"), "Logged $12 on lunch. $12 this week.")
        check("with cents and a place", await say("I spent $4.50 at starbucks yesterday."),
              "Logged $4.50 on starbucks (yesterday). $16.50 this week.")
        check("paid ... for", await say("paid 15 for the DECA fee"), "Logged $15 on the DECA fee. $31.50 this week.")
        check("hours are not dollars", await say("spent 3 hours on the essay"), None)
        check("not a purchase", await say("I spent the whole night studying"), None)
        check("budget", (await reg.command("budget", "35")).speech, "Weekly budget set to $35; you're at $31.50 this week.")
        check("80% nudge", [n.text for n in await money.nudges()], ["💸 $3.50 left of this week's $35 budget."])
        check("over budget", (await reg.command("spent", "20 gas")).speech,
              "Logged $20 on gas. $51.50 this week, $16.50 over your $35 budget.")
        check("over nudge", [n.key for n in await money.nudges()], ["money:2026-09-21:100"])
        check("undo", (await reg.command("spent", "undo")).speech, "Took back $20 on gas.")
        summary = await reg.command("spent", "")
        check("summary speech", summary.speech, "$31.50 spent this week, $3.50 left of your budget.")
        check("categories and earnings", summary.detail.splitlines()[:5], [
            "This week: $31.50 of $35",
            "  • food: $16.50",
            "  • school: $15",
            "This month: $31.50",
            "Earned this week (est.): $240 from 16 shift hours",
        ])
        check("FACTS", await money.facts(),
              ["- MONEY this week (since Mon Sep 21): spent $31.50 of a $35 budget; "
               "earned about $240 from 16 shift hours (estimate)"])
        check("budget off", (await reg.command("budget", "off")).speech, "Weekly budget cleared.")
        check("no nudges without a budget", await money.nudges(), [])
        check("panel", (await money.panel())["lines"], ["Spent this week: $31.50", "Earned (est.): $240"])
        drawn = await money.panel()
        check("the widget's numbers: the week, the budget (cleared), earnings, the latest first",
              (drawn["spent_cents"], drawn["budget_cents"], drawn["earned_cents"], len(drawn["recent"]) <= 4,
               drawn["recent"][0]["cents"] > 0), (3150, None, 24000, True, True))

        plain = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
        quiet = Money(SkillContext(store=store, config=plain, clock=lambda: now["at"]))
        check("no pay rate: no earnings line", "earned" in (await quiet.facts())[0], False)


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("money: spending by rule, categories, weekly budget and nudges, estimated earnings")
