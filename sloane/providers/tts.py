"""Text to speech, behind the same provider seam as the text models.

Two backends:

* **Groq** -- Orpheus over the OpenAI-compatible /audio/speech endpoint. Free
  tier, fast, and the default. It caps input at 200 characters per request, so
  speech is split at sentence boundaries (and, for one runaway sentence, at
  commas and then words) and the pieces are joined back into one clip. Sending
  the whole reply and hoping it fits would fail on exactly the longer answers.
* **Piper** -- a local voice on the box. No quota and no network, so it is the
  fallback when Groq's daily allowance is spent or the API is down -- or the
  main voice, for an accent Groq doesn't have (PIPER_VOICE=en_GB-cori-medium
  is British). A voice given by name is fetched once into the models volume.

Both return WAV. Turning that into the OGG/Opus Telegram wants for a voice note
is the transport's job (sloane/voice.py), not the provider's: a provider's
contract is "text in, audio out", the same shape whichever backend spoke.
"""

from __future__ import annotations

import abc
import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from sloane.config import Settings
from sloane.providers.base import ProviderError, Usage

log = logging.getLogger(__name__)

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

    async def warm(self) -> bool:
        """Get ready ahead of the first voice note. Nothing to do by default."""
        return True


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


# A Piper voice named rather than given as a path ("en_GB-cori-medium"):
# fetched once into the models volume, then kept loaded.
_VOICE_NAME = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-\w+-(?:x_low|low|medium|high)$")


def _library():  # noqa: ANN202 - piper's own class, or None
    """PiperVoice from the piper-tts package, if it is installed (it is in the image)."""
    try:
        from piper import PiperVoice
    except ImportError:
        return None
    return PiperVoice


def _downloader():  # noqa: ANN202
    try:
        from piper.download_voices import download_voice
    except ImportError:
        return None
    return download_voice


def _fetched(task: asyncio.Task) -> None:
    """A fetch nobody waited for still gets its failure logged, once."""
    if not task.cancelled() and task.exception() is not None:
        exc = task.exception()
        log.warning("piper voice fetch failed: %s", getattr(exc, "message", exc))


