"""The Claude CLI kept warm and streamed: reuse, progress, errors, fallbacks.

No network and no real CLI: tests/fake_cli.py stands in for `claude -p`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_cli import FakeCli

from sloane.config import Settings
from sloane.providers import claude_code as cc
from sloane.providers.base import ProviderError
from sloane.router import Router

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def fresh(cli: FakeCli, **settings) -> cc.ClaudeCodeProvider:
    cc.asyncio.create_subprocess_exec = cli.exec
    cc.shutil.which = lambda name: "/usr/bin/claude"
    cc.ClaudeCodeProvider.legacy = cc.ClaudeCodeProvider.no_stream = False
    cc.ClaudeCodeProvider._idle.clear()
    cc.ClaudeCodeProvider._primary = None
    return cc.ClaudeCodeProvider(Settings(database_url="", **settings))


async def settle() -> None:
    """Let the background refill run."""
    for _ in range(5):
        await asyncio.sleep(0)


async def main() -> None:
    real = cc.asyncio.create_subprocess_exec

    # -- a warm process is started ahead, used once, and replaced ----------------
    cli = FakeCli('{"speech": "Two things are due.", "detail": "lab, essay"}')
    provider = fresh(cli)
    await provider.prewarm("SYSTEM")
    check("prewarm starts one process", len(cli.procs), 1)
    seen: list[str] = []

    async def sink(text):
        seen.append(text)

    got = await provider.stream("SYSTEM", "what's due?", on_text=sink)
    check("the answer", got.text, '{"speech": "Two things are due.", "detail": "lab, essay"}')
    check("the warm process was the one used", cli.procs[0].received.count(b"what's due?"), 1)
    check("its input was closed so it answers once and exits", cli.procs[0].stdin_closed, True)
    check("progress grew a few characters at a time", (len(seen) > 5, seen[-1] == got.text), (True, True))
    check("usage from the result event", (got.usage.prompt_tokens, got.usage.completion_tokens, got.usage.model),
          (11, 7, "claude-sonnet-5"))
    await settle()
    check("a replacement is started behind it", len(cli.procs), 2)
    await provider.stream("SYSTEM", "again")
    await settle()
    check("the replacement answers the next turn", (cli.procs[1].received != b"", len(cli.procs)), (True, 3))

    # -- another system prompt isn't kept warm (cards make, etc.) ------------------
    before = len(cli.procs)
    await provider.stream("OTHER SYSTEM", "make cards")
    await settle()
    check("an unusual prompt starts its own process and no spare", len(cli.procs) - before, 1)

    # -- a warm process that died while idle gets one fresh try ----------------------
    cli = FakeCli("ok", die_idle=True)
    provider = fresh(cli)
    await provider.prewarm("S")
    cli.procs[0].warm_age_ticks = 1  # the idle one's pipe is broken
    got = await provider.stream("S", "hi")
    check("a dead warm process is replaced, not an error", got.text, "ok")

    # -- errors -------------------------------------------------------------------------
    provider = fresh(FakeCli("x", error=True))
    try:
        await provider.stream("S", "hi")
        FAILURES.append("an is_error result must raise")
    except ProviderError as exc:
        check("an error result is a ProviderError the router degrades on", "rate limited" in exc.message, True)

    provider = fresh(FakeCli("slow", delay=0.6), claude_cli_timeout=1)
    try:
        await provider.stream("S", "hi")
        FAILURES.append("a hung process must time out")
    except ProviderError as exc:
        check("a hung process times out", "timed out" in exc.message, True)

    async def broken_sink(text):
        raise RuntimeError("telegram is down")

    provider = fresh(FakeCli("still answered"))
    check("a failing progress sink doesn't cost the reply",
          (await provider.stream("S", "hi", on_text=broken_sink)).text, "still answered")

    # -- an old CLI without streaming falls back to the one-shot call ------------------
    cli = FakeCli("one shot", reject={"--input-format"})
    provider = fresh(cli)
    check("no stream-json: one-shot answer", (await provider.stream("S", "hi", on_text=sink)).text, "one shot")
    check("and streaming is not tried again", "--input-format" in cli.calls[-1][0], False)
    await provider.prewarm("S")
    check("nor kept warm", len(cli.procs), 2)

    # -- through the router, on_text reaches the provider ---------------------------------
    cli = FakeCli('{"speech": "hi", "detail": "hi"}')
    fresh(cli)
    router = Router(Settings(database_url="", main_provider="claude_code"))
    progress: list[str] = []

    async def collect(text):
        progress.append(text)

    await router.prewarm("SYS")
    text = await router.reply("SYS", "hey", on_text=collect)
    check("the router streams", (text, bool(progress)), ('{"speech": "hi", "detail": "hi"}', True))
    # A refill is under way right now; closing must stop it too.
    cc.ClaudeCodeProvider.close_all()
    await settle()
    check("close_all leaves nothing idle, even a refill that was starting", cc.ClaudeCodeProvider._idle, {})
    started = len(cli.procs)
    await router.reply("SYS", "after close")
    await settle()
    check("after close, nothing is kept warm", (len(cli.procs) - started, cc.ClaudeCodeProvider._idle), (1, {}))

    cc.asyncio.create_subprocess_exec = real



asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("claude stream: warm reuse and refill, progress, errors, timeouts, old-CLI fallback, router")
