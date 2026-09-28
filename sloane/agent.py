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
from sloane.memory.tiers import assemble, conversation_that_fits, prioritize_state, usage_sink
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


# The channels a turn of chat is stored under: what CONVERSATION already shows.
# Telegram answers are stored as "text" or "voice" (the kind he sent), so a
# check for "telegram" alone never matched one, and every recent exchange came
# back a second time as RECALL, crowding out older memories.
CHAT_CHANNELS = frozenset({"telegram", "text", "voice", "dashboard"})


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
    # The log keeps the first 4,000 characters (a long voice note is cut).
    if rows and rows[-1].get("direction") == "in" and \
            (rows[-1].get("body") or "").strip() == question.strip()[:4000].strip():
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
        # The nightly learn job asks the bulk lane through the same router,
        # and embeds its diary line with the same embedder.
        self.router = self._router
        self.embedder = self._embedder
        # Memory writes run after the reply is on its way (invariant 5, taken
        # literally). settle() waits for them: shutdown, tests, the eval.
        self._pending: set[asyncio.Task] = set()

    def _later(self, coro) -> None:  # noqa: ANN001
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def settle(self) -> None:
        """Wait for memory writes still under way."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

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
        on_text=None,  # noqa: ANN001 - async (raw text so far) -> None, to show it as it's written
        can_act: bool = False,
    ) -> Reply:
        """One turn. Returns a Reply even when the model is unreachable.

        `can_act` is for his own messages only: the reply may then carry
        commands to run for him (sloane/actions.py). With ingested text in
        the prompt it is off whatever the caller says.
        """
        can_act = can_act and not ingested.strip()
        # Landen's clock, never the container's. Docker runs in UTC, and from
        # 6 PM to midnight in Parker it is already tomorrow there -- so "what's
        # due tonight?" at 8 PM would search tomorrow and miss tonight.
        now_local = datetime.now(ZoneInfo(self._config.timezone))
        when = today or now_local.date()

        tiers, notes = await self._facts(when)
        episodes = await self._recall(question)
        conversation = _without_current(tiers["conversation"], question)
        shown = conversation_that_fits(conversation, self._config.budget_conversation,
                                       tz=self._config.timezone)
        if shown:
            # What CONVERSATION really shows needn't come back as RECALL; what
            # its budget had to drop still may. Only chat episodes are in
            # CONVERSATION: a capture or an email from the same window is not,
            # so it must stay in RECALL or "what did I just capture?" finds
            # nothing.
            start = shown[0]["at"]
            episodes = [
                e for e in episodes
                if not (
                    e.get("channel", "telegram") in CHAT_CHANNELS
                    and e.get("occurred_at")
                    and e["occurred_at"] >= start
                )
            ]

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
            state=prioritize_state(tiers["state"], question),
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
            raw = await self._router.reply(self.system(can_act=can_act), prompt, on_text=on_text)
        except NoProviderAvailable as exc:
            log.error("every provider failed: %s", exc)
            reply = Reply(
                speech="I cannot reach a model right now, so I have not answered that.",
                detail=f"Every provider in the main lane failed: {exc}",
            )
            self._later(self._persist(question, reply, channel=channel, answered=False))
            return reply

        reply = parse(raw)
        if (reply.actions or reply.lookup) and not can_act:
            log.warning("dropped actions/lookup from a turn that may not act")
            reply = Reply(speech=reply.speech, detail=reply.detail)
        origin = "ingested" if ingested.strip() else None
        if reply.lookup:
            if self._config.web_lookup:
                reply = await self._looked_up(question, reply, context, on_text)
                origin = "web" if reply.tainted else origin
            else:
                reply = Reply(speech=reply.speech, detail=reply.detail, actions=reply.actions)
        if origin and not reply.tainted:
            reply = Reply(speech=reply.speech, detail=reply.detail, tainted=True)
        self._later(self._persist(question, reply, channel=channel, answered=True, origin=origin))
        return reply

    async def _looked_up(self, question: str, first: Reply, context, on_text) -> Reply:  # noqa: ANN001
        """Run the web lookup her first reply asked for, then answer from it.

        The results are strangers' text: they go in as INGESTED, and the second
        turn may neither act nor look again.
        """
        from sloane.ingest import safe_field, unfence

        log.info("looking up: %s", first.lookup[:80])
        try:
            found = await self._router.research(first.lookup)
        except NoProviderAvailable as exc:
            log.warning("lookup failed: %s", exc)
            return Reply(speech="I tried to look that up and couldn't get through.",
                         detail=f"The web lookup ({safe_field(first.lookup, limit=120)}) failed: {exc}",
                         actions=first.actions)
        context.ingested = (f"WEB SEARCH for {safe_field(first.lookup, limit=200)!r} -- results from the web, "
                            f"untrusted:\n<<<\n{unfence(found)[:12000]}\n>>>")
        prompt = context.to_prompt(
            question + "\n\n(You looked this up: the results are under INGESTED. Answer from them now, "
            "and say where it came from. Don't look it up again.)"
        )
        try:
            raw = await self._router.reply(self.system(can_act=False), prompt, on_text=on_text)
        except NoProviderAvailable as exc:
            return Reply(speech="I found something but couldn't put the answer together.",
                         detail=f"Every provider failed after the lookup: {exc}", actions=first.actions)
        second = parse(raw)
        return Reply(speech=second.speech, detail=second.detail, actions=first.actions, tainted=True)

    def system(self, *, can_act: bool = False) -> str:
        """Her system prompt: the persona, and what she can do when she may act."""
        extra = ""
        if can_act:
            from sloane import actions

            commands = self.skills.command_names if self.skills is not None else frozenset()
            extra = actions.instructions(actions.available(commands))
            if self._config.web_lookup:
                extra += "\n\n" + actions.LOOKUP
        return system_prompt(extra, address=self._config.address_as)

    async def prewarm(self) -> None:
        """Have the model lane ready before the first message: his messages are
        the ones that can act, so that is the prompt kept warm."""
        await self._router.prewarm(self.system(can_act=True))

    # -- writing ---------------------------------------------------------------

    async def _persist(
        self,
        question: str,
        reply: Reply,
        *,
        channel: str,
        answered: bool,
        origin: str | None = None,
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
            # A reply built from email or the web is strangers' words at one
            # remove: remembered untrusted, so recall never serves it as hers.
            await remember(
                "episode(sloane)",
                self._store.add_episode(
                    reply.detail,
                    role="sloane",
                    channel=channel,
                    summary=reply.speech or None,
                    embedding=vectors[1] if len(vectors) > 1 else None,
                    trusted=not reply.tainted,
                    source=origin,
                ),
            )
