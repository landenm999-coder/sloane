"""What she remembers from talking with him, where he can see and correct it.

    /memory                 what she's learned about him, and the loose ends
    /remember <something>   keep it now (so does "remember that ...", in words)
    /forget 2               take a learned fact out of her memory (kept, unpinned)
    /followup call the dentist friday     a loose end, by hand
    /followup done dentist  it's done (she does this too, when he tells her)
    /followups              the loose ends

The nightly `learn` job (sloane/memory/learn.py) fills both from his own words.
A follow-up with a day gets one nudge that morning; the rest she brings up
when the moment is right, because they ride in LOOPS in every prompt.

"Remember that ..." doesn't wait for the night: said plainly, it's kept at once,
the way a person who heard it would. Without a day ("remember that I'm
vegetarian now") it's a learned fact, in STATE from the next message on. With
one ("don't forget I have the dentist friday") it's a loose end for that day,
because a dated note pinned forever would outlive its date. "Remember to ..."
is a loose end too. (skills.Registry offers what follows "remember that" to the
other skills first, so "remember that Maya's birthday is March 3" is a
birthday.)
"""

from __future__ import annotations

import re

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

MAX_WHAT = 160
MAX_NOTE = 240

# "remember that ...", "don't forget ...", "keep in mind ...". Not a question
# about the past ("remember when we ...?"): that one is for her to answer.
REMEMBER = re.compile(
    r"^\s*(?:please\s+)?(?:remember|don'?t\s+forget|do\s+not\s+forget|keep\s+in\s+mind)\b[\s,:]*"
    r"(?:that\s+)?(?P<what>.+?)[\s.!]*$",
    re.I | re.S,
)
_NOT_A_NOTE = re.compile(r"^(?:when|what|how|who|where|why|if|whether|this|me|it|us)\b", re.I)
_NOTE_STOP = frozenset({"a", "an", "the", "my", "i", "im", "i'm", "is", "are", "was", "am", "be", "that", "and",
                        "to", "of", "in", "on", "at", "for", "with", "now", "just", "really", "also", "have", "has",
                        "got", "it", "its", "it's", "his", "her", "their", "our", "me", "we", "he", "she", "they"})


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower())) - {"the", "a", "an", "my", "to"}


def note_key(text: str) -> str:
    """A learned fact's key from what it's about: 'my locker is 214' -> 'note.locker'.

    Letters only, so a correction ("my locker is 318 now") lands on the same key
    and replaces the old value instead of sitting beside it.
    """
    plain = text.lower().replace("'", "").replace("\u2019", "")
    words = [w for w in re.findall(r"[a-z]+", plain) if w not in _NOTE_STOP]
    return "note." + ("_".join(words[:3]) or "misc")


