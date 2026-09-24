"""The lists skill: rules that must not over-reach, and the lists themselves.

DESTRUCTIVE: truncates list_items.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.lists import _ADD, _REMOVE, _SHOW, Lists, list_name, split_items

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- pure ----------------------------------------------------------------------
check("list names", [list_name(n) for n in ["My Groceries", "grocery", "to-do", "the Packing"]],
      ["grocery", "grocery", "todo", "packing"])
check("items split on commas and 'and'", split_items("milk, eggs, and some bread"), ["milk", "eggs", "bread"])
check("items split on '&' and '+'", split_items("chips & salsa + limes"), ["chips", "salsa", "limes"])
check("one item stays whole", split_items("AA batteries."), ["AA batteries"])
check("& inside a word, and a dish with 'and' in it", split_items("m&ms and mac and cheese"), ["m&ms", "mac and cheese"])
check("commas then a final and", split_items("chips, salt and pepper, and limes"), ["chips", "salt and pepper", "limes"])

ADD = _ADD.match("Add milk, eggs and bread to my grocery list.")
check("add: items", ADD["items"], "milk, eggs and bread")
check("add: list", ADD["list"], "grocery")
check("put ... on the ... list", _ADD.match("put phone charger on the packing list")["list"], "packing")
check("a two-word list name", _ADD.match("add Keegan to my DECA call list")["list"], "DECA call")
check("cross X off", _REMOVE.match("cross milk off my grocery list")["items"], "milk")
check("check off N on", _REMOVE.match("check off 2 on the grocery list")["items"], "2")
check("remove X from", _REMOVE.match("remove the eggs from grocery list")["items"], "the eggs")
check("what's on", _SHOW.match("what's on my grocery list?")["list"], "grocery")
check("show me", _SHOW.match("show me the packing list")["list"], "packing")


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    async with Store(config) as store:
        await store._exec("truncate list_items")
        ctx = SkillContext(store=store, config=config)
        lists = Lists(ctx)
        reg = Registry([lists], ctx)

        async def say(text):
            answer = await reg.route(text)
            return None if answer is None else answer.speech

        check("nothing on a list yet", await say("what lists do I have?"), "You don't have anything on a list.")
        check("add three", await say("Add milk, eggs and bread to my grocery list."),
              "Added milk, eggs and bread to your grocery list; 3 things on it now.")
        check("a duplicate is not doubled", await say("add Milk and butter to my groceries list"),
              "Added butter to your grocery list; 4 things on it now.")
        check("only duplicates", await say("add milk to the grocery list"), "Milk is already on your grocery list.")
        check("show", await say("what’s on my grocery list?"),
              "4 things on your grocery list: milk, eggs, bread and butter.")
        check("check off by name", await say("cross the milk off my grocery list"),
              "Checked off milk; 3 things left on your grocery list.")
        check("check off by number, and say what isn't there", await say("check off 1 and kale on the grocery list"),
              "Checked off eggs; 2 things left on your grocery list. I don't see kale on it.")
        await say("put phone charger, AirPods on the packing list")
        facts = await lists.facts()
        check("FACTS: one exact line per list", facts,
              ["- LIST grocery (2 open): bread, butter", "- LIST packing (2 open): phone charger, AirPods"])
        panel = await lists.panel()
        check("panel lines", panel["lines"], ["Grocery: bread, butter", "Packing: phone charger, AirPods"])
        check("all lists", (await reg.command("list", "")).detail, "• grocery: 2\n• packing: 2")
        check("/list <name>", (await reg.command("list", "packing")).detail,
              "**Packing**\n1. phone charger\n2. AirPods")
        check("/list add", (await reg.command("list", "add todo: email Mr. B, print lab")).speech,
              "Added email Mr. B and print lab to your todo list; 2 things on it now.")
        check("/list done", (await reg.command("list", "done todo 1")).speech,
              "Checked off email Mr. B; 1 thing left on your todo list.")
        check("clear", await say("clear the packing list"), "Cleared your packing list: 2 things checked off.")
        check("clear again", await say("clear the packing list"), "Your packing list was already empty.")
        rows = await store._fetch("select count(*) as n from list_items")
        check("nothing was deleted", rows[0]["n"], 8)

        # Must not over-reach: these belong to the agent.
        for text in ["add a conclusion to my essay", "put it on the list", "what's on my calendar?",
                     "I need to add bread to the grocery list later, remind me",
                     "is the reading list due friday?"]:
            check(f"not ours: {text!r}", await say(text), None)


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("lists: add, show, check off, clear, FACTS and panel; rules that don't over-reach")
