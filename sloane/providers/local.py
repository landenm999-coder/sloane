"""A model on hardware he owns: Ollama, llama.cpp's server, LM Studio, vLLM.

Every one of them speaks the OpenAI chat-completions API, so this is one small
client for all of them: LOCAL_BASE_URL is the server's /v1 (Ollama:
http://<host>:11434/v1), LOCAL_MODEL the model it serves ("llama3.2:3b").
Nothing leaves the house, nothing is rationed, and it keeps working when every
cloud lane is down -- at the speed of the box it runs on. A Raspberry Pi 5
manages a 3-4B model at a few words a second, which is fine for the nightly
bulk work and slow for chat; LOCAL_MODELS.md has the trade-offs.

It streams, because a slow model that shows its first words at once feels
far faster than one that answers whole after twenty seconds. Unset, it is not
configured, and the router passes over it without counting a failure.

With LOCAL_STT_MODEL set it also transcribes voice notes, for servers that
offer the OpenAI audio endpoint (speaches / faster-whisper-server, LocalAI).
"""

from __future__ import annotations

import json
import time

import httpx

from sloane.config import Settings
from sloane.providers.base import Completion, Provider, ProviderError, Usage


class LocalProvider(Provider):
    name = "local"

    def __init__(self, settings: Settings) -> None:
        self._base = settings.local_base_url.rstrip("/")
        self._model = settings.local_model
        self._key = settings.local_api_key
        self._timeout = settings.local_timeout
        self._stt_model = settings.local_stt_model
        self._stt_base = (settings.local_stt_base_url or settings.local_base_url).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._base and self._model)

    def _headers(self) -> dict[str, str]:
        # Most local servers ignore it; LM Studio and vLLM can be set to check it.
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def _body(self, system: str, prompt: str, max_tokens: int, stream: bool) -> dict:
        return {
            "model": self._model,
            "max_tokens": max_tokens,
            "stream": stream,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }

    def _usage(self, payload: dict, started: float) -> Usage:
        usage = payload.get("usage") or {}
        return Usage(
            provider=self.name,
            model=str(payload.get("model") or self._model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _check(self) -> None:
        if not self.configured:
            raise ProviderError(self.name, "LOCAL_BASE_URL and LOCAL_MODEL are not set")

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> Completion:
        self._check()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base}/chat/completions", headers=self._headers(),
                                             json=self._body(system, prompt, max_tokens, stream=False))
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"can't reach the local model: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(self.name, f"http {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, f"unexpected response shape: {exc}") from exc
        if not text.strip():
            raise ProviderError(self.name, "empty completion")
        return Completion(text=text, usage=self._usage(payload, started))

    async def stream(self, system: str, prompt: str, *, max_tokens: int = 1024,
                     on_text=None) -> Completion:  # noqa: ANN001
        """Server-sent events, one delta at a time; `on_text` sees the text so far."""
        if on_text is None:
            return await self.complete(system, prompt, max_tokens=max_tokens)
        self._check()
        started = time.monotonic()
        parts: list[str] = []
        last: dict = {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", f"{self._base}/chat/completions", headers=self._headers(),
                                         json=self._body(system, prompt, max_tokens, stream=True)) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        raise ProviderError(self.name, f"http {response.status_code}: {body[:200]}")
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except ValueError:
                            continue
                        last = event
                        choices = event.get("choices") or [{}]
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                        if delta:
                            parts.append(delta)
                            try:
                                await on_text("".join(parts))
                            except Exception:  # noqa: BLE001 - a display hiccup never costs the answer
                                pass
        except httpx.HTTPError as exc:
            if parts:  # cut off mid-answer: what arrived is still an answer
                return Completion(text="".join(parts), usage=self._usage(last, started))
            raise ProviderError(self.name, f"can't reach the local model: {exc}") from exc
        text = "".join(parts)
        if not text.strip():
            raise ProviderError(self.name, "empty completion")
        return Completion(text=text, usage=self._usage(last, started))

    async def transcribe(self, audio: bytes, filename: str = "note.ogg") -> str:
        """Speech to text on a local Whisper server, if LOCAL_STT_MODEL is set."""
        if not (self._stt_base and self._stt_model):
            raise ProviderError(self.name, "LOCAL_STT_MODEL is not set")
        try:
            async with httpx.AsyncClient(timeout=max(self._timeout, 120)) as client:
                response = await client.post(
                    f"{self._stt_base}/audio/transcriptions", headers=self._headers(),
                    files={"file": (filename, audio, "application/octet-stream")},
                    data={"model": self._stt_model, "response_format": "json"},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"can't reach the local speech model: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(self.name, f"stt http {response.status_code}: {response.text[:200]}")
        try:
            return (response.json().get("text") or "").strip()
        except ValueError as exc:
            raise ProviderError(self.name, f"stt returned non-JSON: {exc}") from exc

    async def models(self) -> list[str]:
        """What the server says it serves (doctor checks LOCAL_MODEL is one)."""
        self._check()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{self._base}/models", headers=self._headers())
            response.raise_for_status()
            return [str(m.get("id")) for m in response.json().get("data") or [] if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ProviderError(self.name, f"can't list the local models: {exc}") from exc
