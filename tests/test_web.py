"""The control room (/app): who gets in, what every route refuses, and that the chat is Telegram's own.

The unit half needs nothing. The integration half (DATABASE_URL) mounts the
routes on a bare app against a real Store, with a real Bot answering the chat
through a fake agent, and drives it over ASGI.

DESTRUCTIVE: clears reminders, follow-ups, proposals and learned state, and
messages for chat 5151.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane import web

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
TOKEN = "a-dashboard-password-that-is-long-enough-01"


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- sessions ---------------------------------------------------------------------------
now = 1_800_000_000.0
cookie = web.session_value(TOKEN, now)
check("a fresh session is good", web.valid_session(TOKEN, cookie, now + 10), True)
check("for thirty days", web.valid_session(TOKEN, cookie, now + 29 * 86400), True)
check("and not after", web.valid_session(TOKEN, cookie, now + 31 * 86400), False)
check("a changed password ends every session", web.valid_session(TOKEN + "x", cookie, now), False)
issued, _, sig = cookie.partition(".")
check("a forged time fails", web.valid_session(TOKEN, f"{int(issued) + 5}.{sig}", now), False)
check("one from the future fails", web.valid_session(TOKEN, web.session_value(TOKEN, now + 3600), now), False)
check("junk fails", [web.valid_session(TOKEN, v, now) for v in ("", "x", "abc.def", None)], [False] * 4)
check("off below 32 characters", (web.enabled(isolated(dashboard_token="short")),
                                  web.enabled(isolated(dashboard_token=TOKEN))), (False, True))

guesses = web.Guesses()
for i in range(5):
    guesses.wrong("1.2.3.4", now + i)
check("five wrong guesses and that address waits", guesses.blocked("1.2.3.4", now + 6), True)
check("others don't", guesses.blocked("5.6.7.8", now + 6), False)
check("ten minutes later it may try again", guesses.blocked("1.2.3.4", now + 700), False)

# -- the day rail ---------------------------------------------------------------------------
today = date(2026, 9, 28)


def at(h, m=0, d=0):
    return datetime(2026, 9, 28, h, m, tzinfo=DEN) + timedelta(days=d)


placed = web.rail({
    "shifts": [{"starts_at": at(15), "ends_at": at(19)}, {"starts_at": at(15, d=1), "ends_at": at(19, d=1)},
               {"starts_at": at(8), "ends_at": at(9), "cancelled": True}],
    "events": [{"title": "DECA meeting", "starts_at": at(12), "ends_at": at(12, 30)},
               {"title": "All day", "starts_at": at(0), "ends_at": at(0, d=1), "all_day": True}],
    "assignments": [{"title": "Lab report", "due_at": at(23, 59)}, {"title": "Essay", "due_at": at(8, d=1)}],
    "reminders": [{"text": "call Keegan", "due_at": at(20, 30)}],
}, DEN, today)
check("today's items, in time order; tomorrow, cancelled and all-day ones left off",
      [(i["kind"], i["label"], i["start"], i.get("end")) for i in placed],
      [("event", "DECA meeting", 720, 750), ("shift", "Work", 900, 1140), ("reminder", "call Keegan", 1230, None),
       ("due", "Lab report", 1439, None)])
day = web.agenda({
    "shifts": [{"starts_at": at(15), "ends_at": at(19)}, {"starts_at": at(15, d=1), "ends_at": at(19, d=1)}],
    "events": [{"title": "DECA meeting", "starts_at": at(7, 15), "ends_at": at(7, 55), "location": "Room 204"},
               {"title": "Team call", "starts_at": at(16), "ends_at": at(16, 30)},
               {"title": "Homecoming week", "starts_at": at(0), "ends_at": at(0, d=1), "all_day": True},
               {"title": "Late show", "starts_at": at(23), "ends_at": at(1, d=1)}],
    "assignments": [{"title": "Lab report", "due_at": at(23, 59), "course": "AP Physics"},
                    {"title": "Essay", "due_at": at(8, d=1), "course": "English 12"}],
    "reminders": [{"text": "call Keegan", "due_at": at(20, 45)}, {"text": "meds", "due_at": at(7, d=1)}],
}, "America/Denver", today)
check("the agenda: all-day first, then the day in order, reminders included",
      [(i["kind"], i["title"], i["time"], i["start"], i["end"]) for i in day["items"]],
      [("event", "Homecoming week", "all day", 0, 1440),
       ("event", "DECA meeting", "7:15 AM–7:55 AM", 435, 475),
       ("shift", "Work", "3:00 PM–7:00 PM", 900, 1140),
       ("event", "Team call", "4:00 PM–4:30 PM", 960, 990),
       ("reminder", "call Keegan", "8:45 PM", 1245, 1245),
       ("event", "Late show", "11:00 PM–1:00 AM", 1380, 1440),
       ("due", "Lab report", "11:59 PM", 1439, 1439)])
check("with the place or the course beside it", [i["sub"] for i in day["items"]][1:], ["Room 204", "", "", "", "", "AP Physics"])
check("and what collides, as /today says it", day["clashes"], [{"hard": True, "what": "Team call", "against": "your shift"}])
check("tomorrow is its own day",
      [(i["kind"], i["title"]) for i in web.agenda({"shifts": [{"starts_at": at(15, d=1), "ends_at": at(19, d=1)}],
                                                   "reminders": [{"text": "meds", "due_at": at(7, d=1)}]},
                                                  "America/Denver", today + timedelta(days=1))["items"]],
      [("reminder", "meds"), ("shift", "Work")])
check("when, said the way the page says it", [web._when(x, DEN, today) for x in (at(17), at(7, d=1), at(9, d=3), None)],
      ["Today 5:00 PM", "Tomorrow 7:00 AM", "Thu 9:00 AM", ""])


# -- the routes, over ASGI ------------------------------------------------------------------------
async def integration() -> None:
    import httpx
    from fastapi import FastAPI

    from sloane.agency import Agency, reminder_action
    from sloane.contract import Reply
    from sloane.memory.store import Store
    from sloane.telegram import Bot

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver", telegram_chat_id=5151,
                      dashboard_token=TOKEN)
    async with Store(config) as store:
        for table in ("reminders", "proposals"):
            await store._exec(f"delete from {table}")
        await store._exec("delete from working_set where kind = 'follow_up'")
        await store._exec("delete from state where category = 'learned' or key = 'school.name'")
        await store._exec("delete from messages where chat_id = 5151")

        class Agent:
            asked: list = []

            async def answer(self, question, *, channel="", on_text=None, can_act=False, ingested=""):
                Agent.asked.append((question, channel, can_act))
                raw = '{"speech": "Lab first, it\'s due at 11:59.", "detail": "Then the essay."}'
                if on_text is not None:
                    for cut in (12, 30, len(raw)):
                        await on_text(raw[:cut])
                return Reply(speech="Lab first, it's due at 11:59.", detail="Then the essay.")

        said: list[str] = []

        async def tell(text):
            said.append(text)

        async def ask(row):
            return None

        bot = Bot(store, Agent(), config)
        agency = Agency(store, config, ask=ask, tell=tell)
        agency.register(reminder_action(tell))
        bot.agency = agency

        class Scheduler:
            ran: list = []

            async def run(self, name):
                Scheduler.ran.append(name)

                class Result:
                    ran, reason, reply = True, "ok", None
                return Result()

            def next_runs(self):
                return {}

        state = {"responder": bot, "scheduler": Scheduler()}
        app = FastAPI()
        web.install(app, state, store, config)
        off = FastAPI()
        web.install(off, {}, store, isolated(database_url=os.environ["DATABASE_URL"], dashboard_token=""))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://box") as client, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=off), base_url="http://box") as dark:
            check("off without a password", ((await dark.get("/app")).status_code,
                                             (await dark.get("/api/overview")).status_code), (404, 404))

            first = await client.get("/app")
            check("signed out, /app sends you to the door", (first.status_code, first.headers.get("location")),
                  (303, "/app/login"))
            door = await client.get("/app/login?wrong=<script>")
            check("the door never echoes what it was sent", ("<script>" in door.text, "isn't the password" in door.text),
                  (False, True))
            check("and is framed by nobody, loads nothing outside",
                  ("frame-ancestors 'none'" in door.headers["content-security-policy"],
                   "default-src 'none'" in door.headers["content-security-policy"]), (True, True))

            wrong = await client.post("/app/login", content="token=nope",
                                      headers={"content-type": "application/x-www-form-urlencoded"})
            check("a wrong password: back to the door, no session",
                  (wrong.headers.get("location"), "sloane_session" in wrong.headers.get("set-cookie", "")),
                  ("/app/login?wrong=1", False))

            # Everything that reads or changes refuses a stranger.
            posts = ["/api/chat", "/api/reminders/00000000-0000-0000-0000-000000000000/cancel",
                     "/api/proposals/00000000-0000-0000-0000-000000000000/approve", "/api/trust/revoke",
                     "/api/memory/forget", "/api/followups/00000000-0000-0000-0000-000000000000/close",
                     "/api/jobs/entity_sync/run", "/app/logout", "/api/workshop/ideas",
                     "/api/workshop/00000000-0000-0000-0000-000000000000/accept",
                     "/api/workshop/00000000-0000-0000-0000-000000000000/build", "/api/voice"]
            check("signed out: every read is refused",
                  [(await client.get(p)).status_code for p in ("/api/overview", "/api/history", "/api/workshop")],
                  [401, 401, 401])
            check("signed out: every change is refused",
                  [(await client.post(p, json={}, headers={"X-Sloane": "1"})).status_code for p in posts], [401] * len(posts))

            right = await client.post("/app/login", content=f"token={TOKEN}",
                                      headers={"content-type": "application/x-www-form-urlencoded"})
            set_cookie = right.headers.get("set-cookie", "")
            check("the right password lets him in", right.headers.get("location"), "/app")
            check("with a session only the page's own requests carry",
                  ("HttpOnly" in set_cookie, "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()),
                  (True, True))
            check("/app now serves the room", (await client.get("/app")).status_code, 200)
            check("a change without X-Sloane is refused (no cross-site forms)",
                  [(await client.post(p, json={})).status_code for p in posts], [403] * len(posts))

            # -- the chat is the Telegram conversation ------------------------------------
            async def chat(text):
                response = await client.post("/api/chat", json={"text": text}, headers={"X-Sloane": "1"})
                return response.status_code, [json.loads(line) for line in response.text.splitlines() if line.strip()]

            code, events = await chat("remind me at 5 to call Keegan")
            check("a rule answers without the model", (code, [e["t"] for e in events], Agent.asked),
                  (200, ["reply", "done"], []))
            check("and did it", events[0]["speech"].startswith("Okay, I'll remind you"), True)
            code, events = await chat("what first tonight?")
            kinds = [e["t"] for e in events]
            check("her answer streams, then lands whole", (kinds[0], "partial" in kinds, kinds[-2:]),
                  ("typing", True, ["reply", "done"]))
            check("through the same path as Telegram, allowed to act, marked as the dashboard",
                  Agent.asked[-1], ("what first tonight?", "dashboard", True))
            check("the reply", (events[-2]["speech"], events[-2]["detail"]), ("Lab first, it's due at 11:59.", "Then the essay."))
            code, events = await chat("/reminders")
            check("commands work here too", "1 reminder set." in events[0]["speech"], True)
            logged = await store._fetch("select direction, body from messages where chat_id = 5151 order by at")
            check("both sides land in the one conversation log",
                  [(r["direction"], r["body"][:22]) for r in logged][:4],
                  [("in", "remind me at 5 to call"), ("out", "Okay, I'll remind you "),
                   ("in", "what first tonight?"), ("out", "Lab first, it's due at")])
            history = (await client.get("/api/history")).json()
            check("and the page reads it back", [h["from"] for h in history][:4], ["him", "her", "him", "her"])
            check("an empty message is refused", (await chat("   "))[0], 400)

            # -- the microphone: a voice note, in words, into the same conversation ----------------
            async def speak(audio, kind="audio/webm;codecs=opus"):
                response = await client.post("/api/voice", content=audio, headers={"X-Sloane": "1", "content-type": kind})
                lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
                return response.status_code, lines

            heard: list = []

            async def transcribe(audio, filename="note.ogg"):
                heard.append((audio, filename))
                return "  what first tonight?  "

            check("no transcriber, no microphone", ((await speak(b"RIFF"))[0], bot.hears), (400, False))
            bot._config = isolated(**{**config.model_dump(), "groq_api_key": "gsk_" + "k" * 40})
            bot.transcribe = transcribe  # type: ignore[method-assign]
            check("with one, it hears", bot.hears, True)
            check("only audio", (await speak(b"<html>", "text/html"))[0], 415)
            check("and something recorded", (await speak(b""))[0], 400)
            check("not a long one", (await speak(b"x" * (web.MAX_AUDIO + 1)))[0], 413)
            code, events = await speak(b"\x1aE\xdf\xa3 webm bytes")
            check("his words come back first, then her answer, streamed",
                  (code, events[0], [e["t"] for e in events][-2:]),
                  (200, {"t": "heard", "text": "what first tonight?"}, ["reply", "done"]))
            check("read in the format it was recorded in", heard[-1][1], "note.webm")
            check("Safari's recording too", ((await speak(b"mp4", "audio/mp4"))[0], heard[-1][1]), (200, "note.m4a"))
            said_row = await store._one(
                "select kind, body from messages where chat_id = 5151 and direction = 'in' order by at desc limit 1")
            check("logged as his voice note, in words (never the audio)", (said_row["kind"], said_row["body"]),
                  ("voice", "what first tonight?"))
            check("and the page shows it was spoken", (await client.get("/api/history")).json()[-2].get("voice"), True)

            async def mumble(audio, filename="note.ogg"):
                raise RuntimeError("whisper said no")

            bot.transcribe = mumble  # type: ignore[method-assign]
            failed = await client.post("/api/voice", content=b"x", headers={"X-Sloane": "1", "content-type": "audio/webm"})
            check("a failed transcription is said plainly", (failed.status_code, "couldn't make that out" in failed.text),
                  (502, True))

            # -- the controls ---------------------------------------------------------------
            overview = (await client.get("/api/overview")).json()
            check("the overview has every part", sorted(overview) == sorted([
                "now", "name", "unreadable", "alerts", "rail", "schedule", "due", "overdue", "grades", "reminders",
                "promises", "proposals", "panels", "learned", "loose", "diary", "jobs", "trust", "usage", "system",
                "agenda", "talk", "health"]), True)
            check("the page knows whether it may listen", overview["talk"], {"hears": True, "lang": "en-US"})
            reminder = overview["reminders"][0]
            done = await client.post(f"/api/reminders/{reminder['id']}/cancel", headers={"X-Sloane": "1"})
            check("cancel a reminder", done.json(), {"ok": True, "message": "Cancelled: call Keegan."})
            again = await client.post(f"/api/reminders/{reminder['id']}/cancel", headers={"X-Sloane": "1"})
            check("twice is a plain no", (again.status_code, again.json()["ok"]), (400, False))

            await store.put_state("learned.note.vegetarian", "I'm vegetarian now", category="learned", pin=True)
            await store.put_state("school.name", "Chaparral")
            forgot = await client.post("/api/memory/forget", json={"key": "learned.note.vegetarian"}, headers={"X-Sloane": "1"})
            check("forget something she learned", forgot.json(), {"ok": True, "message": "Forgotten: I'm vegetarian now."})
            seeded = await client.post("/api/memory/forget", json={"key": "school.name"}, headers={"X-Sloane": "1"})
            check("but not what he seeded", seeded.json()["ok"], False)

            await store.add_follow_up("call the dentist", None)
            loose = (await client.get("/api/overview")).json()["loose"][0]
            closed = await client.post(f"/api/followups/{loose['id']}/close", headers={"X-Sloane": "1"})
            check("close a loose end", closed.json()["message"], "Closed: call the dentist.")

            outcome = await agency.propose("remind", "self", {"text": "bring the goggles"})
            pid = str(outcome.proposal["id"])
            check("a waiting approval shows", [p["id"] for p in (await client.get("/api/overview")).json()["proposals"]], [pid])
            approved = await client.post(f"/api/proposals/{pid}/approve", headers={"X-Sloane": "1"})
            check("approve it from the page", (approved.json()["ok"], said[-1]), (True, "Reminder: bring the goggles"))
            stale = await client.post(f"/api/proposals/{pid}/deny", headers={"X-Sloane": "1"})
            check("a decided one can't be decided again", stale.json()["ok"], False)
            bogus = await client.post("/api/proposals/not-a-uuid/approve", headers={"X-Sloane": "1"})
            check("a malformed id decides nothing", bogus.json()["ok"], False)

            # The top bar said "All systems normal" while a job's last run had failed.
            await store.mark_job("inbox", status="failed", error="invalid_grant")
            health = (await client.get("/api/overview")).json()["health"]
            check("a failed job shows in the top bar's light", "inbox" in health["failed_jobs"], True)
            await store.mark_job("inbox", status="ok")
            check("and clears when it runs clean", "inbox" in (await client.get("/api/overview")).json()["health"]["failed_jobs"],
                  False)

            ran = await client.post("/api/jobs/entity_sync/run", headers={"X-Sloane": "1"})
            check("run a job now", (ran.json()["ok"], Scheduler.ran), (True, ["entity_sync"]))
            check("only a job name", (await client.post("/api/jobs/..%2Fetc/run", headers={"X-Sloane": "1"})).status_code
                  in (400, 404), True)

            asset = await client.get("/app/static/app.js")
            check("the script is served, with the same CSP", (asset.status_code, "script-src 'self'" in
                                                               asset.headers["content-security-policy"]), (200, True))
            check("nothing else is", (await client.get("/app/static/..%2Fweb.py")).status_code, 404)
            icon = await client.get("/app/static/icon-180.png")
            check("the home-screen icon is served as an image", (icon.status_code, icon.headers["content-type"],
                                                                 icon.content[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
            manifest = (await client.get("/app/static/manifest.json")).json()
            check("and the install manifest names icons that exist",
                  [(await client.get(i["src"])).status_code for i in manifest["icons"]], [200] * len(manifest["icons"]))

            out = await client.post("/app/logout", headers={"X-Sloane": "1"})
            check("sign out", out.status_code, 200)

            # -- too many wrong guesses ---------------------------------------------------------
            for _ in range(5):
                await client.post("/app/login", content="token=nope",
                                  headers={"content-type": "application/x-www-form-urlencoded"})
            waited = await client.post("/app/login", content=f"token={TOKEN}",
                                       headers={"content-type": "application/x-www-form-urlencoded"})
            check("after five wrong, even the right one waits", waited.headers.get("location"), "/app/login?wait=1")

        await store._exec("delete from reminders")
        await store._exec("delete from proposals")
        await store._exec("delete from working_set where kind = 'follow_up'")
        await store._exec("delete from state where category = 'learned' or key = 'school.name'")
        await store._exec("delete from messages where chat_id = 5151")
        await store._exec("delete from trust where action = 'remind' and target = 'self' and not hard_line")


if os.environ.get("DATABASE_URL"):
    # The wrong-password pause is a second each; the test doesn't need it.
    real_sleep = asyncio.sleep

    async def quick(seconds, *a, **k):
        return await real_sleep(min(seconds, 0.01), *a, **k)

    web.asyncio.sleep = quick  # type: ignore[attr-defined]
    try:
        asyncio.run(integration())
    finally:
        web.asyncio.sleep = real_sleep  # type: ignore[attr-defined]

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("web: sessions, refusals, the shared conversation and every control")
