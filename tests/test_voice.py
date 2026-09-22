"""P3: voice replies. Real ffmpeg, a stub Groq, no model, no Telegram.

The property that matters most is the floor: whatever breaks in the voice path
-- budget, every TTS provider, ffmpeg, Telegram's sendVoice -- the reply still
goes out as text.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import shutil
import struct
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.contract import Reply
from sloane.providers.base import ProviderError, Usage
from sloane.providers.tts import GROQ_TTS_MAX_CHARS, Audio, GroqTTS, TTSProvider, chunk, join_wav
from sloane.router import NoProviderAvailable, Router
from sloane.voice import Voice, to_ogg_opus

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def tone(seconds: float = 0.3, rate: int = 24000) -> bytes:
    """A short sine wave, as the WAV a TTS backend would return."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return out.getvalue()


def frames_in(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes()


# --- chunking: Groq rejects more than 200 characters per request -------------
long_speech = (
    "Two things are due today, the Stat quiz corrections at four which clashes "
    "with your shift, and the Physics lab writeup by eleven fifty-nine tonight. "
    "You also have the DECA officer call at five, which lands in the middle of work."
)
parts = chunk(long_speech)
check("a long reply is split", len(parts) > 1, True)
check("every piece fits the limit", all(len(p) <= GROQ_TTS_MAX_CHARS for p in parts), True)
check("nothing is lost or reordered", " ".join(parts), " ".join(long_speech.split()))
check("short sentences share a request", chunk("Yes. You work at three."), ["Yes. You work at three."])
check("empty speech is nothing to say", chunk("   "), [])
runon = "word " * 120
check(
    "a single runaway sentence still splits, at words",
    all(len(p) <= GROQ_TTS_MAX_CHARS for p in chunk(runon)), True,
)
check("and no word is cut", all(w == "word" for p in chunk(runon) for w in p.split()), True)

# --- joining clips ------------------------------------------------------------
joined = join_wav([tone(0.2), tone(0.3)])
check("clips concatenate frame for frame", frames_in(joined), frames_in(tone(0.2)) + frames_in(tone(0.3)))
try:
    join_wav([tone(0.1, rate=24000), tone(0.1, rate=16000)])
    FAILURES.append("clips in different formats were silently joined")
except ProviderError:
    pass

# --- real transcode to the format Telegram needs -----------------------------
if shutil.which("ffmpeg"):
    ogg = asyncio.run(to_ogg_opus(tone(0.5)))
    check("ffmpeg produces an Ogg container", ogg[:4], b"OggS")
    check("carrying Opus", b"OpusHead" in ogg, True)
    check("and it is small", len(ogg) < 20_000, True)
else:
    print("  (ffmpeg absent: transcode not exercised)")


# --- Groq over a local stub that enforces its own limit ----------------------
REQUESTS: list[dict] = []


class GroqStub(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)
        if len(body.get("input", "")) > GROQ_TTS_MAX_CHARS:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"input too long"}')
            return
        if body.get("input") == "RATE LIMIT":
            self.send_response(429)
            self.end_headers()
            return
        clip = tone(0.1)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(clip)))
        self.end_headers()
        self.wfile.write(clip)


server = HTTPServer(("127.0.0.1", 0), GroqStub)
threading.Thread(target=server.serve_forever, daemon=True).start()
groq_cfg = isolated(groq_api_key="test", groq_base_url=f"http://127.0.0.1:{server.server_port}")

audio = asyncio.run(GroqTTS(groq_cfg).synthesize(long_speech))
check("Groq is called once per chunk", len(REQUESTS), len(parts))
check("with the configured model", REQUESTS[0]["model"], "canopylabs/orpheus-v1-english")
check("and voice", REQUESTS[0]["voice"], "hannah")
check("asking for WAV", REQUESTS[0]["response_format"], "wav")
check("and the clips come back as one", frames_in(audio.wav), frames_in(tone(0.1)) * len(parts))
try:
    asyncio.run(GroqTTS(groq_cfg).synthesize("RATE LIMIT"))
    FAILURES.append("a 429 should raise")
except ProviderError as exc:
    check("a 429 is reported as the daily cap", "rate limited" in exc.message, True)
try:
    asyncio.run(GroqTTS(isolated()).synthesize("hi"))
    FAILURES.append("no key should raise")
except ProviderError:
    pass
server.shutdown()