class PiperTTS(TTSProvider):
    """Local speech: no quota, no network once the voice is on disk.

    With the piper-tts package (in the image) the voice is loaded once and kept
    in memory, so a reply costs only the synthesis -- a fraction of a second for
    two sentences. Without it, the `piper` binary, which reloads the voice on
    every call.
    """

    name = "piper"
    # Loaded voices by file, shared by every instance: loading is the slow part.
    _voices: dict[str, object] = {}
    _loading = threading.Lock()
    _speaking = threading.Lock()
    _fetches: dict[str, asyncio.Task] = {}
    # A failed fetch is retried after this long, not on every voice note.
    RETRY_FETCH_SECONDS = 600
    _failed_at: dict[str, float] = {}

    def __init__(self, settings: Settings) -> None:
        self._bin = settings.piper_bin
        self._voice = settings.piper_voice.strip()
        self._models = Path(settings.embed_cache_dir or Path.home() / ".cache" / "sloane")
        self._timeout = 30
        self._fetch_timeout = 300

    @property
    def by_name(self) -> bool:
        """A voice named ("en_GB-cori-medium"), fetched when missing, not a path."""
        return bool(_VOICE_NAME.match(self._voice))

    @property
    def path(self) -> Path | None:
        """Where the voice's .onnx is (or will be, for a voice given by name)."""
        if not self._voice:
            return None
        if self.by_name:
            return self._models / f"{self._voice}.onnx"
        return Path(self._voice)

    async def warm(self) -> bool:
        """Fetch and load the voice now, so the first voice reply isn't the slow one."""
        try:
            path = await self._ready(wait=True)
            library = _library()
            if library is not None:
                await asyncio.to_thread(self._load, library, path)
        except ProviderError as exc:
            log.warning("piper voice not ready: %s", exc.message)
            return False
        return True

    async def _ready(self, *, wait: bool) -> Path:
        path = self.path
        if path is None:
            raise ProviderError(self.name, "PIPER_VOICE is not set")
        if path.is_file():
            return path
        if not self.by_name:
            raise ProviderError(self.name, "PIPER_VOICE does not point at an .onnx voice file")
        fetch = _downloader()
        if fetch is None:
            raise ProviderError(self.name, "fetching a voice by name needs the piper-tts package")
        failed = self._failed_at.get(self._voice)
        if failed is not None and time.monotonic() - failed < self.RETRY_FETCH_SECONDS:
            raise ProviderError(self.name, f"couldn't fetch {self._voice} recently; will retry")
        task = self._fetches.get(self._voice)
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.create_task(self._fetch_in_thread(fetch, path))
            task.add_done_callback(_fetched)
            self._fetches[self._voice] = task
        if not wait:
            # A reply never waits for a download: the other voice speaks this one.
            raise ProviderError(self.name, f"still fetching {self._voice}")
        await asyncio.shield(task)
        return path

    async def _fetch_in_thread(self, fetch, path: Path) -> None:  # noqa: ANN001
        try:
            await asyncio.wait_for(asyncio.to_thread(self._fetch, fetch, path), timeout=self._fetch_timeout)
        except asyncio.TimeoutError as exc:
            self._failed_at[self._voice] = time.monotonic()
            raise ProviderError(self.name, f"fetching {self._voice} timed out") from exc
        except Exception as exc:  # noqa: BLE001 - urllib, disk, a renamed voice
            self._failed_at[self._voice] = time.monotonic()
            raise ProviderError(self.name, f"could not fetch {self._voice}: {type(exc).__name__}: {exc}"[:200]) from exc
        self._failed_at.pop(self._voice, None)

    def _fetch(self, fetch, path: Path) -> None:  # noqa: ANN001
        """Download beside the target, then move into place: a cut-off download
        never looks like a voice. The model moves last, so its presence means
        its config is there too."""
        if path.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
            fetch(self._voice, Path(tmp))
            model = Path(tmp) / f"{self._voice}.onnx"
            config = Path(tmp) / f"{self._voice}.onnx.json"
            if not model.is_file() or not config.is_file():
                raise RuntimeError("the download was incomplete")
            os.replace(config, path.with_name(path.name + ".json"))
            os.replace(model, path)
        log.info("fetched piper voice %s", self._voice)

    def _load(self, library, path: Path):  # noqa: ANN001, ANN202
        key = str(path)
        with self._loading:
            voice = self._voices.get(key)
            if voice is None:
                voice = library.load(key)
                self._voices[key] = voice
        return voice

    def _speak(self, library, path: Path, text: str) -> bytes:  # noqa: ANN001
        voice = self._load(library, path)
        out = io.BytesIO()
        with self._speaking, wave.open(out, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return out.getvalue()

    async def synthesize(self, text: str) -> Audio:
        path = await self._ready(wait=False)
        started = time.monotonic()
        library = _library()
        if library is not None:
            try:
                wav = await asyncio.wait_for(asyncio.to_thread(self._speak, library, path, text),
                                             timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise ProviderError(self.name, "timed out") from exc
            except Exception as exc:  # noqa: BLE001 - onnxruntime, a corrupt voice file
                raise ProviderError(self.name, f"{type(exc).__name__}: {exc}"[:200]) from exc
        else:
            wav = await self._binary(path, text)
        try:
            with wave.open(io.BytesIO(wav)) as check:
                empty = check.getnframes() == 0
        except (wave.Error, EOFError) as exc:
            raise ProviderError(self.name, f"unreadable audio: {exc}") from exc
        if empty:
            raise ProviderError(self.name, "no audio")
        return Audio(
            wav=wav,
            usage=Usage(
                provider=self.name, model=path.stem,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    async def _binary(self, path: Path, text: str) -> bytes:
        if shutil.which(self._bin) is None:
            raise ProviderError(self.name, f"{self._bin} is not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._bin, "--model", str(path), "--output_file", str(out),
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
            return out.read_bytes()
