"""Skills: self-contained capabilities that plug into Sloane without editing the core.

A skill is one module in this package with a `build(ctx) -> Skill | None`
function. It is discovered at startup; `build` returns None when the skill is
not configured (no API key, no location), and that skill is simply absent.
A skill may offer any of these:

    commands   slash commands it answers (/list, /weather ...), from its own rules
    match      plain messages it answers without a model ("add milk to my
               grocery list"), tried after the built-in rules and before the agent
    session    an ongoing mode (a quiz) that claims his next plain messages until
               it ends, he sends /end, or it sits idle for SESSION_IDLE_MINUTES.
               A rule that opens one from plain words sets match_opens_session,
               so a Capture note never starts one
    facts      lines for FACTS, the exact tier 4 block every answer and brief reads
    panel      a JSON-able dict for the TV dashboard: {"title": ..., "lines": [...]}
               is what /tv renders; anything else rides along in /panels
    nudges     things worth saying unprompted right now (the heartbeat job asks)

Rules every skill keeps, because the core invariants apply here too:

* Skills return `Answer(speech, detail)`, never a `Reply` (invariant 6); the bot
  turns an Answer into a Reply. Speech is read aloud: at most two sentences, no
  markdown, no URLs.
* SQL lives in memory/store.py (invariant 2); a skill calls `ctx.store` methods.
* Model calls go through `ctx.router` (invariant 1).
* `facts()` lines are evidence. Only rows from SQL or structured numbers from an
  API (a temperature, a count) may go there. Third-party free text -- a headline,
  a web page, an email -- never does; it is INGESTED data, not FACTS.
* A skill never crosses a hard line and never acts on its own initiative except
  through `Agency.propose()` (invariant 9). Something Landen explicitly asked for
  in the same message is its own approval.
* A failing skill costs its own feature, never the reply: the registry catches
  and logs, and the message still reaches the agent.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import re
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sloane.config import Settings

log = logging.getLogger(__name__)

# A session untouched this long is over. A quiz he walked away from must not
# swallow the next "what's due tonight?".
SESSION_IDLE_MINUTES = 30

# Handled by the registry itself, for whichever skill holds the session.
END_COMMAND = "end"

# "Now", for one routed message that was said earlier than it arrived (a Capture
# note queued offline). A context variable, so concurrent messages never see
# each other's time.
_AS_OF: ContextVar[datetime | None] = ContextVar("sloane_skill_as_of", default=None)


# "Sloane, add milk to the list": her name in front is how he talks to her, not
# part of what he asked, and no skill's rule should have to know it.
_ADDRESSED = re.compile(r"^\s*(?:(?:hey|hi|ok|okay|yo)[\s,!]+)?sloane\b[\s,:!.-]*", re.I)


_TOLD = re.compile(r"^\s*(?:please\s+)?(?:remember|don'?t\s+forget|keep\s+in\s+mind)\s+(?:that\s+)?(?P<rest>(?!to\b).+)$",
                   re.I | re.S)


def unaddressed(text: str) -> str:
    """The message without "Sloane," (or "hey Sloane,") in front."""
    stripped = _ADDRESSED.sub("", text, count=1)
    return stripped if stripped.strip() else text


def cap(text: str) -> str:
    """First letter up, the rest as he typed it: "DECA prep", not "Deca prep"."""
    return text[:1].upper() + text[1:]


@dataclass(frozen=True)
class Answer:
    """What a skill says back. The bot builds the Reply from it."""

    speech: str
    detail: str = ""


@dataclass(frozen=True)
class Nudge:
    """Something worth saying unprompted, now.

    `key` identifies it: the heartbeat says a given key once, so a skill can
    return the same nudge every tick until its condition clears without it
    being repeated.
    """

    key: str
    text: str


@dataclass
class SkillContext:
    """What a skill may use. Built once in main.py and shared by every skill."""

    store: Any  # sloane.memory.store.Store; Any so skills stay importable without a DB
    config: Settings
    router: Any = None  # sloane.router.Router
    embedder: Any = None
    # Plain text to Landen's chat. None until the bot exists (or with no owner).
    say: Callable[[str], Awaitable[None]] | None = None
    # What time it is. None means the real clock; tests pin it.
    clock: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        """Now, in Landen's timezone. Skills read the time only through here."""
        zone = ZoneInfo(self.config.timezone)
        pinned = _AS_OF.get()
        if pinned is not None:
            return pinned.astimezone(zone)
        return self.clock().astimezone(zone) if self.clock is not None else datetime.now(zone)

    def today(self) -> date:
        return self.now().date()


