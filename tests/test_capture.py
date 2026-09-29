"""POST /capture: auth, validation, storage in his own voice, and captured reminders.

DESTRUCTIVE: deletes capture episodes and truncates reminders.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.capture import ingest
from sloane.memory.store import Store

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=DEN)
TOKEN = "t" * 40


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    on = isolated(database_url=url, timezone="America/Denver", capture_token=TOKEN)
    async with Store(on) as store:
        await store._exec("delete from episodes where source = 'capture'")
        await store._exec("truncate reminders")
        auth = f"Bearer {TOKEN}"

        async def post(payload, header=auth, config=on):
            return await ingest(store, config, payload, header, now=NOW)

        # -- off unless a real token is set ------------------------------------
        check("unset token: disabled", (await post({"text": "x"}, config=isolated(database_url=url))).status, 503)
        weak = isolated(database_url=url, capture_token="short")
        check("a short token counts as unset", (await post({"text": "x"}, "Bearer short", weak)).status, 503)

        # -- auth ---------------------------------------------------------------
        check("no header", (await post({"text": "x"}, None)).status, 401)
        check("wrong token", (await post({"text": "x"}, "Bearer " + "u" * 40)).status, 401)
        check("wrong scheme", (await post({"text": "x"}, f"Basic {TOKEN}")).status, 401)
        check("a prefix of the token is not the token", (await post({"text": "x"}, f"Bearer {TOKEN[:-1]}")).status, 401)

        # -- validation -----------------------------------------------------------
        check("not an object", (await post(["x"])).status, 400)
        check("no text", (await post({"kind": "note"})).status, 400)
        check("blank text", (await post({"text": "   "})).status, 400)
        check("too long", (await post({"text": "x" * 20_001})).status, 413)
        check("bad kind", (await post({"text": "x", "kind": "sql"})).status, 400)
        check("bad timestamp", (await post({"text": "x", "captured_at": "yesterday"})).status, 400)

        # -- stored in his own voice ------------------------------------------------
        ok = await post({"text": "Idea: bakery site needs online ordering", "kind": "transcript",
                         "captured_at": "2026-09-21T18:30:00-06:00"})
        check("stored", (ok.status, ok.body["stored"], ok.body["kind"]), (201, True, "transcript"))
        rows = await store._fetch(
            "select role, trusted, source, channel, occurred_at from episodes where source = 'capture'"
        )
        check("as his own words: trusted, user, from capture",
              [(r["role"], r["trusted"], r["source"], r["channel"]) for r in rows],
              [("user", True, "capture", "capture")])
        check("dated when he said it", rows[0]["occurred_at"].astimezone(DEN).strftime("%m-%d %H:%M"), "09-21 18:30")
        future = await post({"text": "clock skew", "captured_at": "2030-01-01T00:00:00Z"})
        check("a future timestamp is clamped to now", future.status, 201)

        # -- a captured reminder is a reminder ---------------------------------------
        r = await post({"text": "Remind me tomorrow at 7 to bring the lab"})
        check("the reminder is set", r.body.get("reminder"), "tomorrow at 7:00 AM: bring the lab")
        set_ = await store.upcoming_reminders()
        check("and scheduled, marked as from capture",
              [(x["text"], x["source"]) for x in set_], [("bring the lab", "capture")])
        vague = await post({"text": "remind me about the lab thing"})
        check("no time: stored as a note, and says so", (vague.status, vague.body.get("reminder")), (201, None))

        # -- a skill's rule acts on it too, but never as a quiz answer ----------------
        from sloane.skills import Registry, SkillContext
        from sloane.skills.lists import Lists

        await store._exec("delete from list_items where list = 'capturetest'")
        skill_ctx = SkillContext(store=store, config=on)
        skills = Registry([Lists(skill_ctx)], skill_ctx)
        listed = await ingest(store, on, {"text": "add milk to my capturetest list"}, auth, now=NOW, skills=skills)
        check("a list add from Capture", (listed.status, listed.body.get("action")),
              (201, "Added milk to your capturetest list; 1 thing on it now."))
        plain = await ingest(store, on, {"text": "bakery idea: pickup lockers"}, auth, now=NOW, skills=skills)
        check("an ordinary note has no action", "action" in plain.body, False)
        await store.start_session("lists", {})
        routed: list[bool] = []
        original = skills.route

        async def spy(text, *, sessions=True, at=None):
            routed.append(sessions)
            return await original(text, sessions=sessions, at=at)

        skills.route = spy
        await ingest(store, on, {"text": "mitochondria"}, auth, now=NOW, skills=skills)
        check("capture never feeds an open session", routed, [False])
        await store.end_session()
        await store._exec("delete from list_items where list = 'capturetest'")

        # A capture queued offline acts as of when he said it, not when it arrived.
        from sloane.skills.habits import Habits

        await store._exec("delete from habits where name = 'capture reading'")
        habit_ctx = SkillContext(store=store, config=on, clock=lambda: NOW)
        habits = Registry([Habits(habit_ctx)], habit_ctx)
        await store.add_habit("capture reading")
        late = await ingest(store, on, {"text": "did capture reading",
                                        "captured_at": "2026-09-21T23:50:00-06:00"}, auth, now=NOW, skills=habits)
        check("a late capture is marked for the day he said it", late.body.get("action"),
              "Marked capture reading. Streak: 1 day.")
        days = await store._fetch("select l.on_date from habit_log l join habits h on h.id = l.habit_id "
                                  "where h.name = 'capture reading'")
        check("in the log too", [str(d["on_date"]) for d in days], ["2026-09-21"])
        await store._exec("delete from habits where name = 'capture reading'")

        # -- a connection check stores nothing ------------------------------------------------
        before = await store._one("select count(*) as n from episodes where source = 'capture'")
        checked = await post({"check": True})
        after = await store._one("select count(*) as n from episodes where source = 'capture'")
        check("a check answers 200 and stores nothing", (checked.status, checked.body, after["n"] - before["n"]),
              (200, {"ok": True}, 0))
        check("a check with a wrong token is still refused", (await post({"check": True}, "Bearer " + "u" * 40)).status,
              401)

        # -- act: false is memory only (Capture acted on it itself) -----------------------
        await store._exec("truncate reminders")
        await store._exec("delete from list_items where list = 'capturetest'")
        quiet = await ingest(store, on, {"text": "remind me tomorrow at 7 to bring the lab", "act": False},
                             auth, now=NOW, skills=skills)
        check("act false: stored, no reminder set", (quiet.status, "reminder" in quiet.body,
                                                     len(await store.upcoming_reminders())), (201, False, 0))
        quiet_list = await ingest(store, on, {"text": "add milk to my capturetest list", "act": False},
                                  auth, now=NOW, skills=skills)
        check("act false: no skill action either", "action" in quiet_list.body, False)
        check("act must be a boolean", (await post({"text": "x", "act": "no"})).status, 400)

        # -- client_id: a retry after a lost response is the same capture --------------------
        await store._exec("truncate capture_refs")
        await store._exec("truncate reminders")
        first = await post({"text": "remind me tomorrow at 7 to bring the lab", "client_id": "cap-123"})
        again = await post({"text": "remind me tomorrow at 7 to bring the lab", "client_id": "cap-123"})
        check("the retry gets the first answer back", (first.status, again.status, again.body.get("id"),
                                                       again.body.get("duplicate")),
              (201, 200, first.body.get("id"), True))
        check("and nothing twice: one reminder", len(await store.upcoming_reminders()), 1)
        check("a different id is a different capture",
              (await post({"text": "bakery idea", "client_id": "cap-124"})).status, 201)
        check("a bad id is refused", (await post({"text": "x", "client_id": "has spaces"})).status, 400)
        await store._exec("insert into capture_refs (client_id) values ('cap-inflight')")
        check("one still being stored: retry later", (await post({"text": "x", "client_id": "cap-inflight"})).status,
              409)

        class Broken:
            async def embed_one(self, text):
                raise RuntimeError("disk full")

        try:
            await ingest(store, on, {"text": "x", "client_id": "cap-broken"}, auth, now=NOW, embedder=Broken())
        except RuntimeError:
            pass
        check("a failed store lets its retry in", await store.capture_response("cap-broken"), None)
        await store._exec("truncate capture_refs")
        await store._exec("truncate reminders")
        await store._exec("delete from list_items where list = 'capturetest'")

    # -- the route caps the body before reading it all -------------------------------
    import httpx

    from sloane.main import create_app as _make

    app = _make()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://box") as client:
        big = await client.post("/capture", content=b"x" * 70_000,
                                headers={"Authorization": f"Bearer {TOKEN}"})
        check("an oversized body is refused", big.status_code, 413)

        async def chunked():
            for _ in range(80):
                yield b"x" * 1000

        sneaky = await client.post("/capture", content=chunked(),
                                   headers={"Authorization": f"Bearer {TOKEN}"})
        check("so is one with no declared length", sneaky.status_code, 413)

    # -- the route exists; every other POST is a loopback admin one or the control
    # room's (each of those refuses without a session and the X-Sloane header:
    # tests/test_web.py). A new POST anywhere must be added here on purpose.
    from sloane.main import create_app
    posts = sorted(r.path for r in create_app().routes if "POST" in getattr(r, "methods", set()))
    check("POST routes", posts, [
        "/api/chat", "/api/followups/{item_id}/close", "/api/jobs/{name}/run", "/api/memory/forget",
        "/api/prefs", "/api/proposals/{proposal_id}/{decision}", "/api/reminders/{reminder_id}/cancel", "/api/trust/revoke",
        "/api/voice", "/api/workshop/ideas", "/api/workshop/{item_id}/{verb}",
        "/app/login", "/app/logout", "/capture", "/jobs/{name}/run", "/sync",
    ])


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("capture: auth, validation, trusted storage and captured reminders all pass")
