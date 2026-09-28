"""The router must degrade past a failing provider, and account for every attempt."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage
from sloane.router import NoProviderAvailable, Router

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Fake(Provider):
    """A provider that answers or fails on command, and counts its calls."""

    def __init__(self, name: str, *, fails: bool, configured: bool = True) -> None:
        self.name = name
        self._fails = fails
        self._configured = configured
        self.calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    async def complete(self, system, prompt, *, max_tokens=1024) -> Completion:
        self.calls += 1
        if self._fails or not self._configured:  # unset, like the real one, it can't answer
            raise ProviderError(self.name, "down" if self._fails else "not set up")
        return Completion(
            text=f"answer from {self.name}",
            usage=Usage(provider=self.name, model=f"{self.name}-model", latency_ms=7),
        )


def harness(broken: set[str], *, local: bool = False, main: str = "claude_code"):
    """Build a router whose named providers fail, plus the usage rows it logs.

    `local`: whether a local model is set up (unset, like a real box without
    LOCAL_BASE_URL, the lanes pass over it)."""
    made: dict[str, Fake] = {}
    rows: list[tuple[str, bool, str | None, str | None]] = []
    purposes: list[str] = []

    def factory(name: str, config: Settings, bulk: bool) -> Fake:
        if name not in made:
            made[name] = Fake(name, fails=name in broken, configured=local or name != "local")
        return made[name]

    async def sink(usage: Usage, purpose: str, ok: bool, error: str | None,
                   degraded_from: str | None):
        rows.append((usage.provider, ok, error, degraded_from))
        purposes.append(purpose)

    config = Settings(main_provider=main, bulk_provider="groq", database_url="")
    return Router(config, usage_sink=sink, factory=factory), made, rows, purposes


def run(coro):
    return asyncio.run(coro)


# --- the happy path uses the configured provider and stops there -------------
router, made, rows, purposes = harness(broken=set())
check("healthy main lane answer", run(router.reply("s", "p")), "answer from claude_code")
check("no fallback was touched", sorted(made), ["claude_code"])
check("one usage row", len(rows), 1)
check("row is not marked degraded", rows[0], ("claude_code", True, None, None))

# --- one failure degrades to the next provider in the lane ------------------
router, made, rows, purposes = harness(broken={"claude_code"})
check("degrades past one failure", run(router.reply("s", "p")), "answer from anthropic")
check("the failure was logged", rows[0], ("claude_code", False, "down", None))
check("the success records where it came from", rows[1], ("anthropic", True, None, "claude_code"))

# --- two failures degrade twice ---------------------------------------------
router, made, rows, purposes = harness(broken={"claude_code", "anthropic"})
check("degrades twice", run(router.reply("s", "p")), "answer from groq")
check("three attempts accounted for", len(rows), 3)
check("last attempt succeeded on groq", rows[2], ("groq", True, None, "claude_code"))

# --- the whole lane down must raise, never fabricate ------------------------
router, made, rows, purposes = harness(broken={"claude_code", "anthropic", "groq"})
try:
    run(router.reply("s", "p"))
    FAILURES.append("a fully-broken lane returned instead of raising")
except NoProviderAvailable:
    pass
check("every failure was accounted for", len(rows), 3)
check("every row is a failure", [r[1] for r in rows], [False, False, False])

# --- a local model: the last fallback when set up, and first when chosen ------------
router, made, rows, purposes = harness(broken={"claude_code", "anthropic", "groq"}, local=True)
check("with every cloud lane down, a local model answers", run(router.reply("s", "p")), "answer from local")
check("and says what it stood in for", rows[-1], ("local", True, None, "claude_code"))
router, made, rows, purposes = harness(broken=set(), local=True, main="local")
check("MAIN_PROVIDER=local asks it first", (run(router.reply("s", "p")), sorted(made)),
      ("answer from local", ["local"]))
router, made, rows, purposes = harness(broken=set(), main="local")
try:
    run(router.reply("s", "p"))
except NoProviderAvailable:
    pass
check("chosen but not set up, it's a real failure, not a silent skip", rows[0][:2], ("local", False))

# --- the purpose must reach the usage log, or /usage cannot split the lanes --
router, made, rows, purposes = harness(broken=set())
run(router.reply("s", "p"))
check("the main lane logs purpose 'reply'", purposes, ["reply"])

# --- the bulk lane has its own order ----------------------------------------
router, made, rows, purposes = harness(broken={"groq"})
check("bulk degrades groq -> anthropic", run(router.bulk("s", "p")), "answer from anthropic")
check("the bulk lane logs purpose 'bulk' on every attempt", purposes, ["bulk", "bulk"])

# --- a broken usage sink must not break the reply ---------------------------
def exploding_factory(name: str, config: Settings, bulk: bool) -> Fake:
    return Fake(name, fails=False)


async def exploding_sink(*_args):
    raise RuntimeError("supabase hiccup")


router = Router(
    Settings(main_provider="groq", bulk_provider="groq", database_url=""),
    usage_sink=exploding_sink,
    factory=exploding_factory,
)
check("a failing usage sink cannot eat the reply", run(router.reply("s", "p")), "answer from groq")

# --- an unknown provider name is skipped, not fatal ------------------------
# A typo in MAIN_PROVIDER must not take the lane down: the router logs it and
# carries on to the providers that do exist.
def typo_factory(name: str, config: Settings, bulk: bool) -> Fake:
    if name == "typo_provider":
        raise ValueError(f"unknown provider {name!r}")
    return Fake(name, fails=False)


router = Router(
    Settings(main_provider="typo_provider", bulk_provider="groq", database_url=""),
    factory=typo_factory,
)
check("a typo'd provider falls through to a real one", run(router.reply("s", "p")), "answer from claude_code")

# ...but if nothing in the lane can be built, it raises rather than inventing.
def all_typos(name: str, config: Settings, bulk: bool) -> Fake:
    raise ValueError(f"unknown provider {name!r}")


router = Router(
    Settings(main_provider="typo_provider", bulk_provider="groq", database_url=""),
    factory=all_typos,
)
try:
    run(router.reply("s", "p"))
    FAILURES.append("an unbuildable lane should raise")
except NoProviderAvailable:
    pass

# -- the Claude CLI gets no tools, no MCP, and none of Sloane's secrets --------
import os as _os

from sloane.providers import claude_code as _cc

_os.environ.update({
    "GMAIL_REFRESH_TOKEN": "secret-1", "TELEGRAM_BOT_TOKEN": "secret-2",
    "DATABASE_URL": "secret-3", "ANTHROPIC_API_KEY": "secret-4", "CANVAS_TOKEN": "secret-5",
})
_env = _cc._cli_env()
check("no Sloane credential reaches the CLI",
      [k for k in _env if _env[k].startswith("secret-")], [])
check("but it can still find itself and its login", ("PATH" in _env, "HOME" in _env), (True, True))

from fake_cli import FakeCli  # noqa: E402

cli = FakeCli()
_real = _cc.asyncio.create_subprocess_exec
_cc.asyncio.create_subprocess_exec = cli.exec
_cc.shutil.which = lambda name: "/usr/bin/claude"
try:
    run(_cc.ClaudeCodeProvider(Settings(database_url="")).complete("s", "p"))
finally:
    _cc.asyncio.create_subprocess_exec = _real
argv, kw = cli.calls[0]
check("tools are switched off", argv[argv.index("--tools") + 1] if "--tools" in argv else None, "")
check("MCP servers are not loaded", "--strict-mcp-config" in argv, True)
check("the environment is the scrubbed one", kw.get("env") == _env, True)
check("her persona replaces Claude Code's prompt, never appends to it",
      (argv[argv.index("--system-prompt") + 1] if "--system-prompt" in argv else None,
       "--append-system-prompt" in argv), ("s", False))
check("fast start: no session files, no slash commands, no user settings",
      all(flag in argv for flag in _cc.FAST_FLAGS), True)

# An older CLI that rejects a flag gets the minimal set, once, and keeps it.
old = FakeCli(reject={"--disable-slash-commands", "--input-format"})
_cc.asyncio.create_subprocess_exec = old.exec
try:
    got = run(_cc.ClaudeCodeProvider(Settings(database_url="")).complete("s", "p"))
    check("an old CLI still answers", got.text, "hello")
    run(_cc.ClaudeCodeProvider(Settings(database_url="")).complete("s", "p"))
    check("streaming, then fast flags, then the minimal set, and the minimal set from then on",
          [("--input-format" in c, "--disable-slash-commands" in c) for c, _ in old.calls],
          [(True, True), (False, True), (False, False), (False, False)])
    check("still tool-less", all(c[c.index("--tools") + 1] == "" for c, _ in old.calls), True)
finally:
    _cc.asyncio.create_subprocess_exec = _real
    _cc.ClaudeCodeProvider.legacy = _cc.ClaudeCodeProvider.no_stream = False

# -- request URLs carry credentials; httpx must not log them -------------------
import logging as _logging

import sloane.main  # noqa: F401 - create_app() runs at import and sets the levels

check("httpx request lines (which carry the bot token) are not logged",
      _logging.getLogger("httpx").getEffectiveLevel() >= _logging.WARNING, True)
check("nor httpcore's", _logging.getLogger("httpcore").getEffectiveLevel() >= _logging.WARNING, True)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("router: degradation and accounting pass")
