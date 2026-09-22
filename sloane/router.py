"""THE UPGRADE LEVER.

Every model call in Sloane goes through here. No module outside sloane.providers
imports a vendor SDK, which is what makes free-to-paid two lines of .env:

    MAIN_PROVIDER=claude_code  ->  anthropic     # Sonnet 5
    BULK_PROVIDER=groq         ->  anthropic     # Haiku 4.5

Two jobs beyond dispatch:

* Degradation. If the configured provider fails, the router walks the remaining
  ones in order rather than returning nothing. A reply that arrives on the wrong
  model beats a morning brief that never arrives.
* Accounting. Every attempt -- including the failures -- is handed to a usage
  sink so /usage can answer honestly about which model actually did the work.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from collections.abc import Awaitable, Callable, Iterable

from sloane.config import Settings, settings as default_settings
from sloane.providers.anthropic_api import AnthropicProvider
from sloane.providers.base import Completion, Provider, ProviderError, Usage
from sloane.providers.claude_code import ClaudeCodeProvider
from sloane.providers.groq import GroqProvider
from sloane.providers.tts import Audio, GroqTTS, PiperTTS, TTSProvider

log = logging.getLogger(__name__)

# The order each lane falls back through. The configured provider is tried
# first, then the rest of its lane in this order.
# Set by the scheduler around a job. Main-lane calls made inside one are
# accounted as purpose "job", so the scheduled-work budget counts scheduled
# work -- not Landen's own questions, which are never rationed.
SCHEDULED: ContextVar[bool] = ContextVar("sloane_scheduled", default=False)

MAIN_ORDER = ("claude_code", "anthropic", "groq")
BULK_ORDER = ("groq", "anthropic", "claude_code")
# Voice: Groq Orpheus is fast and free up to its daily cap; Piper is local and
# has no cap. Either way, a failure here costs the voice, never the reply.
SPEAK_ORDER = ("groq", "piper")

UsageSink = Callable[[Usage, str, bool, str | None, str | None], Awaitable[None]]


class NoProviderAvailable(RuntimeError):
    """Every provider in the lane failed. The caller must say so, not invent a reply."""


def build(name: str, config: Settings, *, bulk: bool = False) -> Provider:
    """Construct one provider by name. The only place these classes are named."""
    if name == "claude_code":
        return ClaudeCodeProvider(config)
    if name == "groq":
        return GroqProvider(config)
    if name == "anthropic":
        return AnthropicProvider(config, bulk=bulk)
    raise ValueError(f"unknown provider {name!r}")


def build_tts(name: str, config: Settings) -> TTSProvider:
    if name == "groq":
        return GroqTTS(config)
    if name == "piper":
        return PiperTTS(config)
    raise ValueError(f"unknown speech provider {name!r}")


def _lane(preferred: str, order: Iterable[str]) -> list[str]:
    """The preferred provider first, then the rest of the lane, no duplicates."""
    rest = [n for n in order if n != preferred]
    return [preferred, *rest]


class Router:
    def __init__(
        self,
        config: Settings | None = None,
        *,
        usage_sink: UsageSink | None = None,
        factory: Callable[[str, Settings, bool], Provider] | None = None,
        tts_factory: Callable[[str, Settings], TTSProvider] | None = None,
    ) -> None:
        self._config = config or default_settings()
        self._sink = usage_sink
        # Injectable so tests can make a named provider fail on demand without
        # touching the network.
        self._factory = factory or (lambda n, c, b: build(n, c, bulk=b))
        self._cache: dict[tuple[str, bool], Provider] = {}
        self._tts_factory = tts_factory or build_tts
        self._tts_cache: dict[str, TTSProvider] = {}

    # -- lanes ----------------------------------------------------------------

    async def reply(self, system: str, prompt: str, *, max_tokens: int = 0) -> str:
        """The main lane: one turn, the answer Landen reads."""
        return await self._run(
            _lane(self._config.main_provider, MAIN_ORDER),
            system,
            prompt,
            max_tokens or self._config.max_reply_tokens,
            bulk=False,
            purpose="job" if SCHEDULED.get() else "reply",
        )

    async def bulk(self, system: str, prompt: str, *, max_tokens: int = 2048) -> str:
        """The bulk lane: batched triage and summarising, where volume beats polish."""
        return await self._run(
            _lane(self._config.bulk_provider, BULK_ORDER),
            system,
            prompt,
            max_tokens,
            bulk=True,
            purpose="bulk",
        )

    async def speak(self, text: str) -> Audio:
        """Text to WAV, degrading across speech providers like the text lanes."""
        configured = self._config.speak_provider
        failures: list[str] = []
        for name in _lane(configured, SPEAK_ORDER):
            degraded_from = None if name == configured else configured
            try:
                if name not in self._tts_cache:
                    self._tts_cache[name] = self._tts_factory(name, self._config)
                provider = self._tts_cache[name]
            except ValueError as exc:
                log.error("speech provider %s is not usable: %s", name, exc)
                failures.append(f"{name}: {exc}")
                continue
            try:
                audio = await provider.synthesize(text)
            except ProviderError as exc:
                log.warning("speech provider %s failed: %s", name, exc.message)
                failures.append(exc.message)
                await self._record(Usage(provider=name), "speak", ok=False,
                                   error=exc.message, degraded_from=degraded_from)
                continue
            await self._record(audio.usage, "speak", ok=True, error=None,
                               degraded_from=degraded_from)
            return audio
        raise NoProviderAvailable("; ".join(failures) or "no speech providers configured")

    # -- internals ------------------------------------------------------------

    def _provider(self, name: str, bulk: bool) -> Provider:
        key = (name, bulk)
        if key not in self._cache:
            self._cache[key] = self._factory(name, self._config, bulk)
        return self._cache[key]

    async def _run(
        self,
        lane: list[str],
        system: str,
        prompt: str,
        max_tokens: int,
        *,
        bulk: bool,
        purpose: str,
    ) -> str:
        configured = lane[0]
        failures: list[str] = []

        for name in lane:
            degraded_from = None if name == configured else configured
            try:
                provider = self._provider(name, bulk)
            except ValueError as exc:
                # A typo in .env should be visible, not silently skipped.
                log.error("provider %s is not usable: %s", name, exc)
                failures.append(f"{name}: {exc}")
                continue

            try:
                completion: Completion = await provider.complete(
                    system, prompt, max_tokens=max_tokens
                )
            except ProviderError as exc:
                log.warning("provider %s failed: %s", name, exc.message)
                failures.append(exc.message)
                await self._record(
                    Usage(provider=name), purpose, ok=False,
                    error=exc.message, degraded_from=degraded_from,
                )
                continue

            if degraded_from is not None:
                log.warning("degraded from %s to %s", degraded_from, name)
            await self._record(
                completion.usage, purpose, ok=True,
                error=None, degraded_from=degraded_from,
            )
            return completion.text

        raise NoProviderAvailable("; ".join(failures) or "no providers configured")

    async def _record(
        self,
        usage: Usage,
        purpose: str,
        *,
        ok: bool,
        error: str | None,
        degraded_from: str | None,
    ) -> None:
        """Accounting must never be the reason a reply fails to arrive."""
        if self._sink is None:
            return
        try:
            await self._sink(usage, purpose, ok, error, degraded_from)
        except Exception:  # noqa: BLE001 - logging a cost is not worth an outage
            log.exception("could not record usage for %s", usage.provider)
