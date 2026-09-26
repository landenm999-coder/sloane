"""Money: what he spends against a weekly budget, and what his shifts earned.

    "spent 12 on lunch"  ·  "spent $4.50 at starbucks yesterday"  ·  "paid 15 for the DECA fee"
    /spent            this week and month, by category
    /spent 12 lunch   the same as saying it
    /spent undo       take back the last one
    /budget 100       a weekly budget (Monday to Sunday); /budget off

Earnings are an estimate, and say so: the hours of his shifts that have ended
this week times PAY_RATE. The shifts are generated from the fixed 3-7 rule, so
a shift he called out of still counts until it is cancelled.

This tracks and nothing else. Moving money is a hard line; no code here can.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

MAX_WHAT = 80
CATEGORIES: dict[str, tuple[str, ...]] = {
    "food": ("lunch", "dinner", "breakfast", "food", "coffee", "starbucks", "chipotle", "mcdonalds",
             "mcdonald's", "taco", "tacos", "pizza", "snack", "snacks", "groceries", "grocery", "drink",
             "drinks", "boba", "restaurant", "chick-fil-a", "subway", "wendys", "burger", "sonic",
             "dutch", "smoothie", "donuts", "bagel"),
    "gas": ("gas", "fuel"),
    "car": ("car", "parking", "oil", "tires", "insurance", "wash"),
    "school": ("school", "book", "books", "textbook", "supplies", "deca", "fee", "fees", "calculator",
               "lab", "yearbook", "prom", "ap"),
    "business": ("domain", "hosting", "vercel", "software", "subscription", "ads", "api", "figma",
                 "openai", "anthropic", "claude", "server", "template", "client"),
    "fun": ("movie", "movies", "game", "games", "concert", "tickets", "ticket", "steam", "bowling"),
    "clothes": ("shirt", "shoes", "clothes", "hoodie", "jeans", "jacket", "hat"),
}

_SPENT = re.compile(
    r"^\s*(?:i\s+)?(?:just\s+)?(?:spent|paid)\s+\$?\s?(?P<amount>\d{1,6}(?:\.\d{1,2})?)\s*(?:dollars|bucks|usd)?\s+"
    r"(?:on|at|for)\s+(?P<what>.+?)\s*[.!]*\s*$",
    re.I,
)
_AMOUNT = re.compile(r"^\s*\$?\s?(?P<amount>\d{1,6}(?:\.\d{1,2})?)\s*(?:dollars|bucks)?\s+(?:on\s+|at\s+|for\s+)?(?P<what>.+)$", re.I)


def category(what: str) -> str:
    words = set(re.findall(r"[\w'-]+", what.lower()))
    for name, keys in CATEGORIES.items():
        if words & set(keys):
            return name
    return "other"


def dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


class Money(Skill):
    name = "money"
    help = (
        "\"spent 12 on lunch\" — track spending; `/spent` the week; `/spent undo`",
        "`/budget 100` — a weekly budget",
    )
    commands = frozenset({"spent", "budget"})

    def _week(self) -> tuple[date, date]:
        today = self.ctx.today()
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)

    async def _budget(self) -> int | None:
        raw = await self.ctx.store.get_skill_setting(self.name, "weekly_budget_cents")
        return int(raw) if raw and raw.isdigit() else None

    async def _week_cents(self) -> int:
        start, end = self._week()
        return sum(r["cents"] for r in await self.ctx.store.expenses_between(start, end))

    async def _earned(self) -> tuple[float, int] | None:
        """(hours, cents) from this week's shifts that have ended, or None without a pay rate."""
        rate = self.ctx.config.pay_rate
        if not rate or rate <= 0:
            return None
        start, _ = self._week()
        now = self.ctx.now()
        hours = 0.0
        for s in await self.ctx.store.shifts_between(start, now.date()):
            if s.get("cancelled") or s["ends_at"] > now:
                continue
            hours += (s["ends_at"] - s["starts_at"]).total_seconds() / 3600
        return hours, round(hours * rate * 100)

    async def record(self, amount: str, what_text: str) -> Answer:
        try:
            cents = round(float(amount) * 100)
        except ValueError:
            return Answer("How much?")
        if not 0 < cents < 10_000_000:
            return Answer("That amount doesn't look right.")
        today = self.ctx.today()
        when = dates.find(what_text, today, future=False)
        spent_on = today
        if when is not None and 0 <= (today - when.day).days <= 31:
            spent_on, what_text = when.day, dates.remove(what_text, when)
        what = safe_field(what_text.strip(" .!"), limit=MAX_WHAT)
        if not what:
            return Answer("Spent on what?")
        await self.ctx.store.add_expense(cents=cents, what=what, category=category(what), spent_on=spent_on)
        week = await self._week_cents()
        budget = await self._budget()
        day = "" if spent_on == today else f" ({dates.spoken(spent_on, today)})"
        tail = f"{dollars(week)} of {dollars(budget)} this week." if budget else f"{dollars(week)} this week."
        if budget and week > budget:
            tail = f"{dollars(week)} this week, {dollars(week - budget)} over your {dollars(budget)} budget."
        return Answer(f"Logged {dollars(cents)} on {what}{day}. {tail}")

    async def summary(self) -> Answer:
        start, end = self._week()
        today = self.ctx.today()
        week = await self.ctx.store.expenses_between(start, end)
        month = await self.ctx.store.expenses_between(today.replace(day=1), today)
        budget = await self._budget()
        earned = await self._earned()
        week_total = sum(r["cents"] for r in week)
        by_cat: dict[str, int] = {}
        for r in week:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["cents"]
        lines = [f"This week: {dollars(week_total)}" + (f" of {dollars(budget)}" if budget else "")]
        lines += [f"  • {c}: {dollars(v)}" for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
        lines.append(f"This month: {dollars(sum(r['cents'] for r in month))}")
        if earned is not None:
            lines.append(f"Earned this week (est.): {dollars(earned[1])} from {earned[0]:g} shift hours")
        if week:
            lines.append("")
            lines += [f"{r['spent_on']:%a} {dollars(r['cents'])} — {r['what']}" for r in week[-6:]]
        speech = f"{dollars(week_total)} spent this week"
        if budget:
            left = budget - week_total
            speech += f", {dollars(left)} left of your budget" if left >= 0 else f", {dollars(-left)} over budget"
        speech += "."
        return Answer(speech, "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "budget":
            if not rest:
                budget = await self._budget()
                return Answer(f"Your weekly budget is {dollars(budget)}." if budget else "No weekly budget set.",
                              "`/budget 100` sets one; `/budget off` clears it.")
            if rest.lower() in {"off", "none", "clear", "0"}:
                await self.ctx.store.set_skill_setting(self.name, "weekly_budget_cents", None)
                return Answer("Weekly budget cleared.")
            found = re.fullmatch(r"\$?\s?(\d{1,6}(?:\.\d{1,2})?)", rest)
            if not found:
                return Answer("Try /budget 100.")
            cents = round(float(found.group(1)) * 100)
            if cents <= 0:
                return Answer("Try /budget 100.")
            await self.ctx.store.set_skill_setting(self.name, "weekly_budget_cents", str(cents))
            return Answer(f"Weekly budget set to {dollars(cents)}; you're at {dollars(await self._week_cents())} this week.")
        if not rest:
            return await self.summary()
        if rest.lower() in {"undo", "oops", "delete last", "remove last"}:
            row = await self.ctx.store.archive_last_expense()
            if row is None:
                return Answer("Nothing to undo.")
            return Answer(f"Took back {dollars(row['cents'])} on {row['what']}.")
        found = _AMOUNT.match(rest)
        if not found:
            return Answer("Try /spent 12 lunch.")
        return await self.record(found["amount"], found["what"])

    async def match(self, text: str) -> Answer | None:
        found = _SPENT.match(text.replace("’", "'"))
        if found is None:
            return None
        return await self.record(found["amount"], found["what"])

    async def facts(self) -> list[str]:
        start, _ = self._week()
        week = await self._week_cents()
        budget = await self._budget()
        earned = await self._earned()
        if not week and not budget and earned is None:
            return []
        line = f"- MONEY this week (since Mon {start:%b} {start.day}): spent {dollars(week)}"
        if budget:
            line += f" of a {dollars(budget)} budget"
        if earned is not None:
            line += f"; earned about {dollars(earned[1])} from {earned[0]:g} shift hours (estimate)"
        return [line]

    async def panel(self) -> dict | None:
        week = await self._week_cents()
        budget = await self._budget()
        earned = await self._earned()
        if not week and not budget and earned is None:
            return None
        lines = [f"Spent this week: {dollars(week)}" + (f" / {dollars(budget)}" if budget else "")]
        if earned is not None:
            lines.append(f"Earned (est.): {dollars(earned[1])}")
        return {"title": "Money", "lines": lines}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        if not 8 <= now.hour < 21:
            return []
        budget = await self._budget()
        if not budget:
            return []
        week = await self._week_cents()
        monday, _ = self._week()
        if week > budget:
            return [Nudge(f"money:{monday}:100",
                          f"💸 You're {dollars(week - budget)} over this week's {dollars(budget)} budget.")]
        if week >= budget * 0.8:
            return [Nudge(f"money:{monday}:80",
                          f"💸 {dollars(budget - week)} left of this week's {dollars(budget)} budget.")]
        return []


def build(ctx: SkillContext) -> Skill:
    return Money(ctx)

