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

        async def spy(text, *, sessions=True):
            routed.append(sessions)
            return await original(text, sessions=sessions)

        skills.route = spy
        await ingest(store, on, {"text": "mitochondria"}, auth, now=NOW, skills=skills)
        check("capture never feeds an open session", routed, [False])
        await store.end_session()
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

    # -- the route exists and is the only POST besides the loopback admin ones ----
    from sloane.main import create_app
    posts = sorted(r.path for r in create_app().routes if "POST" in getattr(r, "methods", set()))
    check("POST routes", posts, ["/capture", "/jobs/{name}/run", "/sync"])


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("capture: auth, validation, trusted storage and captured reminders all pass")
