"""A reply shown while it's written: typing at once, one message edited as it grows.

DESTRUCTIVE: deletes messages for chat 5151.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane import telegram as tg
from sloane.contract import Reply, parse

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


check("speech alone", tg._shown("Two things.", ""), "Two things.")
check("a detail that repeats the speech stays hidden while it streams", tg._shown("Two things are due.", "Two thi"),
      "Two things are due.")
check("a detail that adds something is shown", tg._shown("Two things.", "- lab"), "Two things.\n\n- lab")

RAW = '{"speech": "Two things are due tomorrow, the lab and the essay.", "detail": "- Lab 4 writeup, 8 AM\\n- Essay, 11:59 PM"}'


class StreamingAgent:
    """Writes its reply a few characters at a time through on_text."""

    def __init__(self, raw: str = RAW, stream: bool = True) -> None:
        self.raw = raw
        self.stream = stream
        self.saw_sink = None

    async def answer(self, body, *, channel="telegram", on_text=None, **_):
        self.saw_sink = on_text is not None
        if on_text is not None and self.stream:
            for i in range(6, len(self.raw) + 1, 6):
                await on_text(self.raw[:i])
                await asyncio.sleep(0)
            await on_text(self.raw)
        return parse(self.raw)


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      telegram_chat_id=5151, telegram_bot_token="x")
    async with Store(config) as store:
        await store._exec("delete from messages where chat_id = 5151")
        calls: list[tuple[str, dict]] = []

        async def fake_call(client, method, **payload):
            calls.append((method, payload))
            return {"message_id": 77} if method == "sendMessage" else {}

        tg.STREAM_EDIT_EVERY = 0  # every update edits, so the test sees them all
        agent = StreamingAgent()
        bot = tg.Bot(store, agent, config)
        bot._call = fake_call
        n = 770000

        async def message(text, **extra):
            nonlocal n
            n += 1
            await bot._handle({"update_id": n, "message": {"chat": {"id": 5151}, "text": text, **extra}})

        await message("what's due tomorrow?")
        methods = [m for m, _ in calls]
        check("typing first", methods[0], "sendChatAction")
        check("one message, then edits", (methods.count("sendMessage"), methods.count("editMessageText") > 3), (1, True))
        first = next(p for m, p in calls if m == "sendMessage")
        check("the first shown text is a phrase with a cursor",
              (len(first["text"]) >= tg.STREAM_MIN_CHARS, first["text"].endswith(tg.CURSOR)), (True, True))
        final = [p for m, p in calls if m == "editMessageText"][-1]
        check("the last edit is the finished reply, no cursor",
              final["text"], "Two things are due tomorrow, the lab and the essay.\n\n- Lab 4 writeup, 8 AM\n- Essay, 11:59 PM")
        check("every edit is the same message", {p["message_id"] for m, p in calls if m == "editMessageText"}, {77})
        logged = await store._fetch("select body from messages where chat_id = 5151 and direction = 'out'")
        check("logged once, as finished", [r["body"] for r in logged], [final["text"]])

        # Nothing streamed (a provider that can't): the reply is sent the plain way.
        calls.clear()
        bot._agent = StreamingAgent(stream=False)
        await message("again?")
        check("no stream: one plain send", [m for m, _ in calls if m != "sendChatAction"], ["sendMessage"])
        check("with no cursor", calls[-1][1]["text"].endswith(tg.CURSOR), False)

        # A voice note is not streamed: it's read aloud whole.
        calls.clear()
        voiced = StreamingAgent()
        bot._agent = voiced

        class STT:
            async def transcribe(self, audio):
                return "what's due tomorrow"

        async def download(client, file_id):
            return b"audio"

        sent: list[Reply] = []

        async def reply(chat_id, r, *, as_voice):
            sent.append((r.speech, as_voice))

        bot._stt, bot._download_voice, bot.reply = STT(), download, reply
        await message("", voice={"file_id": "f"})
        check("voice gets no live text", (voiced.saw_sink, [m for m, _ in calls if m in ("sendMessage", "editMessageText")]),
              (False, []))
        check("and is answered by voice", sent[-1][1], True)
        await store._exec("delete from messages where chat_id = 5151")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("live: typing at once, one message edited as it's written, finished in place, voice untouched")
