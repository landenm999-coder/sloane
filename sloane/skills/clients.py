"""Clients: the pipeline for his AI website business.

    /client add Bella's Bakery $1200 follow up friday
    /clients                                  the pipeline, numbered, follow-ups first
    /client 2                                 one client: stage, value, next step, notes
    /client bella building                    move a stage: lead talking proposal building live paid lost
    /client bella follow up mon: send mockups a follow-up date and what it's for
    /client bella note wants online ordering  a note
    /client bella $1500  ·  /client bella done (followed up)  ·  /client bella drop
    "follow up with Bella's Bakery on friday"

A client is named by its number in /clients or by the first words of its name.
Follow-ups due today or overdue are in FACTS and get one nudge in the morning.
This tracks. It never emails a client, sends an invoice or touches money; those
would go through the agency, and moving money is a hard line.
"""

from __future__ import annotations

import re
from datetime import date

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

STAGES = ("lead", "talking", "proposal", "building", "live", "paid", "lost")
OPEN = STAGES[:5]
STAGE_WORDS = {
    **{s: s for s in STAGES},
    "new": "lead", "talk": "talking", "call": "talking", "meeting": "talking", "quoted": "proposal",
    "quote": "proposal", "proposed": "proposal", "signed": "building", "build": "building",
    "started": "building", "launched": "live", "shipped": "live",
    "won": "paid", "closed": "paid", "dead": "lost", "passed": "lost",
}
MAX_NAME = 80
MAX_NOTE = 400
FACTS_CLIENTS = 8

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)\s*(k)?\b|\b(\d[\d,]*(?:\.\d{1,2})?)\s*(k)?\s*(?:dollars|bucks)\b", re.I)
_FOLLOW = re.compile(r"\bfollow(?:\s|-)?up\b", re.I)
_PLAIN_FOLLOW = re.compile(r"^\s*(?:i\s+(?:need\s+to|should)\s+)?follow(?:\s|-)?up\s+with\s+(?P<rest>.+?)\s*[.!]*\s*$", re.I)


def money(cents: int | None) -> str:
    if cents is None:
        return ""
    dollars = cents / 100
    return f"${dollars:,.0f}" if cents % 100 == 0 else f"${dollars:,.2f}"


def parse_money(text: str) -> tuple[int, tuple[int, int]] | None:
    """'$1,200' / '$1.5k' / '800 dollars' -> (cents, span)."""
    found = _MONEY.search(text)
    if not found:
        return None
    raw = (found.group(1) or found.group(3)).replace(",", "")
    thousands = bool(found.group(2) or found.group(4))
    try:
        value = float(raw) * (1000 if thousands else 1)
    except ValueError:
        return None
    if value < 0 or value > 10_000_000:
        return None
    return round(value * 100), found.span()


def _cut(text: str, span: tuple[int, int]) -> str:
    return re.sub(r"\s+", " ", text[: span[0]] + " " + text[span[1]:]).strip(" ,:-")


def _words(text: str) -> list[str]:
    """Lowercase words with possessives folded: "Bella's Bakery" -> bella, bakery."""
    return re.findall(r"\w+", re.sub(r"['\u2019]s\b|['\u2019]", "", text.lower()))


def _starts(name: str, prefix: list[str], *, partial: bool = True) -> bool:
    """Every word he typed matches the name's words in order; with `partial`,
    the last may be cut short ("/client bel" for Bella's Bakery)."""
    words = _words(name)
    if not prefix or len(prefix) > len(words):
        return False
    last = words[len(prefix) - 1]
    return words[: len(prefix) - 1] == prefix[:-1] and (
        last.startswith(prefix[-1]) if partial else last == prefix[-1])