class Memory(Skill):
    name = "memory"
    help = (
        "`/memory` — what I've learned about you, and loose ends; `/remember <it>`; `/forget <n>`",
        "`/followup call the dentist friday` · `/followup done dentist` · `/followups`",
    )
    commands = frozenset({"memory", "remember", "forget", "followup", "followups"})

    async def match(self, text: str) -> Answer | None:
        told = REMEMBER.match(text)
        if told is None:
            return None
        what = told.group("what").strip()
        if len(what) < 3 or what.endswith("?") or _NOT_A_NOTE.match(what):
            return None
        return await self._remember(what)

    async def _remember(self, what: str) -> Answer:
        """Keep something he told her, now: a loose end if it's a to-do or has a day, else a fact."""
        what = what.strip().rstrip(".!")
        if not what:
            return Answer("What should I remember? Try /remember I'm vegetarian now.")
        todo = re.match(r"^to\s+(.+)$", what, re.I | re.S)
        today = self.ctx.today()
        found = dates.find(what, today)
        if todo or (found is not None and found.day >= today):
            return await self._add(todo.group(1) if todo else what)
        note = safe_field(what, limit=MAX_NOTE)
        await self.ctx.store.put_state(f"learned.{note_key(note)}", note, category="learned", confidence=1.0,
                                       source=f"told {today.isoformat()}", pin=True)
        return Answer("Got it.", f"I'll remember: {note}\n\n`/memory` shows what I know; `/forget <n>` if it changes.")

    async def _loose_ends(self) -> Answer:
        rows = await self.ctx.store.open_follow_ups()
        if not rows:
            return Answer("No loose ends.")
        today = self.ctx.today()
        lines = [f"{i}. {r['summary']}" + (f" ({dates.spoken(r['due_on'], today)})" if r.get("due_on") else "")
                 for i, r in enumerate(rows, 1)]
        return Answer(f"{len(rows)} loose end{'s' if len(rows) != 1 else ''}.",
                      "\n".join(lines) + "\n\n`/followup done <n>` closes one.")

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "followups" or (name == "followup" and not rest):
            return await self._loose_ends()
        if name == "followup":
            head, _, tail = rest.partition(" ")
            if head.lower() in {"done", "did", "close", "closed"} and tail.strip():
                return await self._close(tail)
            return await self._add(rest)
        if name == "forget":
            return await self._forget(rest)
        if name == "remember":
            return await self._remember(rest)
        facts = await self.ctx.store.learned_facts()
        loose = await self.ctx.store.open_follow_ups()
        if not facts and not loose:
            return Answer("I haven't picked anything up yet.",
                          "Each night I note what you mention about yourself and anything you said you'd do.")
        lines = []
        if facts:
            lines.append("**What I've learned**")
            lines += [f"{i}. {f['value']}  ·  `{f['key'].removeprefix('learned.')}`" for i, f in enumerate(facts, 1)]
        if loose:
            lines += (["", "**Loose ends**"] if lines else ["**Loose ends**"])
            lines += [f"• {r['summary']}" for r in loose]
        return Answer(f"{len(facts)} thing{'s' if len(facts) != 1 else ''} learned, "
                      f"{len(loose)} loose end{'s' if len(loose) != 1 else ''}.",
                      "\n".join(lines) + ("\n\n`/forget <n>` if I got one wrong." if facts else ""))

    async def _add(self, text: str) -> Answer:
        today = self.ctx.today()
        found = dates.find(text, today)
        due = found.day if found is not None and found.day >= today else None
        what = safe_field(dates.remove(text, found) if found else text, limit=MAX_WHAT).strip(" .")
        if not what:
            return Answer("What's the loose end? Try /followup call the dentist friday.")
        await self.ctx.store.add_follow_up(what, due)
        when = f", {dates.spoken(due, today)}" if due else ""
        return Answer(f"Noted: {what}{when}.")

    async def _close(self, text: str) -> Answer:
        rows = await self.ctx.store.open_follow_ups()
        text = text.strip()
        if text.isdigit():
            n = int(text)
            hits = [rows[n - 1]] if 1 <= n <= len(rows) else []
        else:
            wanted = _words(text)
            hits = [r for r in rows if wanted and wanted <= _words(r["summary"])]
        if len(hits) != 1:
            return Answer("Which one?" if hits else "I don't have that one open.",
                          "\n".join(f"• {r['summary']}" for r in (hits or rows)[:8]))
        row = await self.ctx.store.close_follow_up(str(hits[0]["id"]))
        return Answer(f"Closed: {row['summary']}." if row else "That one's already closed.")

    async def _forget(self, text: str) -> Answer:
        facts = await self.ctx.store.learned_facts()
        text = text.strip()
        if text.isdigit() and 1 <= int(text) <= len(facts):
            key = facts[int(text) - 1]["key"]
        else:
            key = "learned." + text.lower().removeprefix("learned.")
            if key not in {f["key"] for f in facts}:
                return Answer("Use /forget with a number from /memory.")
        row = await self.ctx.store.unpin_state(key)
        return Answer(f"Forgotten: {row['value']}." if row else "That one's already forgotten.")

    async def panel(self) -> dict | None:
        rows = await self.ctx.store.open_follow_ups()
        if not rows:
            return None
        today = self.ctx.today()
        return {"title": "Loose ends", "lines": [
            r["summary"] + (f" ({dates.spoken(r['due_on'], today)})" if r.get("due_on") else "") for r in rows[:8]]}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        if not 8 <= now.hour < 11:
            return []
        today = now.date()
        return [Nudge(f"followup:{r['id']}:{today}", f"📌 Today: {r['summary']}.")
                for r in await self.ctx.store.open_follow_ups() if r.get("due_on") == today]


def build(ctx: SkillContext) -> Skill:
    return Memory(ctx)
