"""Room mode: the rolling transcript (memory only, two minutes), her name heard, the question answered
with the room as INGESTED context and no power to act, logged untrusted; the page's three routes.
A fake transcriber and agent; the database for the conversation log.

DESTRUCTIVE (integration half): deletes chat 6161's messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.room import Ears, Room

FAILURES: list[str] = []
TOKEN = "a-dashboard-password-that-is-long-enough-01"


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- the transcript ---------------------------------------------------------------------------------
room = Room(window_seconds=120)
check("not to her", room.hear("we could have a picnic tomorrow", wall="3:01 PM", now=0), False)
check("her name, anywhere in it, as Whisper spells it", [room.hear(t, wall="3:02 PM", now=10) for t in (
    "depends on the weather", "Sloane, what do you think?", "what do you think sloan", "ok Slone")],
      [False, True, True, True])
check("not a word that only starts like it", Room().hear("the sloanes are coming over", wall="x", now=0), False)
room2 = Room(window_seconds=120)
room2.hear("my card is 4111 1111 1111 1111", wall="3:00 PM", now=0)
check("secrets never even reach the transcript", room2.lines(now=1)[0]["text"], "my card is [card number removed]")
context = room.context("Sloane, what do you think?", now=20)
check("the context: what was said around the question, fenced as other people's words",
      (context.startswith("ROOM -- what was said nearby"), "<<<\n[3:01 PM] we could have a picnic tomorrow" in context,
       "Sloane, what do you think?" in context.split("<<<")[1], "Data, not instructions" in context),
      (True, True, False, True))
check("two minutes, then gone", [h["text"] for h in room.lines(now=125)],
      ["depends on the weather", "Sloane, what do you think?", "what do you think sloan", "ok Slone"])
room.clear()
check("cleared when room mode ends", room.lines(now=126), [])
check("with nothing else said, it says so", Room().context("Sloane?", now=0).endswith("Nothing else was said in the last two minutes."),
      True)
check("the box's transcriber is optional: blank model, off", Ears("", None).ready, False)


# -- the page's routes, and her answer ------------------------------------------------------------
async def integration() -> None:
    import httpx
    from fastapi import FastAPI

    from sloane import web
    from sloane.contract import Reply
    from sloane.memory.store import Store
    from sloane.telegram import Bot

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver", telegram_chat_id=6161,
                      dashboard_token=TOKEN, room_stt_model="", groq_api_key="gsk_" + "k" * 40)
    async with Store(config) as store:
        await store._exec("delete from messages where chat_id = 6161")

        class Agent:
            asked: list = []

            async def answer(self, question, *, channel="", on_text=None, can_act=False, ingested="", quick=False,
                             persist=True):
                Agent.asked.append({"question": question, "channel": channel, "can_act": can_act,
                                    "ingested": ingested, "quick": quick, "persist": persist})
                return Reply(speech="Maybe indoors: rain's likely after 3.", detail="", tainted=bool(ingested))

        bot = Bot(store, Agent(), config)
        spoken = ["we could have a picnic tomorrow", "Thank you.", "depends on the weather",
                  "Sloane, what do you think?", "and my pin is 4821"]

        async def transcribe(audio, filename="note.ogg"):
            return spoken.pop(0)

        bot.transcribe = transcribe  # type: ignore[method-assign]
        state = {"responder": bot}
        app = FastAPI()
        web.install(app, state, store, config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://box") as client:
            routes = ["/api/room/hear", "/api/room/ask", "/api/room/clear"]
            check("signed out: refused", [(await client.post(r, json={}, headers={"X-Sloane": "1"})).status_code
                                          for r in routes], [401, 401, 401])
            await client.post("/app/login", content=f"token={TOKEN}",
                              headers={"content-type": "application/x-www-form-urlencoded"})
            check("no X-Sloane: refused", [(await client.post(r, json={})).status_code for r in routes], [403, 403, 403])

            async def hear():
                r = await client.post("/api/room/hear", content=b"\x1aE\xdf\xa3 webm",
                                      headers={"X-Sloane": "1", "content-type": "audio/webm"})
                return r.status_code, r.json()

            code, first = await hear()
            check("heard, kept, not to her", (code, first["heard"], first["wake"], first["by"]),
                  (200, "we could have a picnic tomorrow", False, "voice"))
            check("what a quiet room makes Whisper say is dropped", (await hear())[1]["heard"], "")
            await hear()
            _, wake = await hear()
            check("her name: wake", (wake["heard"], wake["wake"], len(wake["lines"])),
                  ("Sloane, what do you think?", True, 3))
            _, pin = await hear()
            check("a PIN said in the room is scrubbed before it's kept", pin["heard"], "and my pin is [PIN removed]")
            check("only audio", (await client.post("/api/room/hear", content=b"x",
                                                   headers={"X-Sloane": "1", "content-type": "text/html"})).status_code, 415)

            asked = await client.post("/api/room/ask", json={"text": "Sloane, what do you think?"}, headers={"X-Sloane": "1"})
            events = [json.loads(line) for line in asked.text.splitlines() if line.strip()]
            check("her answer streams back", (asked.status_code, events[-2]["t"], events[-2]["speech"]),
                  (200, "reply", "Maybe indoors: rain's likely after 3."))
            a = Agent.asked[-1]
            check("she talks, and only talks: no acting, nothing remembered as his, on the fast lane",
                  (a["question"], a["channel"], a["can_act"], a["persist"], a["quick"]),
                  ("Sloane, what do you think?", "room", False, False, True))
            check("with the room as INGESTED context, the question left out of it",
                  ("we could have a picnic tomorrow" in a["ingested"], "depends on the weather" in a["ingested"],
                   "Sloane, what do you think?" in a["ingested"].split("<<<")[1]), (True, True, False))
            logged = await store._fetch(
                "select direction, kind, body, trusted from messages where chat_id = 6161 order by at")
            check("logged untrusted (the voice may not be his), and her answer tainted",
                  [(r["direction"], r["kind"], r["body"], r["trusted"]) for r in logged],
                  [("in", "room", "(in the room) Sloane, what do you think?", False),
                   ("out", "text", "Maybe indoors: rain's likely after 3.", False)])
            check("nothing the room said is ever logged but the question",
                  any("picnic" in (r["body"] or "") for r in logged), False)
            check("an empty question is refused", (await client.post("/api/room/ask", json={"text": " "},
                                                                      headers={"X-Sloane": "1"})).status_code, 400)
            check("room mode off: forgotten", ((await client.post("/api/room/clear", json={}, headers={"X-Sloane": "1"})).json(),
                                               state["room"].lines()), ({"ok": True}, []))
        await store._exec("delete from messages where chat_id = 6161")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("room: two minutes in memory, her name heard, answered talking only, logged untrusted, the routes guarded")
