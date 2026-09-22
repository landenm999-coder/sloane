"""Text to speech, behind the same provider seam as the text models.

Two backends:

* **Groq** -- Orpheus over the OpenAI-compatible /audio/speech endpoint. Free
  tier, fast, and the default. It caps input at 200 characters per request, so
  speech is split at sentence boundaries (and, for one runaway sentence, at
  commas and then words) and the pieces are joined back into one clip. Sending
  the whole reply and hoping it fits would fail on exactly the longer answers.
* **Piper** -- a local binary and an .onnx voice on the box. No quota and no
  network, so it is the fallback when Groq's daily allowance is spent or the
  API is down. The voice file is downloaded once at setup.

Both return WAV. Turning that into the OGG/Opus Telegram wants for a voice note
is the transport's job (sloane/voice.py), not the provider's: a provider's
contract is "text in, audio out", the same shape whichever backend spoke.
"""

from __future__ import annotations

import abc
import asyncio
import io
import re
import shutil
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from sloane.config import Settings
from sloane.providers.base import ProviderError, Usage

# Groq Orpheus rejects longer input.
GROQ_TTS_MAX_CHARS = 200

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


@dataclass
class Audio:
    wav: bytes
    usage: Usage


def chunk(text: str, limit: int = GROQ_TTS_MAX_CHARS) -> list[str]:
    """Split text into pieces no longer than `limit`, breaking where speech would.

    Sentences first, then clauses, then words. A piece is never cut mid-word
    unless a single word is longer than the limit, which does not happen in
    speech that passed through the contract.
    """
    text = " ".join((text or "").split())
    if not text:
        return []

    def pieces(fragment: str) -> list[str]:
        if len(fragment) <= limit:
            return [fragment]
        for splitter in (_SENTENCE_END, _CLAUSE_END):
            parts = [p for p in splitter.split(fragment) if p]
            if len(parts) > 1:
                return [x for p in parts for x in pieces(p)]
        words, out, current = fragment.split(" "), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    out.append(current)
                current = word[:limit]
        if current:
            out.append(current)
        return out

    # Re-pack greedily so two short sentences share one request.
    packed: list[str] = []
    for piece in pieces(text):
        if packed and len(packed[-1]) + 1 + len(piece) <= limit:
            packed[-1] = f"{packed[-1]} {piece}"
        else:
            packed.append(piece)
    return packed


def join_wav(clips: list[bytes]) -> bytes:
    """Concatenate WAV clips that share a format into one."""
    if len(clips) == 1:
        return clips[0]
    frames, params = [], None
    for clip in clips:
        with wave.open(io.BytesIO(clip)) as w:
            shape = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = w.getparams()
                first = shape
            elif shape != first:
                raise ProviderError("tts", "clips came back in different audio formats")
            frames.append(w.readframes(w.getnframes()))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for f in frames:
            w.writeframes(f)
    return out.getvalue()


class TTSProvider(abc.ABC):
    name: str = "tts"

    @abc.abstractmethod
    async def synthesize(self, text: str) -> Audio:
        """Return WAV audio for `text`, or raise ProviderError."""


class GroqTTS(TTSProvider):
    name = "groq"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.groq_api_key
        self._base = settings.groq_base_url.rstrip("/")
        self._model = settings.groq_tts_model
        self._voice = settings.groq_tts_voice

    async def synthesize(self, text: str) -> Audio:
        if not self._key:
            raise ProviderError(self.name, "GROQ_API_KEY is not set")
        parts = chunk(text)
        if not parts:
            raise ProviderError(self.name, "nothing to say")

        started = time.monotonic()
        clips: list[bytes] = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for part in parts:
                    response = await client.post(
                        f"{self._base}/audio/speech",
                        headers={"Authorization": f"Bearer {self._key}"},
                        json={
                            "model": self._model,
                            "voice": self._voice,
                            "input": part,
                            "response_format": "wav",
                        },
                    )
                    if response.status_code == 429:
                        raise ProviderError(self.name, "rate limited (daily TTS cap)")
                    if response.status_code >= 400:
                        raise ProviderError(
                            self.name, f"tts http {response.status_code}: {response.text[:200]}"
                        )
                    clips.append(response.content)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {type(exc).__name__}") from exc

        try:
            wav = join_wav(clips)
        except (wave.Error, EOFError) as exc:
            raise ProviderError(self.name, f"tts returned unreadable audio: {exc}") from exc
        return Audio(
            wav=wav,
            usage=Usage(
                provider=self.name, model=self._model,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, settings: Settings) -> None:
        self._bin = settings.piper_bin
        self._voice = settings.piper_voice
        self._timeout = 30

    async def synthesize(self, text: str) -> Audio:
        if not self._voice or not Path(self._voice).is_file():
            raise ProviderError(self.name, "PIPER_VOICE does not point at an .onnx voice file")
        if shutil.which(self._bin) is None:
            raise ProviderError(self.name, f"{self._bin} is not on PATH")

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._bin, "--model", self._voice, "--output_file", str(out),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise ProviderError(self.name, f"could not start piper: {exc}") from exc
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(text.encode()), timeout=self._timeout
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ProviderError(self.name, "timed out") from exc
            if proc.returncode != 0 or not out.exists():
                raise ProviderError(
                    self.name, stderr.decode(errors="replace").strip()[:200] or "no audio"
                )
            wav = out.read_bytes()

        return Audio(
            wav=wav,
            usage=Usage(
                provider=self.name, model=Path(self._voice).stem,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
