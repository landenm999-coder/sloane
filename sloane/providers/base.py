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

    @abc.abstractmethod
    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> Completion:
        """Return one completion, or raise ProviderError."""

    async def healthy(self) -> bool:
        """Cheap liveness check. Used by doctor.py and /health."""
        try:
            got = await self.complete("Reply with the word ok.", "ok", max_tokens=16)
        except ProviderError:
            return False
        return bool(got.text.strip())