# --- the speak lane degrades and accounts ------------------------------------
class FakeTTS(TTSProvider):
    def __init__(self, name: str, fails: bool) -> None:
        self.name, self._fails = name, fails

    async def synthesize(self, text: str) -> Audio:
        if self._fails:
            raise ProviderError(self.name, "down")
        return Audio(wav=tone(0.1), usage=Usage(provider=self.name))


def speak_router(broken: set[str], rows: list):
    async def sink(usage, purpose, ok, error, degraded_from):
        rows.append((usage.provider, purpose, ok, degraded_from))

    return Router(
        isolated(speak_provider="groq"), usage_sink=sink,
        tts_factory=lambda n, c: FakeTTS(n, n in broken),
    )


rows: list = []
asyncio.run(speak_router({"groq"}, rows).speak("hi"))
check("groq down degrades to piper", rows[-1], ("piper", "speak", True, "groq"))
check("and the failure is accounted as speech", rows[0], ("groq", "speak", False, None))
try:
    asyncio.run(speak_router({"groq", "piper"}, []).speak("hi"))
    FAILURES.append("every speech provider down should raise")
except NoProviderAvailable:
    pass


# --- Voice never raises, it returns None -------------------------------------
class Usage_:
    def __init__(self, spent: int) -> None:
        self.spent = spent

    async def usage_today(self):
        return {"speak": self.spent}


cfg = isolated(daily_speak_budget=5)
ok_router = speak_router(set(), [])
dead_router = speak_router({"groq", "piper"}, [])
check("over budget: no voice", asyncio.run(Voice(ok_router, Usage_(5), cfg).render("hi")), None)
check("every provider down: no voice", asyncio.run(Voice(dead_router, Usage_(0), cfg).render("hi")), None)
check(
    "no ffmpeg: no voice",
    asyncio.run(Voice(ok_router, Usage_(0), isolated(ffmpeg_bin="no-such-ffmpeg")).render("hi")),
    None,
)
check("nothing to say: no voice", asyncio.run(Voice(ok_router, Usage_(0), cfg).render("  ")), None)
if shutil.which("ffmpeg"):
    note = asyncio.run(Voice(ok_router, Usage_(0), cfg).render("You work at three."))
    check("the happy path is a voice note", note[:4] if note else None, b"OggS")


# --- the bot: voice when he used voice, text always as the floor -------------
from sloane.telegram import Bot


class StubVoice:
    def __init__(self, result) -> None:
        self.result = result

    async def render(self, speech):
        return self.result


def bot_with(voice, voice_send_fails=False):
    bot = Bot(None, None, isolated(telegram_bot_token="x"), voice=voice)
    sent = {"voice": [], "text": []}

    async def send_voice(chat_id, ogg):
        if voice_send_fails:
            raise RuntimeError("telegram sendVoice -> 400")
        sent["voice"].append(ogg)

    async def send(chat_id, reply):
        sent["text"].append(reply)

    bot.send_voice, bot.send = send_voice, send
    return bot, sent


answer = Reply(speech="You work at three.", detail="| shift | 3-7 PM |")

bot, sent = bot_with(StubVoice(b"OggS..."))
asyncio.run(bot.reply(1, answer, as_voice=True))
check("a voice note in gets a voice note out", sent["voice"], [b"OggS..."])
check("then the detail as text", [r.detail for r in sent["text"]], ["| shift | 3-7 PM |"])

bot, sent = bot_with(StubVoice(b"OggS..."))
asyncio.run(bot.reply(1, Reply(speech="Yes.", detail="Yes."), as_voice=True))
check("detail that only repeats the speech is not sent twice", sent["text"], [])

bot, sent = bot_with(StubVoice(b"OggS..."))
asyncio.run(bot.reply(1, answer, as_voice=False))
check("a typed message gets text, not voice", (sent["voice"], len(sent["text"])), ([], 1))

bot, sent = bot_with(StubVoice(None))
asyncio.run(bot.reply(1, answer, as_voice=True))
check("no voice available: the whole reply goes as text", sent["text"], [answer])

bot, sent = bot_with(StubVoice(b"OggS..."), voice_send_fails=True)
asyncio.run(bot.reply(1, answer, as_voice=True))
check("sendVoice failing: the whole reply goes as text", sent["text"], [answer])

bot, sent = bot_with(None)
asyncio.run(bot.reply(1, answer, as_voice=True))
check("no voice configured at all: text", sent["text"], [answer])

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("voice: chunking, real transcode, degradation, and text as the floor all pass")
