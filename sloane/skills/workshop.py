"""The workshop, on Telegram: ideas in, and a morning word about what's ready.

    /idea a skill that tracks my workouts    into the workshop; she plans it
    /workshop                                what's ready for him, building, planned
    /workshop build 2                        build that one tonight
    /workshop build 2 now                    ...or right away

"Sloane, add a workout tracker to the workshop" works too (/idea is on the
actions list). The review itself -- what changed, the tests, Accept or Deny --
is in the control room (/app → Workshop), where he can see it properly. The
building is sloane/workshop.py; this is the conversation around it.
"""

from __future__ import annotations

import hashlib

from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

LISTED = ("ready", "building", "deploying", "queued", "planned", "idea")
LABEL = {"ready": "ready for you", "building": "building", "deploying": "going live", "queued": "queued",
         "planned": "planned", "idea": "idea"}


class WorkshopSkill(Skill):
    name = "workshop"
    help = (
        "`/idea <something you want me to have>` — into the workshop: I plan it, build it, you approve it",
        "`/workshop` — what's ready for you, building and planned; `/workshop build <n> [now]`",
    )
    commands = frozenset({"idea", "workshop"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        from sloane.workshop import Workshop

        self.shop = Workshop(ctx.store, ctx.config, ctx.router)

    async def _listed(self) -> list[dict]:
        rows = await self.ctx.store.workshop_items(LISTED, limit=40)
        order = {status: i for i, status in enumerate(LISTED)}
        return sorted(rows, key=lambda r: (order.get(r["status"], 9), r["created_at"]))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "idea":
            if not rest:
                return Answer("What's the idea? Try /idea a skill that tracks my workouts.")
            row = await self.shop.add_idea(rest)
            return Answer(f"It's in the workshop: {row['title']}.",
                          "I'll write a plan for it. Say `/workshop build <n>` (or press Build in the control room) "
                          "and I'll build it tonight; nothing goes live until you accept it.")
        rows = await self._listed()
        parts = rest.split()
        if parts[:1] == ["build"] and len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            if not 1 <= n <= len(rows):
                return Answer("Which one? `/workshop` lists them.")
            now = len(parts) > 2 and parts[2].lower() == "now"
            row = await self.shop.queue(str(rows[n - 1]["id"]), now=now)
            if row is None:
                return Answer("That one can't be built from where it is.")
            if not self.shop.ready:
                return Answer(f"Queued: {row['title']}.", "Building needs the GitHub token on the box (DEPLOY 7h).")
            return Answer(f"Building {row['title']} {'now' if now else 'tonight'}.",
                          "I'll tell you when it's ready for your OK.")
        if not rows:
            return Answer("The workshop's empty.", "Add something with `/idea <what you want me to have>`.")
        lines = [f"{i}. {r['title']} — {LABEL.get(r['status'], r['status'])}"
                 + (" (my idea)" if r["origin"] == "her" else "") for i, r in enumerate(rows, 1)]
        ready = sum(r["status"] == "ready" for r in rows)
        return Answer(f"{ready} ready for you." if ready else f"{len(rows)} in the workshop.",
                      "\n".join(lines) + "\n\nReview and accept in the control room (/app → Workshop).")

    async def facts(self) -> list[str]:
        rows = await self._listed()
        if not rows:
            return []
        by: dict[str, list[str]] = {}
        for r in rows:
            by.setdefault(r["status"], []).append(safe_field(r["title"], limit=80))
        return ["- WORKSHOP " + "; ".join(f"{LABEL.get(s, s)}: {', '.join(t[:5])}" for s, t in by.items())]

    async def panel(self) -> dict | None:
        rows = [r for r in await self._listed() if r["status"] in ("ready", "building", "deploying")]
        if not rows:
            return None
        return {"title": "Workshop", "lines": [f"{r['title']} — {LABEL[r['status']]}" for r in rows[:6]]}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        if not 7 <= now.hour < 11:
            return []
        ready = [r for r in await self._listed() if r["status"] == "ready"]
        if not ready:
            return []
        ids = hashlib.sha1("".join(sorted(str(r["id"]) for r in ready)).encode()).hexdigest()[:10]
        mine = sum(r["origin"] == "her" for r in ready)
        titles = ", ".join(r["title"] for r in ready[:3]) + (" and more" if len(ready) > 3 else "")
        what = f"{len(ready)} thing{'s' if len(ready) != 1 else ''}"
        note = f" ({mine} of them my own idea{'s' if mine != 1 else ''})" if mine else ""
        return [Nudge(f"workshop:ready:{ids}",
                      f"🔧 {what} in the workshop ready for you{note}: {titles}. Accept or deny in the control room.")]


def build(ctx: SkillContext) -> Skill:
    return WorkshopSkill(ctx)
