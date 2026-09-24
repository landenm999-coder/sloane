"""Birthdays: the day, a heads-up before it, and nothing he has to remember.

    /birthday Keegan mar 3    ·   /birthdays     ·   /birthday Keegan forget
    "Keegan's birthday is March 3"   ·   "when is Keegan's birthday?"

Stored on `people` (month and day, no year), so the same Keegan his promises
point at. The heartbeat says it a week out, the evening before, and the
morning of. A Feb 29 birthday is kept on Feb 28 in other years. Birthdays in
the next two weeks are FACTS lines, so a brief can say "Keegan's birthday is
Saturday" without being asked.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

FACTS_DAYS = 14
PANEL_DAYS = 30
MAX_NAME = 60

_SET = re.compile(r"^\s*(?P<name>[A-Za-z][\w .'-]{0,40}?)['’]s\s+birthday\s+is\s+(?:on\s+)?(?P<when>.+?)\s*[.!]*\s*$", re.I)
_ASK = re.compile(r"^\s*when(?:'?s|\s+is)\s+(?P<name>[A-Za-z][\w .'-]{0,40}?)['’]s\s+birthday\s*\??\s*$", re.I)


def next_birthday(month: int, day: int, today: date) -> date:
    """The next time it comes round, today included. Feb 29 falls on Feb 28."""
    for year in (today.year, today.year + 1):
        try:
            when = date(year, month, day)
        except ValueError:
            when = date(year, month, 28)  # Feb 29 in a common year
        if when >= today:
            return when
    raise ValueError("unreachable")


def _in(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


class Birthdays(Skill):
    name = "birthdays"
    help = ("`/birthday Keegan mar 3` — remember a birthday; `/birthdays` what's coming",)
    commands = frozenset({"birthday", "birthdays"})

    async def _upcoming(self) -> list[tuple[dict, date, int]]:
        today = self.ctx.today()
        out = []
        for row in await self.ctx.store.birthdays():
            when = next_birthday(int(row["birth_month"]), int(row["birth_day"]), today)
            out.append((row, when, (when - today).days))
        return sorted(out, key=lambda item: item[2])

    async def remember_birthday(self, name: str, when_text: str) -> Answer:
        name = safe_field(name.strip(" ."), limit=MAX_NAME)
        found = dates.find(when_text, self.ctx.today())
        if not name or found is None:
            return Answer("Whose birthday, and when? Try /birthday Keegan mar 3.")
        matches = await self.ctx.store.find_people(name)
        person_id = str(matches[0]["id"]) if matches else await self.ctx.store.person_id(name)
        row = await self.ctx.store.set_birthday(person_id, found.day.month, found.day.day)
        days = (next_birthday(found.day.month, found.day.day, self.ctx.today()) - self.ctx.today()).days
        return Answer(f"Got it: {row['name']}'s birthday is {found.day:%B} {found.day.day}, {_in(days)}.")

    async def ask(self, name: str) -> Answer | None:
        matches = [m for m in await self.ctx.store.find_people(name.strip()) if m.get("birth_month")]
        if len(matches) != 1:
            return None  # not known here: the agent may still have something
        row = matches[0]
        today = self.ctx.today()
        when = next_birthday(int(row["birth_month"]), int(row["birth_day"]), today)
        return Answer(f"{row['name']}'s birthday is {when:%B} {when.day}, {_in((when - today).days)}.")

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "birthdays" or not rest:
            upcoming = await self._upcoming()
            if not upcoming:
                return Answer("I don't know anyone's birthday yet.", "Try `/birthday Keegan mar 3`.")
            today = self.ctx.today()
            lines = [f"• {row['name']}: {when:%b} {when.day} ({_in(days)})" for row, when, days in upcoming]
            first, when, days = upcoming[0]
            return Answer(f"Next up: {first['name']}, {_in(days)} on {dates.spoken(when, today)}.",
                          "\n".join(lines))
        words = rest.split()
        if len(words) >= 2 and words[-1].lower() in {"forget", "remove", "clear"}:
            matches = await self.ctx.store.find_people(" ".join(words[:-1]))
            if not matches:
                return Answer("I don't know who that is.")
            await self.ctx.store.set_birthday(str(matches[0]["id"]), None, None)
            return Answer(f"Forgot {matches[0]['name']}'s birthday.")
        found = dates.find(rest, self.ctx.today())
        if found is None:
            asked = await self.ask(rest)
            return asked or Answer("I don't have that birthday. Add it with /birthday <name> <date>.")
        return await self.remember_birthday(dates.remove(rest, found), rest[found.start:found.end])

    async def match(self, text: str) -> Answer | None:
        if found := _SET.match(text):
            if dates.find(found["when"], self.ctx.today()) is None:
                return None
            return await self.remember_birthday(found["name"], found["when"])
        if found := _ASK.match(text):
            return await self.ask(found["name"])
        return None

    async def facts(self) -> list[str]:
        today = self.ctx.today()
        return [f"- BIRTHDAY {safe_field(row['name'], limit=MAX_NAME)}: {when:%a %b} {when.day} ({_in(days)})"
                for row, when, days in await self._upcoming() if days <= FACTS_DAYS and when >= today]

    async def panel(self) -> dict | None:
        soon = [(row, when, days) for row, when, days in await self._upcoming() if days <= PANEL_DAYS]
        if not soon:
            return None
        return {"title": "Birthdays", "lines": [f"🎂 {row['name']}: {when:%b} {when.day} ({_in(days)})"
                                                for row, when, days in soon]}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        minutes = now.hour * 60 + now.minute
        out = []
        for row, when, days in await self._upcoming():
            key = f"birthday:{row['id']}:{when.isoformat()}"
            if days == 7:
                out.append(Nudge(f"{key}:7", f"🎂 {row['name']}'s birthday is a week from today ({when:%A}, {when:%b} {when.day})."))
            elif days == 1 and minutes >= 17 * 60:
                out.append(Nudge(f"{key}:1", f"🎂 {row['name']}'s birthday is tomorrow."))
            elif days == 0 and minutes < 12 * 60:
                out.append(Nudge(f"{key}:0", f"🎂 It's {row['name']}'s birthday today."))
        return out


def build(ctx: SkillContext) -> Skill:
    return Birthdays(ctx)
