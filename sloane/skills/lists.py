"""Lists: grocery, packing, to-do -- whatever list he names.

    "add milk, eggs and bread to my grocery list"
    "put charger on the packing list"
    "cross milk off my grocery list"   ·   "check off 2 on the grocery list"
    "what's on my grocery list?"       ·   "clear the packing list"
    /list  ·  /list grocery

Every rule needs the word "list", so "add a section to the essay" still goes to
the agent. No model: a list is the kind of thing that must work with every
provider down, from a voice note in the car.

Nothing is deleted. Checking an item off (or clearing a list) marks it done.
"""

from __future__ import annotations

import re

from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

# How much of each list rides in FACTS and on the TV.
FACTS_ITEMS = 12
MAX_LISTS_IN_FACTS = 6
MAX_ITEM = 120

_FILLER = {"my", "the", "our", "a", "an"}
_ALIASES = {"groceries": "grocery", "to do": "todo", "to-do": "todo", "todos": "todo",
            "to dos": "todo", "to-dos": "todo", "shopping": "shopping"}

_LIST = r"(?:my\s+|the\s+|our\s+)?(?P<list>[\w'&-]+(?:\s+[\w'&-]+){0,2}?)\s+list"
_END = r"[.!?]*\s*$"
_ADD = re.compile(
    r"^\s*(?:please\s+)?(?:add|put|throw|stick)\s+(?P<items>.+?)\s+(?:to|on|onto|in|into)\s+" + _LIST + _END,
    re.I | re.S,
)
_REMOVE = re.compile(
    r"^\s*(?:please\s+)?(?:remove|delete|take|cross|check|tick|scratch|mark)\s+(?:off\s+)?"
    r"(?P<items>.+?)\s+(?:off\s+of|off|from|on|out\s+of|as\s+done\s+on)\s+" + _LIST + _END,
    re.I | re.S,
)
_SHOW = re.compile(
    r"^\s*(?:(?:what'?s|whats|what\s+is|what\s+do\s+i\s+have)\s+on|(?:show|read|send|give|tell)\s+(?:me\s+)?)\s*"
    + _LIST + _END,
    re.I,
)
_CLEAR = re.compile(r"^\s*(?:please\s+)?(?:clear|empty|reset|wipe)\s+(?:out\s+)?" + _LIST + _END, re.I)
_ALL = re.compile(
    r"^\s*(?:(?:what|which)\s+lists\s+(?:do\s+i\s+have|are\s+there)|(?:show|what\s+are)\s+(?:me\s+)?(?:all\s+)?my\s+lists)"
    + _END,
    re.I,
)
_SPLIT = re.compile(r"\s*(?:,\s*and\s+|,|\s+and\s+|\s*&\s*|\s+\+\s+|;)\s*", re.I)


def list_name(raw: str) -> str:
    """'My Groceries' -> 'grocery'; 'to-do' -> 'todo'. Lowercase, no filler."""
    words = [w for w in re.findall(r"[\w'&-]+", raw.lower()) if w not in _FILLER]
    if words and words[-1] == "list":
        words = words[:-1]
    name = " ".join(words)
    return _ALIASES.get(name, name)


def split_items(raw: str) -> list[str]:
    """'milk, eggs, and bread' -> ['milk', 'eggs', 'bread']."""
    items = []
    for part in _SPLIT.split(raw.strip()):
        part = re.sub(r"^(?:some|a|an)\s+", "", part.strip(" .!?\"'"), flags=re.I)
        if part:
            items.append(safe_field(part, limit=MAX_ITEM))
    return items


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _count(n: int) -> str:
    return f"{n} thing{'s' if n != 1 else ''}"


