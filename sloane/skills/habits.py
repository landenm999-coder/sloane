"""Habits: the things he means to do every day, and the streak.

    /habit add reading  ·  /habit drop reading  ·  /habits
    /did reading  ·  /did reading yesterday  ·  /habit undo reading
    "did reading"  ·  "done with gym"  ·  "finished my stretches today"

A plain "did <x>" is only taken when <x> is one of his habits, so "did you get
my email?" still reaches the agent. Streaks are counted in code from the days
logged: a streak runs through today if today is done, or through yesterday if
it isn't done yet -- the day isn't over. Once in the evening (8:30-10 PM) the
heartbeat mentions any streak of two days or more that ends at midnight unless
he does it, and nothing else: a habit tracker that nags gets muted.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

MAX_NAME = 60
HISTORY_DAYS = 400  # long enough for a year-long streak
FACTS_HABITS = 10

_DID = re.compile(
    r"^\s*(?:i\s+)?(?:did|done|finished|completed|knocked\s+out)\s+(?:with\s+)?(?:my\s+|the\s+)?"
    r"(?P<name>[\w' -]+?)(?:\s+(?:today|just\s+now|already))?\s*[.!]*\s*$",
    re.I,
)


def _words(text: str) -> tuple[str, ...]:
    return tuple(w for w in re.findall(r"\w+", text.lower()) if w not in {"my", "the", "a"})


def streak(days: set[date], today: date) -> int:
    """Consecutive days ending today, or ending yesterday while today is open."""
    cursor = today if today in days else today - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def best(days: set[date]) -> int:
    top = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
        top = max(top, run)
        previous = d
    return top


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


class Habits(Skill):
    name = "habits"
    help = (
        "`/habit add reading` — track a daily habit; `/habits` streaks",
        "`/did reading` (or just say \"did reading\") — mark it done today",
    )
    commands = frozenset({"habit", "habits", "did"})

    async def _state(self) -> tuple[list[dict], dict[str, set[date]]]:
        habits = await self.ctx.store.active_habits()
        since = self.ctx.today() - timedelta(days=HISTORY_DAYS)
        logged: dict[str, set[date]] = {str(h["id"]): set() for h in habits}
        for row in await self.ctx.store.habit_days(since):
            logged.setdefault(str(row["habit_id"]), set()).add(row["on_date"])
        return habits, logged

    def _find(self, habits: list[dict], raw: str) -> dict | None:
        """A habit by its name, or the one habit whose name starts with these words."""
        want = _words(raw)
        if not want:
            return None
        exact = [h for h in habits if _words(h["name"]) == want]
        if exact:
            return exact[0]
        starts = [h for h in habits if _words(h["name"])[: len(want)] == want]
        return starts[0] if len(starts) == 1 else None

    async def mark(self, raw: str, *, undo: bool = False) -> Answer:
        habits, logged = await self._state()
        if not habits:
            return Answer("You aren't tracking any habits yet.", "Start one with `/habit add reading`.")
        today = self.ctx.today()
        on = today
        found = dates.find(raw, today, future=False)
        if found is not None and 0 <= (today - found.day).days <= 7:
            on, raw = found.day, dates.remove(raw, found)
        elif found is not None:
            return Answer("I can only mark habits for the last week.")
        habit = self._find(habits, raw)
        if habit is None:
            names = ", ".join(h["name"] for h in habits)
            return Answer(f"Which habit? You track {names}.")
        hid = str(habit["id"])
        when = "" if on == today else f" for {dates.spoken(on, today)}"
        if undo:
            if not await self.ctx.store.unlog_habit(hid, on):
                return Answer(f"{habit['name'].capitalize()} wasn't marked{when or ' today'}.")
            logged[hid].discard(on)
            return Answer(f"Unmarked {habit['name']}{when}. Streak: {_days(streak(logged[hid], today))}.")
        fresh = await self.ctx.store.log_habit(hid, on)
        logged[hid].add(on)
        run = streak(logged[hid], today)
        if not fresh:
            return Answer(f"{habit['name'].capitalize()} was already done{when or ' today'}. Streak: {_days(run)}.")
        cheer = " New best." if run > 1 and run > best(logged[hid] - {on}) else ""
        return Answer(f"Marked {habit['name']}{when}. Streak: {_days(run)}.{cheer}")

    async def listing(self) -> Answer:
        habits, logged = await self._state()
        if not habits:
            return Answer("You aren't tracking any habits yet.", "Start one with `/habit add reading`.")
        today = self.ctx.today()
        lines, left = [], []
        for h in habits:
            days = logged[str(h["id"])]
            done = today in days
            if not done:
                left.append(h["name"])
            week = sum(1 for i in range(7) if today - timedelta(days=i) in days)
            lines.append(f"{'✓' if done else '·'} {h['name']}: {_days(streak(days, today))} "
                         f"(best {best(days)}, {week}/7 this week)")
        speech = ("All done today." if not left
                  else f"{len(left)} left today: {', '.join(left)}.")
        return Answer(speech, "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "did":
            if not rest:
                return await self.listing()
            return await self.mark(rest)
        if name == "habits" or not rest:
            return await self.listing()
        verb, _, tail = rest.partition(" ")
        verb = verb.lower()
        if verb in {"add", "new", "track"}:
            label = safe_field(tail, limit=MAX_NAME).strip(" .")
            if not _words(label):
                return Answer("What habit? Try /habit add reading.")
            row = await self.ctx.store.add_habit(label)
            if row is None:
                return Answer(f"You already track {label}.")
            return Answer(f"Tracking {label}. Say \"did {label}\" when it's done.")
        if verb in {"drop", "remove", "stop"}:
            habits = await self.ctx.store.active_habits()
            habit = self._find(habits, tail)
            if habit is None:
                return Answer("Which habit?", ", ".join(h["name"] for h in habits))
            await self.ctx.store.archive_habit(str(habit["id"]))
            return Answer(f"Stopped tracking {habit['name']}.")
        if verb in {"undo", "unmark"}:
            return await self.mark(tail, undo=True)
        if verb in {"did", "done"}:
            return await self.mark(tail)
        return Answer("Try /habit add <name>, /habit drop <name>, /habit undo <name> or /habits.")

    async def match(self, text: str) -> Answer | None:
        found = _DID.match(text.replace("’", "'"))
        if found is None:
            return None
        habits = await self.ctx.store.active_habits()
        named = found["name"]
        when = dates.find(named, self.ctx.today(), future=False)
        if self._find(habits, dates.remove(named, when) if when else named) is None:
            return None  # "did you ..." and anything else that isn't a habit: the agent's
        return await self.mark(named)

    async def facts(self) -> list[str]:
        habits, logged = await self._state()
        today = self.ctx.today()
        lines = []
        for h in habits[:FACTS_HABITS]:
            days = logged[str(h["id"])]
            run = streak(days, today)
            state = "done today" if today in days else "not done yet today"
            lines.append(f"- HABIT {safe_field(h['name'], limit=MAX_NAME)}: {_days(run)} streak, {state}")
        return lines

    async def panel(self) -> dict | None:
        habits, logged = await self._state()
        if not habits:
            return None
        today = self.ctx.today()
        return {"title": "Habits", "lines": [
            f"{'✓' if today in logged[str(h['id'])] else '·'} {h['name']} — "
            f"{_days(streak(logged[str(h['id'])], today))}" for h in habits]}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        if not 20 * 60 + 30 <= now.hour * 60 + now.minute < 22 * 60:
            return []
        habits, logged = await self._state()
        today = now.date()
        at_risk = [(h["name"], streak(logged[str(h["id"])], today)) for h in habits
                   if today not in logged[str(h["id"])] and streak(logged[str(h["id"])], today) >= 2]
        if not at_risk:
            return []
        if len(at_risk) == 1:
            label, run = at_risk[0]
            text = f"🔥 {label.capitalize()} isn't done today; your {run}-day streak ends at midnight."
        else:
            text = "🔥 Streaks ending at midnight: " + ", ".join(f"{n} ({r} days)" for n, r in at_risk) + "."
        return [Nudge(f"habits:{today}", text)]


def build(ctx: SkillContext) -> Skill:
    return Habits(ctx)
