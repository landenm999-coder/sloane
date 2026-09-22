"""Voice replies: speech in, an OGG/Opus voice note out -- or nothing.

This is the transport half of P3. The provider half (sloane/providers/tts.py)
turns text into WAV; this turns WAV into what Telegram plays as a voice note
and decides whether to try at all.

Its one promise is that it never costs a reply. Every failure -- over budget,
every speech provider down, ffmpeg missing -- returns None, and the caller sends
the text it was always going to send. A voice note is an upgrade on an answer,
not a condition of getting one.

It reads `speech` only. That field is capped at two sentences with no markdown,
lists or URLs precisely so that this file could be a transport swap and nothing
more.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from sloane.config import Settings
from sloane.memory.store import Store
from sloane.router import NoProviderAvailable, Router

log = logging.getLogger(__name__)


class VoiceUnavailable(RuntimeError):
    pass


async def to_ogg_opus(wav: bytes, *, ffmpeg: str = "ffmpeg", timeout: float = 20.0) -> bytes:
    """Transcode WAV to OGG/Opus, the only format Telegram shows as a voice note.

    Mono at 48 kHz is Opus's native rate; 32 kbit/s is plenty for speech and
    keeps a two-sentence reply to a few kilobytes.
    """
    if shutil.which(ffmpeg) is None:
        raise VoiceUnavailable(f"{ffmpeg} is not on PATH")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "wav", "-i", "pipe:0",
        "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k",
        "-application", "voip", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(wav), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise VoiceUnavailable("ffmpeg timed out") from exc
    if proc.returncode != 0 or not out:
        raise VoiceUnavailable(err.decode(errors="replace").strip()[:200] or "ffmpeg produced nothing")
    return out


class Voice:
    def __init__(self, router: Router, store: Store | None, config: Settings) -> None:
        self._router = router
        self._store = store
        self._config = config

    async def _within_budget(self) -> bool:
        if self._store is None:
            return True
        try:
            used = (await self._store.usage_today()).get("speak", 0)
        except Exception as exc:  # noqa: BLE001 - unreadable accounting never blocks
            log.warning("speech usage unreadable, allowing: %s", exc)
            return True
        if used >= self._config.daily_speak_budget:
            log.info("speech budget spent (%s/%s); replying in text",
                     used, self._config.daily_speak_budget)
            return False
        return True

    async def render(self, speech: str) -> bytes | None:
        """An OGG/Opus voice note for `speech`, or None. Never raises."""
        if not speech.strip():
            return None
        if not await self._within_budget():
            return None
        try:
            audio = await self._router.speak(speech)
        except NoProviderAvailable as exc:
            log.warning("no voice this time, replying in text: %s", exc)
            return None
        try:
            return await to_ogg_opus(audio.wav, ffmpeg=self._config.ffmpeg_bin)
        except VoiceUnavailable as exc:
            log.warning("could not encode the voice note, replying in text: %s", exc)
            return None
