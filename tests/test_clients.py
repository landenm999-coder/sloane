"""The clients skill: the pipeline, follow-ups, stages, money parsing, FACTS.

DESTRUCTIVE: truncates clients and client_notes.
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
from sloane.skills.clients import Clients, money, parse_money

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


check("money", [money(120000), money(99950), money(None)], ["$1,200", "$999.50", ""])
check("parse $1,200", parse_money("Bella $1,200 follow up")[0], 120000)
check("parse $1.5k", parse_money("$1.5k")[0], 150000)
check("parse 800 dollars", parse_money("about 800 dollars")[0], 80000)
check("a bare number is not money", parse_money("Bella 1200"), None)


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 9, 0, tzinfo=DEN)}  # Thursday
    async with Store(config) as store:
        await store._exec("truncate clients, client_notes cascade")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        clients = Clients(ctx)
        reg = Registry([clients], ctx)

        async def cmd(rest, name="client"):
            return await reg.command(name, rest)

        check("empty pipeline", (await cmd("", "clients")).speech, "No open clients.")
        added = await cmd("add Bella's Bakery $1200 follow up friday: send mockups")
        check("add with value and follow-up", (added.speech, added.detail),
              ("Added Bella's Bakery as a lead.", "Bella's Bakery: lead, $1,200, follow up tomorrow (send mockups)"))
        check("add plain", (await cmd("add Peak Plumbing")).speech, "Added Peak Plumbing as a lead.")
        check("add a duplicate", (await cmd("add bella's bakery")).speech, "bella's bakery is already in your pipeline.")
        await cmd("add Parker Dental $2.5k follow up today: call back about hosting")

        listing = await cmd("", "clients")
        check("pipeline speech", listing.speech, "3 open clients, $3,700 in the pipeline; 1 follow-up due.")
        check("numbered with follow-ups first", listing.detail.splitlines(), [
            "1. Parker Dental — lead, $2,500, follow up today (call back about hosting)",
            "2. Bella's Bakery — lead, $1,200, follow up tomorrow (send mockups)",
            "3. Peak Plumbing — lead",
        ])

        check("stage by number", (await cmd("3 talking")).speech, "Peak Plumbing is now talking.")
        check("stage by name prefix, with a synonym", (await cmd("bella signed")).speech, "Bella's Bakery is now building.")
        check("value", (await cmd("peak $800")).speech, "Peak Plumbing is worth $800.")
        check("follow-up with a step", (await cmd("peak follow up next tue: send the quote")).speech,
              "Follow up with Peak Plumbing Tuesday: send the quote.")
        check("a note", (await cmd("bella note wants online ordering")).speech, "Noted on Bella's Bakery.")
        shown = await cmd("bella")
        check("one client", shown.speech, "Bella's Bakery: building, $1,200, follow up tomorrow (send mockups).")
        check("with notes", "wants online ordering" in shown.detail, True)
        check("unknown client", (await cmd("zed building")).speech,
              "Which client? Use the number from /clients or the start of the name.")
        check("nonsense action", (await cmd("peak banana")).speech, "I didn't follow that.")

        facts = await clients.facts()
        check("FACTS", facts, [
            "- CLIENTS 3 open, $4,500 in the pipeline",
            "- CLIENT Parker Dental: lead, $2,500, follow up today (call back about hosting)",
            "- CLIENT Bella's Bakery: building, $1,200, follow up tomorrow (send mockups)",
            "- CLIENT Peak Plumbing: talking, $800, follow up Tuesday (send the quote)",
        ])
        nudges = await clients.nudges()
        check("morning nudge for today's follow-ups", [n.text for n in nudges],
              ["💼 Follow up today: Parker Dental (call back about hosting)."])
        check("followed up", (await cmd("parker done")).speech, "Follow-up with Parker Dental done.")
        check("nothing to nudge", await clients.nudges(), [])

        # Plain words.
        check("follow up with a client", (await reg.route("follow up with parker dental on monday")).speech,
              "Follow up with Parker Dental Monday.")
        check("follow up with someone else: the agent's", await reg.route("follow up with Keegan friday"), None)

        now["at"] += timedelta(days=2)  # Saturday: Bella's follow-up (Friday) is overdue
        check("overdue shows as overdue", (await cmd("bella")).speech,
              "Bella's Bakery: building, $1,200, follow-up overdue since yesterday (send mockups).")
        check("paid clears the follow-up", (await cmd("bella paid")).speech,
              "Bella's Bakery is now paid. Nice work. Its follow-up is cleared.")
        listing = await cmd("", "clients")
        check("paid leaves the pipeline", listing.speech, "2 open clients, $3,300 in the pipeline.")
        check("and counts as paid this month", listing.detail.splitlines()[-1], "Paid this month: $1,200 (Bella's Bakery)")
        check("a closed client can still be named", (await cmd("bella")).speech, "Bella's Bakery: paid, $1,200.")
        check("drop", (await cmd("peak drop")).speech, "Took Peak Plumbing off the pipeline.")
        check("panel", (await clients.panel())["lines"], ["Lead: Parker Dental"])
        now["at"] = datetime(2026, 9, 28, 15, 0, tzinfo=DEN)
        check("no nudge in the afternoon", await clients.nudges(), [])
        _ = date


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("clients: pipeline, stages, follow-ups, notes, money, FACTS and the morning nudge")