class Skill:
    """Base class. Override what the skill offers; the defaults offer nothing."""

    name: str = ""
    # Lines for /help, e.g. "`/list grocery` — show a list".
    help: tuple[str, ...] = ()
    # Slash command names (without the slash) routed to `command()`.
    commands: frozenset[str] = frozenset()
    # True if `match()` can open a session ("let's do a roleplay"). Such rules
    # are skipped for a Capture note, which is never a conversation.
    match_opens_session: bool = False

    def __init__(self, ctx: SkillContext) -> None:
        self.ctx = ctx

    async def command(self, name: str, rest: str) -> Answer | None:
        """A slash command from `commands`. `rest` is everything after it."""
        return None

    async def match(self, text: str) -> Answer | None:
        """A plain message. Return None unless it is unmistakably this skill's."""
        return None

    async def session(self, text: str, state: dict) -> tuple[Answer, dict | None]:
        """The next message of an open session. Return the new state, or None to end it."""
        raise NotImplementedError(f"{self.name} has no sessions")

    async def facts(self) -> list[str]:
        """Exact lines for FACTS, each starting with '- '."""
        return []

    async def panel(self) -> dict | None:
        """Data for the TV dashboard, or None to show nothing."""
        return None

    async def nudges(self) -> list[Nudge]:
        """Things worth saying now. Called every heartbeat; keep it cheap."""
        return []

    # -- helpers ---------------------------------------------------------------

    async def begin_session(self, state: dict) -> None:
        """Open a session for this skill, ending any other."""
        await self.ctx.store.start_session(self.name, state)


