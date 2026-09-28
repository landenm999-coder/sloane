"""The shape every provider presents to the router."""

from __future__ import annotations

import abc
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """A provider failed to produce a completion.

    The router catches this and degrades to the next provider. Anything that is
    not a ProviderError propagates: a bug in our own code should be loud, not
    silently retried on a second vendor.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


@dataclass
class Usage:
    """What one call cost. Persisted to usage_log so /usage can answer honestly."""

    provider: str
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


@dataclass
class Completion:
    text: str
    usage: Usage


class Provider(abc.ABC):
    """A text-in, text-out model. Parsing into a Reply is the contract's job."""

    name: str = "base"

    @property
    def configured(self) -> bool:
        """False when it has nothing to call (no URL): the router passes over it
        without counting a failure. Providers that need a key still fail loudly."""
        return True

    @abc.abstractmethod
    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> Completion:
        """Return one completion, or raise ProviderError."""

    async def stream(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
        on_text=None,  # noqa: ANN001 - async (text so far) -> None
    ) -> Completion:
        """Like complete(), calling `on_text` with the text so far as it grows.

        Providers that can't stream just complete; the caller sees one final
        answer instead of a growing one, which is the same answer.
        """
        return await self.complete(system, prompt, max_tokens=max_tokens)

    async def research(self, query: str) -> str:
        """Search the web and summarise, with sources. Most providers can't."""
        raise ProviderError(self.name, "no web lookup on this provider")

    async def prewarm(self, system: str) -> None:
        """Get ready to answer with this system prompt. Most providers need nothing."""
        return None

    async def healthy(self) -> bool:
        """Cheap liveness check. Used by doctor.py and /health."""
        try:
            got = await self.complete("Reply with the word ok.", "ok", max_tokens=16)
        except ProviderError:
            return False
        return bool(got.text.strip())
