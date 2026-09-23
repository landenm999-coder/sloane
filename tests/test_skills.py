"""The skills registry: commands, rule matches, sessions, FACTS, panels, nudges.

DESTRUCTIVE: truncates skill_sessions.

The unit half runs against an in-memory store; the integration half runs the
same flows through the real session table and the real bot.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Answer, Nudge, Registry, Skill, SkillContext, load
from sloane.telegram import BUILTIN_COMMANDS

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class MemorySessions:
    """Just the four session methods, in memory."""

    def __init__(self) -> None:
        self.open: dict | None = None
        self.n = 0

    async def active_session(self, idle_minutes):
        return self.open

    async def start_session(self, skill, state):
        self.n += 1
        self.open = {"id": str(self.n), "skill": skill, "state": state}
        return self.open

    async def touch_session(self, session_id, state):
        if self.open and self.open["id"] == session_id:
            self.open = {**self.open, "state": state}

    async def end_session(self, session_id=None):
        ended, self.open = self.open, None
        return ended


class Counter(Skill):
    """Counts to three in a session; answers /count and 'count for me'."""

    name = "counter"
    help = ("`/count` — count to three",)
    commands = frozenset({"count"})

    async def command(self, name, rest):
        await self.begin_session({"n": 0})
        return Answer("Say anything.")

    async def match(self, text):
        return Answer("counting") if text == "count for me" else None

    async def session(self, text, state):
        n = state["n"] + 1
        return Answer(str(n)), ({"n": n} if n < 3 else None)

    async def facts(self):
        return ["- COUNTER at zero"]

    async def panel(self):
        return {"n": 0}

    async def nudges(self):
        return [Nudge("counter:hello", "hello")]


class Broken(Skill):
    name = "broken"
    commands = frozenset({"break"})

    async def command(self, name, rest):
        raise RuntimeError("boom")

    async def match(self, text):
        raise RuntimeError("boom")

    async def facts(self):
        raise RuntimeError("boom")

    async def panel(self):
        raise RuntimeError("boom")

    async def nudges(self):
        raise RuntimeError("boom")


def registry(store) -> Registry:
    ctx = SkillContext(store=store, config=isolated())
    return Registry([Broken(ctx), Counter(ctx)], ctx)


async def unit() -> None:
    store = MemorySessions()
    reg = registry(store)

    check("names in load order", reg.names, ["broken", "counter"])
    check("/end is offered once a skill has sessions", "`/end` — stop a quiz or practice session" in reg.help_lines(), True)
    check("help lines come from the skills", "`/count` — count to three" in reg.help_lines(), True)
    check("an unknown command is not ours", await reg.command("nope", ""), None)

    failed = await reg.command("break", "")
    check("a crashing command answers instead of raising", failed.speech, "/break hit an error, so I didn't do it.")
    check("and says why", "boom" in failed.detail, True)

    check("a crashing match falls through to the next skill", (await reg.route("count for me")).speech, "counting")
    check("nothing matches: the agent answers", await reg.route("what's due?"), None)

    check("a command can open a session", (await reg.command("count", "")).speech, "Say anything.")
    check("the session claims the next message", (await reg.route("what's due?")).speech, "1")
    check("state carries between messages", (await reg.route("hi")).speech, "2")
    check("the last step ends it", ((await reg.route("hi")).speech, store.open), ("3", None))
    check("after it ends, messages go back to the agent", await reg.route("what's due?"), None)

    await reg.command("count", "")
    check("/end stops a session", ((await reg.command("end", "")).speech, store.open), ("Ended counter.", None))
    check("/end with nothing running says so", (await reg.command("end", "")).speech, "Nothing is running.")

    await store.start_session("gone", {})
    check("a session from a removed skill is dropped", (await reg.route("count for me")).speech, "counting")
    check("and closed", store.open, None)

    lines, notes = await reg.facts()
    check("facts from working skills", lines, ["- COUNTER at zero"])
    check("a failing skill is a note, not silence", notes, ["broken could not be read this turn"])
    check("panels mark a broken skill", await reg.panels(), {"broken": {"error": "unavailable"}, "counter": {"n": 0}})
    check("nudges skip a broken skill", await reg.nudges(), [Nudge("counter:hello", "hello")])

    ctx = SkillContext(store=store, config=isolated())
    try:
        Registry([Counter(ctx), Counter(ctx)], ctx)
    except ValueError:
        pass
    else:
        FAILURES.append("two skills claiming /count must be refused")

    # Every real skill in the package loads without a network or keys, and none
    # claims a built-in command or /end.
    real = load(SkillContext(store=store, config=isolated()))
    clash = sorted(real.command_names & BUILTIN_COMMANDS)
    check("no skill claims a built-in command", clash, [])
    check("/end is the registry's", "end" in real.command_names, True)

    # The agent puts skill lines in FACTS, and a failing skill in NOTES.
    from sloane.agent import Agent
    from sloane.memory.embed import EmbedUnavailable
    from sloane.providers.base import Completion, Provider, Usage
    from sloane.router import Router

    class EmptyStore(MemorySessions):
        async def get_state(self): return []
        async def get_working_set(self): return []
        async def assignments_due(self, start, end, **_): return []
        async def overdue_assignments(self): return []
        async def shifts_between(self, start, end): return []
        async def courses(self): return []
        async def open_commitments(self): return []
        async def events_between(self, start, end): return []
        async def reminders_between(self, start, end): return []
        async def search_episodes(self, *a, **k): return []
        async def add_episode(self, *a, **k): return "x"
        async def log_usage(self, **k): return None

    class NoEmbedder:
        async def embed_one(self, text): raise EmbedUnavailable("not here")
        async def embed(self, texts): raise EmbedUnavailable("not here")

    class Recorder(Provider):
        name = "recorder"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def complete(self, system, prompt, *, max_tokens=1024):
            self.prompts.append(prompt)
            return Completion(text='{"speech": "ok", "detail": "ok"}', usage=Usage(provider=self.name))

    recorder = Recorder()
    config = isolated(timezone="America/Denver")
    agent = Agent(EmptyStore(), config, router=Router(config, factory=lambda n, c, b: recorder),
                  embedder=NoEmbedder(), skills=registry(MemorySessions()))
    await agent.answer("anything new?")
    prompt = recorder.prompts[0]
    check("skill facts reach FACTS", "- COUNTER at zero" in prompt.split("FACTS", 1)[-1], True)
    check("a failing skill reaches NOTES", "broken could not be read this turn" in prompt, True)


async def integration() -> None:
    from sloane.memory.store import Store
    from sloane.telegram import Bot

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      telegram_chat_id=42, telegram_bot_token="x")
    async with Store(config) as store:
        await store._exec("truncate skill_sessions")
        check("no session to start with", await store.active_session(30), None)
        first = await store.start_session("counter", {"n": 0})
        second = await store.start_session("counter", {"n": 5})
        open_now = await store.active_session(30)
        check("starting one ends the other", str(open_now["id"]), str(second["id"]))
        check("state round-trips as JSON", open_now["state"], {"n": 5})
        await store.touch_session(str(second["id"]), {"n": 6})
        check("touch updates state", (await store.active_session(30))["state"], {"n": 6})
        check("ending a closed session is a no-op", await store.end_session(str(first["id"])), None)
        await store._exec("update skill_sessions set touched_at = now() - interval '2 hours'")
        check("an idle session expires", await store.active_session(30), None)

        # Through the bot: /count opens a session, plain messages go to it.
        ctx = SkillContext(store=store, config=config)
        bot = Bot(store, None, config, skills=Registry([Counter(ctx)], ctx))
        sent: list = []

        async def capture(chat_id, reply, *, as_voice=False):
            sent.append(reply.speech)

        async def send(chat_id, reply):
            sent.append(reply.speech)

        bot.reply = capture
        bot.send = send
        n = 1000

        async def message(text):
            nonlocal n
            n += 1
            await bot._handle({"update_id": n, "message": {"chat": {"id": 42}, "text": text}})

        await message("/count")
        await message("hello")
        await message("hello")
        await message("hello")
        check("the bot routes a whole session", sent, ["Say anything.", "1", "2", "3"])
        help_reply = await bot._handle_command("/help")
        check("/help lists skill commands", "`/count` — count to three" in help_reply.detail, True)


asyncio.run(unit())
if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("skills: commands, rules, sessions, FACTS, panels, nudges, and no clash with built-ins")
