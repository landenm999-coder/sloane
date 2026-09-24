"""Context assembly to budget, then one turn.

The shape of a turn:

    1. read every tier (tier 4 by SQL, tier 3 by decayed similarity)
    2. render each into its own budget
    3. one model call through the router
    4. parse into a Reply
    5. persist the episode -- and if that fails, still return the Reply

Step 5 is the one people get wrong. A Supabase hiccup must not eat the morning
brief, so every write here goes through `remember()`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sloane.config import Settings, settings as default_settings
from sloane.contract import Reply, parse
from sloane.jobs.conflicts import find as find_conflicts, render as render_conflicts
from sloane.memory.embed import Embedder, EmbedUnavailable
from sloane.memory.store import Store, remember
from sloane.memory.tiers import assemble, usage_sink
from sloane.persona import system_prompt
from sloane.router import NoProviderAvailable, Router
from sloane.skills import Registry

log = logging.getLogger(__name__)

# How far ahead FACTS looks. Wide enough that "what's due Friday" and "what's
# due next week" are both answered from exact rows rather than from recall.
FACTS_HORIZON_DAYS = 8


# --- hard lines ---------------------------------------------------------------
#
# THIS is the gate. The six rows in the `trust` table are a record of it, for
# auditing and for the P4 UI; they are not consulted here, because a gate that
# needs a working database is a gate that opens when the database is down.
#
# Keep this tuple and the seed rows in sql/001_init.sql in step.

HARD_LINES: frozenset[tuple[str, str]] = frozenset(
    {
        ("submit", "schoolwork"),
        ("place", "trade"),
        ("move", "money"),
        ("delete", "anything"),
        ("contact", "school_staff"),
        ("publish", "public"),
    }
)


def _stamp(moment: datetime) -> str:
    """'Tuesday September 22, 2026, 8:04 PM' -- unambiguous, and portable."""
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return (
        f"{moment:%A %B} {moment.day}, {moment:%Y}, "
        f"{hour}:{moment.minute:02d} {meridiem}"
    )


def _without_current(rows: list, question: str) -> list:
    """The conversation minus the message being answered (it's logged first)."""
    rows = list(rows)
    if rows and rows[-1].get("direction") == "in" and (rows[-1].get("body") or "").strip() == question.strip():
        rows.pop()
    return rows


class HardLineViolation(PermissionError):
    """An action crossed a hard line. Raised before anything executes."""

    def __init__(self, action: str, target: str) -> None:
        super().__init__(f"hard line: {action} -> {target} is never permitted")
        self.action = action
        self.target = target


def ensure_allowed(action: str, target: str) -> None:
    """Pre-execution check. Call before any side effect, not after.

    Both the exact pair and the wildcard target are refused, so ('delete',
    'anything') blocks every delete rather than only a literal target named
    "anything".
    """
    pair = (action.strip().lower(), target.strip().lower())
    if pair in HARD_LINES or (pair[0], "anything") in HARD_LINES:
        raise HardLineViolation(*pair)


class Agent:
    def __init__(
        self,
        store: Store,
        config: Settings | None = None,
        *,
        router: Router | None = None,
        embedder: Embedder | None = None,
        skills: Registry | None = None,
    ) -> None:
        self._config = config or default_settings()
        self._store = store
        self._router = router or Router(self._config, usage_sink=usage_sink(store))
        self._embedder = embedder or Embedder(self._config)
        self.skills = skills

    # -- reading ---------------------------------------------------------------

    async def _facts(self, today: date) -> tuple[dict, list[str]]:
        """Tier 4 and tiers 1-2, read concurrently. Notes record what failed.

        A failed read is never silently an empty result: an empty FACTS block
        and an unreadable FACTS block look identical to the model, and one of
        them means "nothing is due" while the other means "I do not know".
        """
        horizon = today + timedelta(days=FACTS_HORIZON_DAYS)
        reads = {
            "state": self._store.get_state(),
            "working_set": self._store.get_working_set(),
            "assignments": self._store.assignments_due(today, horizon),
            "overdue": self._store.overdue_assignments(),
            "shifts": self._store.shifts_between(today, horizon),
            "courses": self._store.courses(),
            "commitments": self._store.open_commitments(),
            "events": self._store.events_between(today, horizon),
            "reminders": self._store.reminders_between(
                datetime.now(ZoneInfo(self._config.timezone)),
                datetime.combine(horizon + timedelta(days=1), datetime.min.time(),
                                 tzinfo=ZoneInfo(self._config.timezone)),
            ),
        }
        if self._config.telegram_chat_id:
            reads["conversation"] = self._store.recent_messages(
                self._config.telegram_chat_id,
                datetime.now(ZoneInfo(self._config.timezone))
                - timedelta(hours=self._config.conversation_hours),
                self._config.conversation_messages,
            )
        if self.skills is not None:
            reads["skills"] = self.skills.facts()
        settled = await asyncio.gather(*reads.values(), return_exceptions=True)

        out: dict = {}
        notes: list[str] = []
        for name, result in zip(reads, settled):
            if isinstance(result, BaseException):
                log.warning("tier read %s failed: %s", name, result)
                out[name] = []
                notes.append(f"{name} could not be read this turn")
            elif name == "skills":
                out[name], skill_notes = result
                notes.extend(skill_notes)
            else:
                out[name] = result
        out.setdefault("skills", [])
        out.setdefault("conversation", [])

        if notes:
            notes.append(
                "Some exact records are missing, so say you cannot confirm rather "
                "than stating any date, time or grade you cannot see."
            )
        return out, notes

    async def _recall(self, question: str) -> list[dict]:
        """Tier 3. Never load-bearing: if it fails, the turn proceeds without it."""
        vector: list[float] | None = None
        try:
            vector = await self._embedder.embed_one(question)
        except EmbedUnavailable as exc:
            # Not fatal any more: the full-text arm answers on its own.
            log.warning("recall running lexical-only, no embedder: %s", exc)
        try:
            # Both arms. The lexical one carries the question's rare tokens --
            # a course name, a teacher, a person -- which is exactly what the
            # vector arm blurs. It also means recall still works when the
            # embedder is unavailable and `vector` is None.
            return await self._store.search_episodes(vector, text=question)
        except Exception:  # noqa: BLE001 - recall is context, not evidence
            log.exception("recall search failed")
            return []

    # -- one turn --------------------------------------------------------------

    async def answer(
        self,
        question: str,
        *,
        ingested: str = "",
        channel: str = "telegram",
        today: date | None = None,
    ) -> Reply:
        """One turn. Returns a Reply even when the model is unreachable."""
        # Landen's clock, never the container's. Docker runs in UTC, and from
        # 6 PM to midnight in Parker it is already tomorrow there -- so "what's
        # due tonight?" at 8 PM would search tomorrow and miss tonight.
        now_local = datetime.now(ZoneInfo(self._config.timezone))
        when = today or now_local.date()

        tiers, notes = await self._facts(when)
        episodes = await self._recall(question)
        conversation = _without_current(tiers["conversation"], question)
        if conversation:
            # What is already in CONVERSATION verbatim needn't come back as RECALL.
            start = conversation[0]["at"]
            episodes = [e for e in episodes if not (e.get("occurred_at") and e["occurred_at"] >= start)]

        # Computed, not inferred. A collision the model happens not to mention
        # is a missed conflict, and "zero missed" is the P2 gate -- so they are
        # found in code and handed over as findings.
        collisions = find_conflicts(
            assignments=tiers["assignments"],
            shifts=tiers["shifts"],
            events=tiers["events"],
        )
        if collisions:
            log.info("%s conflict(s) found for %s", len(collisions), when)

        context = assemble(
            state=tiers["state"],
            working_set=tiers["working_set"],
            assignments=tiers["assignments"],
            overdue=tiers["overdue"],
            shifts=tiers["shifts"],
            courses=tiers["courses"],
            commitments=tiers["commitments"],
            events=tiers["events"],
            reminders=tiers["reminders"],
            skill_facts=tiers["skills"],
            conflicts=render_conflicts(collisions, self._config.timezone),
            episodes=episodes,
            conversation=conversation,
            ingested=ingested,
            config=self._config,
        )
        context.notes.extend(notes)
        # Relative dates ("Friday", "tonight") are only answerable against a
        # stated now. The Claude CLI happens to inject the date into its own
        # prompt; Groq and the API do not, and a model guessing today is a
        # model guessing deadlines.
        context.now = (
            _stamp(now_local)
            if when == now_local.date()
            else f"{when:%A %B} {when.day}, {when:%Y}"
        )

        prompt = context.to_prompt(question)
        log.info(
            "context assembled: %s tokens (%s)",
            context.tokens,
            ", ".join(f"{k}={v}" for k, v in context.spent.items()),
        )

        try:
            raw = await self._router.reply(system_prompt(), prompt)
        except NoProviderAvailable as exc:
            log.error("every provider failed: %s", exc)
            reply = Reply(
                speech="I cannot reach a model right now, so I have not answered that.",
                detail=f"Every provider in the main lane failed: {exc}",
            )
            await self._persist(question, reply, channel=channel, answered=False)
            return reply

        reply = parse(raw)
        await self._persist(question, reply, channel=channel, answered=True)
        return reply

    # -- writing ---------------------------------------------------------------

    async def _persist(
        self,
        question: str,
        reply: Reply,
        *,
        channel: str,
        answered: bool,
    ) -> None:
        """Log both sides of the turn. Never allowed to raise into the caller."""
        vectors: list[list[float]] = []
        try:
            to_embed = [question] + ([reply.detail] if answered and reply.detail else [])
            vectors = await self._embedder.embed(to_embed)
        except EmbedUnavailable as exc:
            log.warning("storing episodes without embeddings: %s", exc)

        await remember(
            "episode(user)",
            self._store.add_episode(
                question,
                role="user",
                channel=channel,
                embedding=vectors[0] if vectors else None,
            ),
        )
        if answered and reply.detail:
            await remember(
                "episode(sloane)",
                self._store.add_episode(
                    reply.detail,
                    role="sloane",
                    channel=channel,
                    summary=reply.speech or None,
                    embedding=vectors[1] if len(vectors) > 1 else None,
                ),
            )
