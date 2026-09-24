"""The Claude Code CLI as a provider.

`claude -p` draws on Landen's Claude Pro subscription rather than API credits,
which is the whole reason this is the default MAIN_PROVIDER. Auth lives in the
claude-auth volume and survives container rebuilds, so there is no key here.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage


# What the CLI needs to find itself, its login and the network. Nothing else:
# every credential Sloane holds (Telegram, Canvas, Gmail, the database) lives
# in this process's environment, and none of it is the CLI's business.
# ANTHROPIC_API_KEY is left out on purpose -- with it set the CLI bills the API
# instead of the Pro subscription, which is the one thing this lane exists for.
_CLI_ENV_KEEP = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
}


def _cli_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if k in _CLI_ENV_KEEP or k.startswith("CLAUDE_CODE_") or k == "CLAUDE_CONFIG_DIR"
    }


# Flags that make a call fast and make it hers. `--system-prompt` *replaces*
# Claude Code's own prompt: appended, she was a coding assistant with Sloane's
# persona bolted on ("ready to help with whatever coding task you've got").
# The rest skip session files, slash commands and user-level settings, which is
# about a second of start-up per call. An older CLI that doesn't know one of
# them gets the minimal set instead (LEGACY), once, and keeps it.
FAST_FLAGS = ("--no-session-persistence", "--disable-slash-commands", "--setting-sources", "project")


def build_argv(cli: str, system: str, *, model: str = "", legacy: bool = False,
               streaming: bool = False) -> list[str]:
    argv = [cli, "-p"]
    if streaming:
        argv += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--include-partial-messages"]
    else:
        argv += ["--output-format", "json"]
    argv += [
        "--system-prompt", system,
        # No tools and no MCP servers. The prompt carries ingested text --
        # email, calendar, Canvas -- and a model that can Read or Grep can
        # be talked into reading .env. It only ever needs to write text.
        "--tools", "",
        "--strict-mcp-config",
    ]
    if not legacy:
        argv += list(FAST_FLAGS)
    if model:
        argv += ["--model", model]
    return argv


def _unknown_option(stderr: str) -> bool:
    low = stderr.lower()
    return "unknown option" in low or "unrecognized option" in low or "unknown argument" in low


class ClaudeCodeProvider(Provider):
    name = "claude_code"
    # Set once a CLI has refused the fast flags; shared by every instance.
    legacy = False

    def __init__(self, settings: Settings) -> None:
        self._cli = settings.claude_cli
        self._timeout = settings.claude_cli_timeout
        self._model = settings.claude_cli_model.strip()

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> Completion:
        if shutil.which(self._cli) is None:
            raise ProviderError(self.name, f"{self._cli} is not on PATH")
        try:
            return await self._once(system, prompt, legacy=ClaudeCodeProvider.legacy)
        except _OldCli:
            ClaudeCodeProvider.legacy = True
            return await self._once(system, prompt, legacy=True)

    async def _once(self, system: str, prompt: str, *, legacy: bool) -> Completion:
        argv = build_argv(self._cli, system, model=self._model, legacy=legacy)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_cli_env(),
                cwd=tempfile.gettempdir(),
            )
        except OSError as exc:
            raise ProviderError(self.name, f"could not start {self._cli}: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ProviderError(self.name, f"timed out after {self._timeout}s") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
            if not legacy and _unknown_option(detail):
                raise _OldCli(detail)
            raise ProviderError(self.name, detail)

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            raise ProviderError(self.name, "empty output")

        text, model, prompt_tokens, completion_tokens = _unwrap(raw)
        if not text.strip():
            raise ProviderError(self.name, "no text in output")

        return Completion(
            text=text,
            usage=Usage(
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            ),
        )


class _OldCli(Exception):
    """The installed CLI predates one of FAST_FLAGS."""


def _unwrap(raw: str) -> tuple[str, str, int | None, int | None]:
    """Pull the result text out of the CLI's JSON envelope.

    The CLI has changed this envelope before, and it may emit bare text when
    --output-format is unsupported. Treat the whole payload as the answer rather
    than failing on a shape change.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw, "claude-code", None, None

    if isinstance(payload, list):
        payload = next(
            (p for p in reversed(payload) if isinstance(p, dict) and p.get("result")),
            {},
        )
    if not isinstance(payload, dict):
        return raw, "claude-code", None, None

    text = payload.get("result") or payload.get("text") or payload.get("content") or ""
    if not isinstance(text, str):
        text = json.dumps(text)

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return (
        text or raw,
        str(payload.get("model") or "claude-code"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