class Clients(Skill):
    name = "clients"
    help = (
        "`/client add <name> [$amount] [follow up <day>]` — a client; `/clients` the pipeline",
        "`/client <n|name> building` · `follow up fri: <what>` · `note <text>` · `$1200` · `done` · `drop`",
    )
    commands = frozenset({"client", "clients"})

    # -- finding one ---------------------------------------------------------------

    async def _resolve(self, text: str, *, partial: bool = True) -> tuple[dict | None, str, list[dict]]:
        """(client, the rest of the words, the numbered list). Number or name prefix.

        A slash command may cut the last word short; a plain sentence may not,
        or "follow up with Pete" would land on Peterson Plumbing.
        """
        rows = await self.ctx.store.open_clients()
        closed = [r for r in await self.ctx.store.all_clients() if r["stage"] not in OPEN]
        text = text.strip()
        first, _, rest = text.partition(" ")
        if first.isdigit():
            n = int(first)
            return (rows[n - 1] if 1 <= n <= len(rows) else None), rest.strip(), rows
        words = text.split()
        for k in range(len(words), 0, -1):
            prefix = _words(" ".join(words[:k]))
            if not prefix:
                continue
            hits = [r for r in rows + closed if _starts(r["name"], prefix, partial=partial)]
            if len(hits) == 1:
                return hits[0], " ".join(words[k:]), rows
            if len(hits) > 1:
                exact = [r for r in hits if _words(r["name"]) == prefix]
                if len(exact) == 1:
                    return exact[0], " ".join(words[k:]), rows
                return None, text, rows
        return None, text, rows

    # -- rendering -------------------------------------------------------------------

    def _summary(self, r: dict, today: date) -> str:
        bits = [r["stage"]]
        if r.get("value_cents") is not None:
            bits.append(money(r["value_cents"]))
        if r.get("follow_up_on"):
            late = (today - r["follow_up_on"]).days
            when = dates.spoken(r["follow_up_on"], today)
            follow = f"follow-up overdue since {when}" if late > 0 else f"follow up {when}"
            if r.get("next_step"):
                follow += f" ({r['next_step']})"
            bits.append(follow)
        elif r.get("next_step"):
            bits.append(f"next: {r['next_step']} (no date)")
        return ", ".join(bits)

    # -- commands ----------------------------------------------------------------------

    async def add(self, rest: str) -> Answer:
        today = self.ctx.today()
        text = rest.strip()
        value = parse_money(text)
        if value:
            text = _cut(text, value[1])
        follow_on = step = None
        follow = _FOLLOW.search(text)
        if follow:
            tail = text[follow.end():]
            text = text[: follow.start()].strip(" ,:-")
            found = dates.find(tail, today)
            if found:
                follow_on = found.day
                step = safe_field(dates.remove(tail, found).strip(" :,-"), limit=200) or None
            else:  # "follow up after launch": kept as the next step, with no date
                step = safe_field(("follow up " + tail.strip(" :,-")).strip(), limit=200)
        name = safe_field(text.strip(" ,:-"), limit=MAX_NAME)
        if not _words(name):
            return Answer("Who's the client? Try /client add Bella's Bakery $1200.")
        row = await self.ctx.store.add_client(name, value_cents=value[0] if value else None,
                                              follow_up_on=follow_on, next_step=step)
        if row is None:
            return Answer(f"{name} is already in your pipeline.")
        extra = self._summary(row, today)
        return Answer(f"Added {name} as a lead.", f"{name}: {extra}")

    async def listing(self) -> Answer:
        rows = await self.ctx.store.open_clients()
        today = self.ctx.today()
        if not rows:
            return Answer("No open clients.", "Add one with `/client add <name>`.")
        lines = [f"{i}. {r['name']} — {self._summary(r, today)}" for i, r in enumerate(rows, 1)]
        total = sum(r["value_cents"] or 0 for r in rows)
        due = [r for r in rows if r.get("follow_up_on") and r["follow_up_on"] <= today]
        month_start = self.ctx.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        paid = await self.ctx.store.clients_paid_since(month_start)
        speech = f"{len(rows)} open client{'s' if len(rows) != 1 else ''}"
        speech += f", {money(total)} in the pipeline" if total else ""
        speech += f"; {len(due)} follow-up{'s' if len(due) != 1 else ''} due." if due else "."
        footer = []
        if paid:
            footer.append(f"Paid this month: {money(sum(p['value_cents'] or 0 for p in paid))} "
                          f"({', '.join(p['name'] for p in paid)})")
        return Answer(speech, "\n".join(lines + ([""] + footer if footer else [])))

    async def show(self, client: dict) -> Answer:
        today = self.ctx.today()
        notes = await self.ctx.store.client_notes(str(client["id"]))
        lines = [f"**{client['name']}** — {self._summary(client, today)}"]
        lines += [f"• {n['at'].astimezone(self.ctx.now().tzinfo):%b %d}: {n['note']}" for n in notes]
        return Answer(f"{client['name']}: {self._summary(client, today)}.", "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "clients" or not rest:
            return await self.listing()
        verb, _, tail = rest.partition(" ")
        if verb.lower() in {"add", "new"}:
            return await self.add(tail)
        client, action, rows = await self._resolve(rest)
        if client is None:
            return Answer("Which client? Use the number from /clients or the start of the name.")
        return await self.act(client, action)

    async def act(self, client: dict, action: str) -> Answer:
        today = self.ctx.today()
        cid = str(client["id"])
        action = action.strip()
        if not action:
            return await self.show(client)
        head, _, tail = action.partition(" ")
        word = head.lower().strip(":,.")
        if word == "note":
            note = safe_field(tail, limit=MAX_NOTE)
            if not note:
                return Answer("What's the note?")
            await self.ctx.store.add_client_note(cid, note)
            return Answer(f"Noted on {client['name']}.")
        if word in {"drop", "archive", "remove"}:
            await self.ctx.store.archive_client(cid)
            return Answer(f"Took {client['name']} off the pipeline.")
        if word in {"followed", "done", "contacted"} and not tail.strip() and client.get("follow_up_on"):
            await self.ctx.store.update_client(cid, follow_up_on=None, next_step=None)
            return Answer(f"Follow-up with {client['name']} done.")
        if _FOLLOW.match(action):
            after = action[_FOLLOW.match(action).end():]
            found = dates.find(after, today)
            if found is None:
                return Answer("When? Try /client bella follow up friday: send mockups.")
            step = safe_field(dates.remove(after, found).strip(" :,-"), limit=200) or None
            row = await self.ctx.store.update_client(cid, follow_up_on=found.day, next_step=step)
            what = f": {step}" if step else ""
            return Answer(f"Follow up with {row['name']} {dates.spoken(found.day, today)}{what}.")
        value = parse_money(action)
        if value and not _cut(action, value[1]):
            row = await self.ctx.store.update_client(cid, value_cents=value[0])
            return Answer(f"{row['name']} is worth {money(value[0])}.")
        if word in STAGE_WORDS and not tail.strip():
            stage = STAGE_WORDS[word]
            row = await self.ctx.store.update_client(cid, stage=stage)
            cheer = {"paid": " Nice work.", "lost": " On to the next one."}.get(stage, "")
            follow = ""
            if stage in {"paid", "lost"} and row.get("follow_up_on"):
                await self.ctx.store.update_client(cid, follow_up_on=None, next_step=None)
                follow = " Its follow-up is cleared."
            return Answer(f"{row['name']} is now {stage}.{cheer}{follow}")
        return Answer("I didn't follow that.", "Try a stage (" + ", ".join(STAGES) + "), "
                      "`follow up <day>: <what>`, `note <text>`, `$amount`, `done` or `drop`.")

    async def match(self, text: str) -> Answer | None:
        found = _PLAIN_FOLLOW.match(text.replace("’", "'"))
        if found is None:
            return None
        today = self.ctx.today()
        rest = found["rest"]
        when = dates.find(rest, today)
        if when is None:
            return None
        client, leftover, _ = await self._resolve(dates.remove(rest, when), partial=False)
        if client is None:
            return None  # not a client of his: the agent's (or a promise)
        return await self.act(client, f"follow up {when.day.isoformat()} {leftover}".strip())

    # -- context, dashboard, heartbeat ----------------------------------------------------

    async def facts(self) -> list[str]:
        rows = await self.ctx.store.open_clients()
        if not rows:
            return []
        today = self.ctx.today()
        total = sum(r["value_cents"] or 0 for r in rows)
        lines = [f"- CLIENTS {len(rows)} open" + (f", {money(total)} in the pipeline" if total else "")]
        for r in rows[:FACTS_CLIENTS]:
            lines.append(f"- CLIENT {safe_field(r['name'], limit=MAX_NAME)}: "
                         f"{safe_field(self._summary(r, today), limit=240)}")
        if len(rows) > FACTS_CLIENTS:
            lines.append(f"- CLIENT {len(rows) - FACTS_CLIENTS} more open, not listed here (/clients has all)")
        return lines

    async def panel(self) -> dict | None:
        rows = await self.ctx.store.open_clients()
        if not rows:
            return None
        today = self.ctx.today()
        by_stage: dict[str, list[str]] = {}
        for r in rows:
            by_stage.setdefault(r["stage"], []).append(r["name"])
        due = [r["name"] for r in rows if r.get("follow_up_on") and r["follow_up_on"] <= today]
        lines = [f"{stage.capitalize()}: {', '.join(names)}" for stage, names in by_stage.items()]
        if due:
            lines.insert(0, "Follow up: " + ", ".join(due))
        return {"title": "Clients", "lines": lines,
                "pipeline_cents": sum(r["value_cents"] or 0 for r in rows)}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        if not 8 <= now.hour < 12:
            return []
        today = now.date()
        due = [r for r in await self.ctx.store.open_clients()
               if r.get("follow_up_on") and r["follow_up_on"] <= today]
        if not due:
            return []
        parts = [r["name"] + (f" ({r['next_step']})" if r.get("next_step") else "") for r in due]
        return [Nudge(f"clients:{today}", "💼 Follow up today: " + "; ".join(parts) + ".")]


def build(ctx: SkillContext) -> Skill:
    return Clients(ctx)
