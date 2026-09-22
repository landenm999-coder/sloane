"""The paid destination of the upgrade lever.

Flipping MAIN_PROVIDER to `anthropic` puts replies on Sonnet 5 (~$5.40/mo at
Landen's volume) and BULK_PROVIDER on Haiku 4.5 (~$1.80/mo). Nothing else in the
codebase changes.

The SDK is imported lazily so that `anthropic` stays an optional dependency
while the free tier is doing the work.
"""

from __future__ import annotations

import time

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, settings: Settings, *, bulk: bool = False) -> None:
        self._key = settings.anthropic_api_key
        self._model = (
            settings.anthropic_bulk_model if bulk else settings.anthropic_main_model
        )
        # Sonnet 5 accepts output_config.effort; Haiku 4.5 rejects it. Unset by
        # default so a bulk call cannot 400 on a parameter it does not support.
        self._effort = settings.anthropic_effort.strip()

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> Completion:
        if not self._key:
            raise ProviderError(self.name, "ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ProviderError(
                self.name, "the anthropic package is not installed"
            ) from exc

        client = anthropic.AsyncAnthropic(api_key=self._key)
        request: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._effort:
            request["output_config"] = {"effort": self._effort}

        started = time.monotonic()
        try:
            message = await client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise ProviderError(self.name, "the API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(self.name, "rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(self.name, f"http {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(self.name, f"network error: {exc}") from exc
        finally:
            await client.close()

        latency_ms = int((time.monotonic() - started) * 1000)

        # A safety decline is an HTTP 200 with stop_reason "refusal", so it has
        # to be checked before reading content.
        if getattr(message, "stop_reason", None) == "refusal":
            raise ProviderError(self.name, "the model declined the request")

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        if not text.strip():
            raise ProviderError(self.name, "empty completion")

        usage = getattr(message, "usage", None)
        return Completion(
            text=text,
            usage=Usage(
                provider=self.name,
                model=str(getattr(message, "model", self._model)),
                prompt_tokens=getattr(usage, "input_tokens", None),
                completion_tokens=getattr(usage, "output_tokens", None),
                latency_ms=latency_ms,
            ),
        )