class Registry:
    """Every loaded skill, and the one place the rest of Sloane talks to them."""

    def __init__(self, skills: Iterable[Skill], ctx: SkillContext) -> None:
        self.ctx = ctx
        self.skills: list[Skill] = list(skills)
        self._by_name = {s.name: s for s in self.skills}
        self._by_command: dict[str, Skill] = {}
        for skill in self.skills:
            for name in skill.commands:
                if name in self._by_command or name == END_COMMAND:
                    raise ValueError(f"/{name} is claimed twice ({skill.name})")
                self._by_command[name] = skill

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.skills]

    @property
    def command_names(self) -> frozenset[str]:
        return frozenset(self._by_command) | {END_COMMAND}

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    def help_lines(self) -> list[str]:
        lines = [line for s in self.skills for line in s.help]
        if any(type(s).session is not Skill.session for s in self.skills):
            lines.append("`/end` — stop a quiz or practice session")
        return lines

    # -- messages --------------------------------------------------------------

    async def command(self, name: str, rest: str) -> Answer | None:
        """A slash command, if a skill owns it. None means not ours."""
        if name == END_COMMAND:
            ended = await self.ctx.store.end_session()
            if ended is None:
                return Answer("Nothing is running.")
            return Answer(f"Ended {ended['skill']}.")
        skill = self._by_command.get(name)
        if skill is None:
            return None
        try:
            return await skill.command(name, rest)
        except Exception as exc:  # noqa: BLE001 - one skill must not cost the bot
            log.exception("skill %s failed on /%s", skill.name, name)
            return Answer(f"/{name} hit an error, so I didn't do it.", detail=f"{type(exc).__name__}: {exc}"[:300])

    async def route(self, text: str, *, sessions: bool = True, at: datetime | None = None) -> Answer | None:
        """See `_route`. `at` is when he said it, if that was earlier than now."""
        token = _AS_OF.set(at) if at is not None else None
        try:
            return await self._route(text, sessions=sessions)
        finally:
            if token is not None:
                _AS_OF.reset(token)

    async def _route(self, text: str, *, sessions: bool = True) -> Answer | None:
        """A plain message: an open session first, then each skill's rules.

        None means no skill wants it, and the agent answers as usual. With
        `sessions=False` (a Capture note, not a chat reply) an open quiz never
        takes it as an answer; only the rules are tried.
        """
        row = None
        try:
            if sessions:
                row = await self.ctx.store.active_session(SESSION_IDLE_MINUTES)
        except Exception:  # noqa: BLE001 - no session state is not no reply
            log.exception("could not read the open skill session")
            row = None
        if row is not None:
            skill = self._by_name.get(row["skill"])
            if skill is None:  # a session left by a skill that is gone now
                await self.ctx.store.end_session(str(row["id"]))
            else:
                try:
                    answer, state = await skill.session(text, dict(row.get("state") or {}))
                except Exception as exc:  # noqa: BLE001
                    log.exception("skill %s session failed", skill.name)
                    await self.ctx.store.end_session(str(row["id"]))
                    return Answer(f"The {skill.name} session hit an error, so I ended it.",
                                  detail=f"{type(exc).__name__}: {exc}"[:300])
                if state is None:
                    await self.ctx.store.end_session(str(row["id"]))
                else:
                    await self.ctx.store.touch_session(str(row["id"]), state)
                return answer

        plain = unaddressed(text)
        # "Remember that Maya's birthday is March 3" is a birthday: what follows
        # "remember that" goes to the other skills first, and only if none
        # takes it does the memory skill keep it as a note.
        told = _TOLD.match(plain)
        if told is not None:
            for skill in self.skills:
                if skill.name == "memory" or (not sessions and skill.match_opens_session):
                    continue
                try:
                    answer = await skill.match(told.group("rest"))
                except Exception:  # noqa: BLE001
                    log.exception("skill %s failed to match", skill.name)
                    continue
                if answer is not None:
                    return answer
        for skill in self.skills:
            if not sessions and skill.match_opens_session:
                continue
            try:
                answer = await skill.match(plain)
            except Exception:  # noqa: BLE001 - a broken rule falls through to the agent
                log.exception("skill %s failed to match", skill.name)
                continue
            if answer is not None:
                return answer
        return None

    # -- context, dashboard, heartbeat ------------------------------------------

    async def facts(self) -> tuple[list[str], list[str]]:
        """(lines, notes). A skill that fails says so in notes, never silently."""
        settled = await asyncio.gather(*(s.facts() for s in self.skills), return_exceptions=True)
        lines: list[str] = []
        notes: list[str] = []
        for skill, result in zip(self.skills, settled):
            if isinstance(result, BaseException):
                log.warning("skill %s facts failed: %s", skill.name, result)
                notes.append(f"{skill.name} could not be read this turn")
            else:
                lines.extend(result)
        return lines, notes

    async def panels(self) -> dict[str, Any]:
        settled = await asyncio.gather(*(s.panel() for s in self.skills), return_exceptions=True)
        out: dict[str, Any] = {}
        for skill, result in zip(self.skills, settled):
            if isinstance(result, BaseException):
                log.warning("skill %s panel failed: %s", skill.name, result)
                out[skill.name] = {"error": "unavailable"}
            elif result is not None:
                out[skill.name] = result
        return out

    async def nudges(self) -> list[Nudge]:
        settled = await asyncio.gather(*(s.nudges() for s in self.skills), return_exceptions=True)
        out: list[Nudge] = []
        for skill, result in zip(self.skills, settled):
            if isinstance(result, BaseException):
                log.warning("skill %s nudges failed: %s", skill.name, result)
            else:
                out.extend(result)
        return out


def load(ctx: SkillContext) -> Registry:
    """Discover every skill module here and build the configured ones.

    A module that fails to import or build is logged and skipped: a broken
    skill costs its feature, not the process.
    """
    disabled = ctx.config.disabled_skills
    skills: list[Skill] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
            build = getattr(module, "build", None)
            skill = build(ctx) if build is not None else None
        except Exception:  # noqa: BLE001
            log.exception("skill module %s failed to load", info.name)
            continue
        if skill is None or skill.name in disabled:
            continue
        skills.append(skill)
    registry = Registry(skills, ctx)
    log.info("skills loaded: %s", ", ".join(registry.names) or "none")
    return registry
