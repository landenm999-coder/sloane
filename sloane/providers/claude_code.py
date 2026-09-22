"""The Claude Code CLI as a provider.

`claude -p` draws on Landen's Claude Pro subscription rather than API credits,
which is the whole reason this is the default MAIN_PROVIDER. Auth lives in the
claude-auth volume and survives container rebuilds, so there is no key here.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage


class ClaudeCodeProvider(Provider):
    name = "claude_code"

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

        argv = [
            self._cli,
            "-p",
            "--output-format",
            "json",
            "--append-system-prompt",
            system,
        ]
        if self._model:
            argv += ["--model", self._model]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
