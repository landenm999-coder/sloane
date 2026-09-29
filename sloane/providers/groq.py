"""Groq, over its OpenAI-compatible endpoint.

Free tier, with limits it actually publishes: 1K requests and 200K tokens a day
for chat, 2K a day for Whisper. That is why bulk work lands here and not on
Gemini. Groq has no embedding API, which is why embeddings run locally instead.
"""

from __future__ import annotations

import time

import httpx

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage


class GroqProvider(Provider):
    name = "groq"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # `transport` is for tests: a stub that sees the request instead of Groq.
        self._transport = transport
        self._key = settings.groq_api_key
        self._base = settings.groq_base_url.rstrip("/")
        self._model = settings.groq_model
        self._stt_model = settings.groq_stt_model

    async def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 1024,
    ) -> Completion:
        if not self._key:
            raise ProviderError(self.name, "GROQ_API_KEY is not set")

        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        # gpt-oss reasons before it answers, and the reasoning counts against max_tokens:
        # at its default effort a short budget is spent thinking and the answer comes back
        # empty. Low effort leaves room for the words.
        if "gpt-oss" in self._model:
            body["reasoning_effort"] = "low"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 429:
            raise ProviderError(self.name, "rate limited (daily free-tier cap)")
        if response.status_code >= 400:
            raise ProviderError(
                self.name, f"http {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, f"unexpected response shape: {exc}") from exc

        if not text.strip():
            raise ProviderError(self.name, "empty completion")

        usage = payload.get("usage") or {}
        return Completion(
            text=text,
            usage=Usage(
                provider=self.name,
                model=str(payload.get("model") or self._model),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                latency_ms=latency_ms,
            ),
        )

    async def transcribe(self, audio: bytes, filename: str = "note.ogg") -> str:
        """Whisper via the same OpenAI-compatible surface. 2K requests a day free."""
        if not self._key:
            raise ProviderError(self.name, "GROQ_API_KEY is not set")
        try:
            async with httpx.AsyncClient(timeout=120.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    files={"file": (filename, audio, "application/octet-stream")},
                    data={"model": self._stt_model, "response_format": "json"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name, f"stt http {response.status_code}: {response.text[:200]}"
            )
        try:
            return (response.json().get("text") or "").strip()
        except ValueError as exc:
            raise ProviderError(self.name, f"stt returned non-JSON: {exc}") from exc
