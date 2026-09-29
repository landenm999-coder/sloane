"""Workouts: what he did, how long, how far, and the week against his goal.

    "ran 3 miles"  ·  "biked for 45 minutes"  ·  "did an hour of yoga"
    "went for a run"  ·  "went to the gym for an hour"  ·  "worked out yesterday"
    "did I work out this week?"  ·  "how many workouts this week?"
    /workout run 3 mi 28 min  ·  /workout lift 45  ·  /workout goal 4  ·  /workout undo
    /workouts

A plain message is taken only when it is plainly a workout he did: a past-tense
verb with an amount ("ran 3 miles"; "I ran into Keegan" is not one), or a thing
that is only ever exercise ("went to the gym", "went for a run"). Questions and
plans ("should I run?", "I'm going to run") reach the agent. No model: logging a
run from a voice note in the car must work with every provider down.

Whoop workouts land here too (sloane/skills/whoop.py), once each. The week runs
Monday to Sunday, as the control room's week does.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sloane import dates
from sloane.skills import Answer, Skill, SkillContext

SKILL = "workouts"
MAX_MINUTES = 600
METERS_PER_MILE = 1609.344
WEEKS_SHOWN = 4

# What he'd say, and the one word it's filed under. Longer phrases first.
KINDS: dict[str, tuple[str, ...]] = {
    "run": ("half marathon", "marathon", "5k", "10k", "ran", "run", "runs", "running", "jog", "jogged", "jogging"),
    "walk": ("walked", "walk", "walking"),
    "bike": ("bike ride", "biked", "bike", "biking", "cycled", "cycling", "rode", "spin class", "spin", "peloton"),
    "swim": ("swam", "swim", "swimming"),
    "hike": ("hiked", "hike", "hiking"),
    "row": ("rowed", "row", "rowing"),
    "lift": ("leg day", "push day", "pull day", "weightlifting", "lifted", "lift", "lifting", "weights", "gym",
             "strength", "legs", "chest", "arms"),
    "yoga": ("yoga",), "pilates": ("pilates",), "climb": ("climbed", "climbing", "bouldering"),
    "hiit": ("hiit", "crossfit", "circuit"), "stretch": ("stretched", "stretching", "mobility"),
    "basketball": ("basketball",), "soccer": ("soccer",), "tennis": ("tennis",), "golf": ("golf",),
    "boxing": ("boxing",), "football": ("football",), "volleyball": ("volleyball",), "hockey": ("hockey",),
    "baseball": ("baseball",), "ski": ("skied", "skiing"), "snowboard": ("snowboarded", "snowboarding"),
    "dance": ("danced", "dance", "dancing"), "workout": ("workout", "worked out", "exercise", "exercised"),
}
_WORD_KIND = sorted(((w, k) for k, words in KINDS.items() for w in words), key=lambda p: -len(p[0]))
SAID = {"run": "run", "walk": "walk", "bike": "ride", "swim": "swim", "hike": "hike", "row": "row", "lift": "lift",
        "climb": "climb", "stretch": "stretch", "ski": "ski day", "snowboard": "snowboard day", "workout": "workout"}

_DISTANCE = re.compile(r"(?<![\w.])(?P<n>\d+(?:\.\d+)?)\s*-?\s*(?P<u>miles?|mi|kilomet(?:er|re)s?|km|k|met(?:er|re)s?)\b",
                       re.I)
_DURATION = re.compile(r"(?<![\w.])(?P<n>\d+(?:\.\d+)?)\s*-?\s*(?P<u>hours?|hrs?|hr|h|minutes?|mins?|min|m)\b", re.I)
_HOURS_SAID = ((re.compile(r"\ban?\s+hour\s+and\s+a\s+half\b", re.I), 90),
               (re.compile(r"\bhalf\s+an?\s+hour\b", re.I), 30),
               (re.compile(r"\ban?\s+hour\b", re.I), 60))
_WHEN = re.compile(r"\s*,?\s*\b(?P<when>today|this\s+morning|this\s+afternoon|this\s+evening|tonight|earlier(?:\s+today)?|"
                   r"yesterday(?:\s+(?:morning|afternoon|evening))?|last\s+night)\s*[.!]*\s*$", re.I)
_I = r"^\s*(?:i\s+|i'?ve\s+|i\s+have\s+)?(?:just\s+|finally\s+|already\s+)?"
_VERBED = re.compile(_I + r"(?P<verb>ran|jogged|walked|biked|cycled|swam|hiked|rowed|lifted|climbed|rode)\b(?P<rest>.+)$",
                     re.I | re.S)
_DID = re.compile(_I + r"(?:did|finished|completed|got\s+in|had)\s+(?!i\b|you\b|we\b)(?P<rest>.+)$", re.I | re.S)
_WENT = re.compile(_I + r"went\s+(?P<rest>(?:for|on)\s+an?\s+.+|to\s+(?:the\s+)?(?:gym|yoga|pilates|spin|practice)\b.*|"
                   r"(?:running|jogging|walking|biking|cycling|swimming|hiking|rowing|climbing|bouldering|lifting|skiing|"
                   r"snowboarding)\b.*)$", re.I | re.S)
_WORKED_OUT = re.compile(_I + r"(?:worked\s+out|hit\s+the\s+gym|exercised)\b(?P<rest>.*)$", re.I | re.S)
_LOG = re.compile(r"^\s*(?:please\s+)?(?:log|add|record)\s+(?:a\s+|my\s+)?workout\s*[:,-]?\s*(?P<rest>.+)$", re.I | re.S)
_SHOW = re.compile(
    r"^\s*(?:how\s+many\s+(?:times\s+(?:have\s+i|did\s+i)\s+(?:worked\s+out|work\s+out|exercise[d]?)|workouts"
    r"(?:\s+(?:have|did)\s+i\s+(?:done|had|do|have))?)|(?:did|have)\s+i\s+(?:work(?:ed)?\s*out|exercise[d]?)|"
    r"(?:show\s+(?:me\s+)?)?my\s+workouts|how'?s\s+my\s+training)"
    r"(?:\s+(?:this\s+week|today|yet|so\s+far(?:\s+this\s+week)?|lately))*\s*[?.!]*\s*$",
    re.I,
)
_NOT_EXERCISE = re.compile(r"\b(?:late|early|behind|over|short|errands?)\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^\s*(?:should|could|can|would|will|do|does|did\s+i|have\s+i|how|what|when|why|is|are)\b",
                       re.I)


def _kind(text: str) -> str | None:
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    for word, kind in _WORD_KIND:
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low):
            return kind
    return None


def amounts(text: str) -> tuple[int | None, float | None]:
    """(minutes, meters) said in `text`: "3 miles in 28 minutes", "an hour and a half", "5k"."""
    meters = None
    for m in _DISTANCE.finditer(text):
        n, unit = float(m.group("n")), m.group("u").lower()
        if unit.startswith("mi"):
            meters = (meters or 0) + n * METERS_PER_MILE
        elif unit.startswith("met"):
            meters = (meters or 0) + n
        else:
            meters = (meters or 0) + n * 1000
    rest = _DISTANCE.sub(" ", text)
    minutes = 0.0
    found = False
    for pattern, worth in _HOURS_SAID:
        rest, n = pattern.subn(" ", rest)
        minutes += worth * n
        found = found or bool(n)
    for m in _DURATION.finditer(rest):
        n, unit = float(m.group("n")), m.group("u").lower()
        minutes += n * 60 if unit.startswith("h") else n
        found = True
    return (round(minutes) if found and minutes >= 1 else None), (round(meters, 1) if meters else None)


def _when(text: str, today: date) -> tuple[str, date]:
    m = _WHEN.search(text)
    if not m:
        return text, today
    said = m.group("when").lower()
    day = today - timedelta(days=1) if said.startswith("yesterday") or said == "last night" else today
    return text[: m.start()], day


def distance(meters: float | None, miles: bool) -> str:
    if not meters:
        return ""
    if miles:
        n = meters / METERS_PER_MILE
        return f"{n:.1f}".rstrip("0").rstrip(".") + " mi"
    return f"{meters / 1000:.1f}".rstrip("0").rstrip(".") + " km"


def length(minutes: int | None) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h and m else f"{h}h" if h else f"{m} min"


def spoken_length(minutes: int) -> str:
    """For speech: "45 minutes", "an hour", "an hour and a half", "2 hours", "1 hour 15 minutes"."""
    h, m = divmod(int(minutes), 60)
    if not h:
        return f"{m} minute{'s' if m != 1 else ''}"
    hours = "an hour" if h == 1 else f"{h} hours"
    if not m:
        return hours
    if m == 30:
        return f"{hours} and a half"
    return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"


def _article(number: str) -> str:
    return "an" if number.startswith("8") or number in {"11", "18"} else "a"


def described(row: dict, miles: bool) -> str:
    """"run 3.1 mi · 28 min", "lift 45 min", "yoga"."""
    parts = [p for p in (distance(row.get("distance_m") and float(row["distance_m"]), miles), length(row.get("minutes"))) if p]
    return row["kind"] + (" " + " · ".join(parts) if parts else "")


def _a(kind: str, minutes: int | None, meters: float | None, miles: bool) -> str:
    """For speech: "a 3 mile run", "a 45 minute lift", "an hour of yoga", "a workout"."""
    thing = SAID.get(kind, kind)
    if meters:
        n = distance(meters, miles)
        n = n.replace(" mi", " mile") if miles else n.replace(" km", "K")
        return f"{_article(n)} {n} {thing}"
    if minutes:
        if kind not in SAID:
            return f"{spoken_length(minutes)} of {kind}"
        if minutes == 60:
            return f"an hour-long {thing}"
        span = f"{minutes // 60} hour" if minutes % 60 == 0 else f"{minutes} minute"
        return f"{_article(span)} {span} {thing}"
    if kind not in SAID:
        return kind
    return ("an " if thing[0] in "aeiou" else "a ") + thing


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


class Workouts(Skill):
    name = SKILL
    help = (
        "`/workout run 3 mi 28 min` — log one (or say \"ran 3 miles\"); `/workout goal 4` a week; `/workouts`",
    )
    commands = frozenset({"workout", "workouts"})

    @property
    def miles(self) -> bool:
        return (self.ctx.config.weather_units or "").lower().startswith("f")

    async def _goal(self) -> int | None:
        raw = await self.ctx.store.get_skill_setting(SKILL, "goal")
        return int(raw) if raw and raw.isdigit() else None

    async def _week(self) -> list[dict]:
        return [dict(r) for r in await self.ctx.store.workouts_since(week_start(self.ctx.today()))]

    async def _tally(self) -> str:
        count, goal = len(await self._week()), await self._goal()
        if goal:
            return f"{count} of {goal} this week."
        return f"That's {count} this week."

    async def log(self, text: str, *, bare_minutes: bool = False) -> Answer | None:
        """Log what `text` says he did. None if it doesn't say what or how much."""
        text, day = _when(text, self.ctx.today())
        kind = _kind(text)
        minutes, meters = amounts(text)
        if minutes is None and bare_minutes:
            number = re.search(r"(?<![\w.])(\d{1,3})(?![\w.])", text)
            minutes = int(number.group(1)) if number else None
        if kind is None and bare_minutes:
            # "/workout pickleball 60": whatever he calls it.
            words = [w for w in re.findall(r"[a-z][a-z'-]*", text.lower())
                     if w not in {"min", "mins", "minute", "minutes", "hour", "hours", "hr", "hrs", "for", "of", "a", "an",
                                  "the", "my", "mi", "mile", "miles", "km", "k"}]
            kind = " ".join(words[:3]) or None
        if kind is None:
            if meters:
                kind = "run"
            elif minutes and bare_minutes:
                kind = "workout"
            else:
                return None
        if minutes is not None and minutes > MAX_MINUTES:
            return Answer(f"{length(minutes)} is longer than I can believe. Say it again with the minutes?")
        await self.ctx.store.add_workout(kind=kind[:60], minutes=minutes, distance_m=meters, done_on=day,
                                         logged_at=self.ctx.now())
        when = "" if day == self.ctx.today() else " yesterday"
        return Answer(f"Logged {_a(kind, minutes, meters, self.miles)}{when}. {await self._tally()}")

    async def summary(self) -> Answer:
        week, goal = await self._week(), await self._goal()
        today = self.ctx.today()
        if not week:
            last = await self.ctx.store.last_workout()
            said = f" The last one was {dates.spoken(last['done_on'], today)}." if last else ""
            return Answer(f"No workouts yet this week{f' (goal {goal})' if goal else ''}.{said}")
        total = sum(r["minutes"] or 0 for r in week)
        head = f"{len(week)} workout{'s' if len(week) != 1 else ''} this week" + (f", of {goal}" if goal else "")
        lines = [f"- {r['done_on']:%a}: {described(r, self.miles)}" for r in week]
        return Answer(f"{head}{f', {spoken_length(total)} in all' if total else ''}.", "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        low = rest.lower()
        if name == "workouts" or not rest:
            return await self.summary()
        if low.startswith("goal"):
            value = low[4:].strip()
            if value in {"off", "none", "0", "clear"}:
                await self.ctx.store.set_skill_setting(SKILL, "goal", None)
                return Answer("No workout goal now.")
            if not value.isdigit() or not 1 <= int(value) <= 14:
                return Answer("How many a week? Try /workout goal 4.")
            await self.ctx.store.set_skill_setting(SKILL, "goal", value)
            return Answer(f"Goal: {value} workouts a week. {await self._tally()}")
        if low in {"undo", "delete", "remove last", "oops"}:
            gone = await self.ctx.store.undo_workout(self.ctx.now() - timedelta(hours=24))
            if gone is None:
                return Answer("There's nothing you logged today to take back.")
            return Answer(f"Took back the {described(dict(gone), self.miles)}.")
        answer = await self.log(rest, bare_minutes=True)
        return answer or Answer("What did you do? Try /workout run 3 mi, or /workout lift 45.")

    async def match(self, text: str) -> Answer | None:
        if _SHOW.match(text):
            return await self.summary()
        m = _LOG.match(text)
        if m:
            return await self.log(m.group("rest"), bare_minutes=True)
        if _QUESTION.search(text):
            return None
        m = _VERBED.match(text)
        if m:
            minutes, meters = amounts(m.group("rest"))
            if (minutes is None and meters is None) or _NOT_EXERCISE.search(m.group("rest")):
                return None  # "I ran into Keegan", "I ran 10 minutes late"
            return await self.log(text)
        m = _WENT.match(text)
        if m:
            return await self.log(text) if _kind(m.group("rest")) else None  # "went on a date"
        if _WORKED_OUT.match(text):
            return await self.log(text)
        m = _DID.match(text)
        if m:
            rest = m.group("rest")
            minutes, meters = amounts(rest)
            if (minutes is None and meters is None) or (_kind(rest) is None and meters is None):
                return None  # "did 20 minutes of homework", "did the dishes"
            return await self.log(rest)
        return None

    async def facts(self) -> list[str]:
        week, goal = await self._week(), await self._goal()
        today = self.ctx.today()
        if week:
            done = "; ".join(f"{r['done_on']:%a} {described(r, self.miles)}" for r in week)
            return [f"- WORKOUTS this week (Mon-Sun): {len(week)}{f' of a {goal} goal' if goal else ''}: {done}"]
        last = await self.ctx.store.last_workout()
        if last is None and not goal:
            return []
        tail = f"; the last was {dates.spoken(last['done_on'], today)}, {described(dict(last), self.miles)}" if last else ""
        return [f"- WORKOUTS this week (Mon-Sun): none yet{f' (goal {goal})' if goal else ''}{tail}"]

    async def panel(self) -> dict | None:
        today = self.ctx.today()
        start = week_start(today)
        since = start - timedelta(weeks=WEEKS_SHOWN - 1)
        rows = [dict(r) for r in await self.ctx.store.workouts_since(since)]
        goal = await self._goal()
        last = await self.ctx.store.last_workout() if not rows else None
        if not rows and last is None and not goal:
            return None
        week = [r for r in rows if r["done_on"] >= start]
        days = []
        for i in range(7):
            on = start + timedelta(days=i)
            those = [r for r in week if r["done_on"] == on]
            days.append({"day": f"{on:%a}", "on": on.isoformat(), "count": len(those), "today": on == today,
                         "minutes": sum(r["minutes"] or 0 for r in those), "kinds": [r["kind"] for r in those]})
        weeks = [sum(1 for r in rows if week_start(r["done_on"]) == start - timedelta(weeks=n))
                 for n in range(WEEKS_SHOWN - 1, -1, -1)]
        newest = (rows[-1] if rows else last)
        meters = sum(float(r["distance_m"] or 0) for r in week)
        lines = [f"This week: {len(week)}" + (f" of {goal}" if goal else "")]
        if newest:
            lines.append(f"Last: {described(newest, self.miles)} ({dates.spoken(newest['done_on'], today)})")
        return {"title": "Workouts", "lines": lines, "count": len(week), "goal": goal, "days": days, "weeks": weeks,
                "minutes": sum(r["minutes"] or 0 for r in week), "distance": distance(meters, self.miles) or None,
                "last": {"text": described(newest, self.miles), "when": dates.spoken(newest["done_on"], today)}
                if newest else None}


def build(ctx: SkillContext) -> Skill:
    return Workouts(ctx)