class Lists(Skill):
    name = "lists"
    help = (
        "`/list` · `/list grocery` — your lists (or say \"add milk to my grocery list\")",
    )
    commands = frozenset({"list", "lists"})

    # -- names -------------------------------------------------------------------

    async def _resolve(self, raw: str) -> str:
        """A list he means, matched to one that exists where the plural differs."""
        name = list_name(raw)
        known = {r["list"] for r in await self.ctx.store.list_names()}
        if name in known or not name:
            return name
        for candidate in (name.rstrip("s"), name + "s", re.sub(r"ies$", "y", name), re.sub(r"y$", "ies", name)):
            if candidate in known:
                return candidate
        return name

    # -- actions -------------------------------------------------------------------

    async def add(self, raw_list: str, raw_items: str) -> Answer:
        name = await self._resolve(raw_list)
        items = split_items(raw_items)
        if not name or not items:
            return Answer("What should I add, and to which list?")
        added = await self.ctx.store.add_list_items(name, items)
        total = len(await self.ctx.store.open_list_items(name))
        already = [i for i in items if i.lower() not in {r["item"].lower() for r in added}]
        if not added:
            return Answer(f"{_join(already).capitalize()} {'is' if len(already) == 1 else 'are'} "
                          f"already on your {name} list.")
        speech = f"Added {_join([r['item'] for r in added])} to your {name} list; {_count(total)} on it now."
        detail = f"Already there: {_join(already)}" if already else ""
        return Answer(speech, detail)

    async def show(self, raw_list: str) -> Answer:
        name = await self._resolve(raw_list)
        rows = await self.ctx.store.open_list_items(name)
        if not rows:
            return Answer(f"Your {name} list is empty.")
        lines = [f"{i}. {r['item']}" for i, r in enumerate(rows, 1)]
        head = _join([r["item"] for r in rows[:6]]) + (f", and {len(rows) - 6} more" if len(rows) > 6 else "")
        return Answer(f"{_count(len(rows)).capitalize()} on your {name} list: {head}.",
                      f"**{name.capitalize()}**\n" + "\n".join(lines))

    async def check_off(self, raw_list: str, raw_items: str) -> Answer:
        name = await self._resolve(raw_list)
        rows = await self.ctx.store.open_list_items(name)
        if not rows:
            return Answer(f"Your {name} list is already empty.")
        wanted = split_items(raw_items)
        picked: dict[str, dict] = {}
        missing: list[str] = []
        for want in wanted:
            hit = self._match(want, rows)
            if hit is None:
                missing.append(want)
            else:
                picked[str(hit["id"])] = hit
        done = await self.ctx.store.check_off_items(list(picked)) if picked else []
        left = len(rows) - len(done)
        parts = []
        if done:
            parts.append(f"Checked off {_join([r['item'] for r in done])}; {_count(left)} left on your {name} list.")
        if missing:
            parts.append(f"I don't see {_join(missing)} on it.")
        return Answer(" ".join(parts))

    @staticmethod
    def _match(want: str, rows: list[dict]) -> dict | None:
        """An item by its number, its exact text, or one unique word match."""
        if want.isdigit():
            n = int(want)
            return rows[n - 1] if 1 <= n <= len(rows) else None
        low = want.lower()
        exact = [r for r in rows if r["item"].lower() == low]
        if exact:
            return exact[0]
        words = set(re.findall(r"\w+", low)) - _FILLER - {"some"}
        partial = [r for r in rows if words and words <= set(re.findall(r"\w+", r["item"].lower()))]
        return partial[0] if len(partial) == 1 else None

    async def clear(self, raw_list: str) -> Answer:
        name = await self._resolve(raw_list)
        n = await self.ctx.store.clear_list(name)
        if not n:
            return Answer(f"Your {name} list was already empty.")
        return Answer(f"Cleared your {name} list: {_count(n)} checked off.")

    async def all_lists(self) -> Answer:
        rows = [r for r in await self.ctx.store.list_names() if int(r["open"])]
        if not rows:
            return Answer("You don't have anything on a list.",
                          'Say "add milk to my grocery list" to start one.')
        lines = [f"• {r['list']}: {r['open']}" for r in rows]
        return Answer(f"{len(rows)} list{'s' if len(rows) != 1 else ''} with something on "
                      f"{'them' if len(rows) != 1 else 'it'}.", "\n".join(lines))

    # -- the skill contract --------------------------------------------------------

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if not rest:
            return await self.all_lists()
        words = rest.split(maxsplit=1)
        if len(words) == 2 and words[0].lower() in {"add", "done", "off", "clear"}:
            verb, tail = words[0].lower(), words[1]
            # /list add grocery: milk, eggs  ·  /list done grocery 2  ·  /list clear grocery
            if verb == "clear":
                return await self.clear(tail)
            target, _, items = tail.partition(":")
            if not items:
                target, _, items = tail.partition(" ")
            if verb == "add":
                return await self.add(target, items)
            return await self.check_off(target, items)
        return await self.show(rest)

    async def match(self, text: str) -> Answer | None:
        text = text.replace("\u2019", "'")  # a phone's curly apostrophe
        if "list" not in text.lower():
            return None
        if _ALL.match(text):
            return await self.all_lists()
        for pattern, action in ((_ADD, "add"), (_REMOVE, "off"), (_CLEAR, "clear"), (_SHOW, "show")):
            found = pattern.match(text)
            # "put it on the list" names no list: that one is the agent's.
            if found is None or not list_name(found["list"]):
                continue
            if action == "add":
                return await self.add(found["list"], found["items"])
            if action == "off":
                return await self.check_off(found["list"], found["items"])
            if action == "clear":
                return await self.clear(found["list"])
            return await self.show(found["list"])
        return None

    async def _grouped(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for r in await self.ctx.store.open_list_items():
            grouped.setdefault(r["list"], []).append(r["item"])
        return grouped

    async def facts(self) -> list[str]:
        lines = []
        for name, items in list((await self._grouped()).items())[:MAX_LISTS_IN_FACTS]:
            shown = ", ".join(safe_field(i, limit=60) for i in items[:FACTS_ITEMS])
            more = f", +{len(items) - FACTS_ITEMS} more" if len(items) > FACTS_ITEMS else ""
            lines.append(f"- LIST {safe_field(name, limit=40)} ({len(items)} open): {shown}{more}")
        return lines

    async def panel(self) -> dict | None:
        grouped = await self._grouped()
        if not grouped:
            return None
        return {
            "title": "Lists",
            "lines": [f"{name.capitalize()}: " + ", ".join(items[:FACTS_ITEMS])
                      + (f" +{len(items) - FACTS_ITEMS}" if len(items) > FACTS_ITEMS else "")
                      for name, items in grouped.items()],
            "lists": grouped,
        }


def build(ctx: SkillContext) -> Skill:
    return Lists(ctx)
