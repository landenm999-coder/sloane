"""Whoop against a stub API: rotating refresh tokens kept across restarts, the numbers, workouts.

No network, no database. The stub refuses any refresh token but the newest, as
Whoop does, so a token that wasn't saved (or was reused) fails here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import types
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

import sloane.skills.whoop as whoop
from sloane.skills import Registry, SkillContext
from sloane.skills.whoop import Whoop, build, parse_day, zone

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


RECOVERY = {"records": [{"cycle_id": 1, "score_state": "SCORED", "score": {
    "recovery_score": 64, "resting_heart_rate": 52, "hrv_rmssd_milli": 58.3}}]}
CYCLE = {"records": [{"id": 1, "start": "2026-09-29T11:00:00Z", "end": None, "score_state": "SCORED",
                      "score": {"strain": 8.43}}]}
SLEEP = {"records": [
    {"id": "nap-1", "nap": True, "score_state": "SCORED", "score": {"stage_summary": {"total_in_bed_time_milli": 1}}},
    {"id": "night-1", "nap": False, "score_state": "SCORED", "score": {
        "sleep_performance_percentage": 91, "stage_summary": {
            "total_in_bed_time_milli": 8 * 3600000, "total_awake_time_milli": 30 * 60000}}}]}
WORKOUTS = {"records": [
    {"id": "w-run", "sport_name": "running", "start": "2026-09-29T00:30:00Z", "end": "2026-09-29T01:10:00Z",
     "score_state": "SCORED", "score": {"strain": 11.2, "distance_meter": 6437.4}},
    {"id": "w-lift", "sport_name": "weightlifting", "start": "2026-09-29T14:00:00Z", "end": "2026-09-29T14:45:00Z",
     "score_state": "SCORED", "score": {"strain": 7.0}},
]}


class Stub(BaseHTTPRequestHandler):
    current = "refresh-secret-0000"   # the only refresh token Whoop will take
    issued = 0
    api_calls: list[str] = []
    refreshes: list[str] = []
    retire_next = False               # the next API call answers 401 once
    refuse = False

    def do_POST(self):  # noqa: N802
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        Stub.refreshes.append(form.get("refresh_token", ""))
        if Stub.refuse or form.get("grant_type") != "refresh_token" or form.get("refresh_token") != Stub.current \
                or form.get("client_secret") != "shh":
            return self.reply(400, {"error": "invalid_grant"})
        Stub.issued += 1
        Stub.current = f"refresh-secret-{Stub.issued:04d}"
        self.reply(200, {"access_token": f"access-secret-{Stub.issued:04d}", "expires_in": 3600,
                         "refresh_token": Stub.current, "token_type": "bearer"})

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        Stub.api_calls.append(path)
        if self.headers.get("Authorization") != f"Bearer access-secret-{Stub.issued:04d}" or Stub.retire_next:
            Stub.retire_next = False
            return self.reply(401, {})
        body = {"/developer/v2/recovery": RECOVERY, "/developer/v2/cycle": CYCLE,
                "/developer/v2/activity/sleep": SLEEP, "/developer/v2/activity/workout": WORKOUTS}.get(path)
        self.reply(200 if body else 404, body or {})

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class FakeStore:
    def __init__(self):
        self.workouts: dict = {}

    async def add_workout(self, *, kind, minutes, distance_m, done_on, logged_at, source="him", source_id=None):
        if (source, source_id) in self.workouts:
            return None
        self.workouts[(source, source_id)] = (kind, minutes, round(distance_m) if distance_m else None, done_on)
        return {"id": source_id}

    async def active_session(self, idle_minutes):
        return None


day = parse_day(RECOVERY, CYCLE, SLEEP, WORKOUTS)
check("the numbers", (day.recovery, round(day.hrv), day.rhr, day.sleep_minutes, day.sleep_performance, day.strain),
      (64, 58, 52.0, 450, 91, 8.43))
check("a nap isn't last night", day.sleep_minutes, 450)
check("zones", [zone(x) for x in (90, 67, 66, 34, 33, None)], ["green", "green", "yellow", "yellow", "red", ""])
unscored = parse_day({"records": [{"score_state": "PENDING_SCORE"}]}, CYCLE, {"records": []}, {"records": []})
check("not scored yet: None, not zero", (unscored.recovery, unscored.sleep_minutes, unscored.strain), (None, None, 8.43))
check("off without its three settings", build(SkillContext(store=None, config=isolated())), None)


async def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    folder = Path(tempfile.mkdtemp())
    now = [1000.0]
    whoop.clock = types.SimpleNamespace(monotonic=lambda: now[0])
    heard: list[str] = []

    class Ear(logging.Handler):
        def emit(self, record):
            heard.append(record.getMessage())

    logging.getLogger("sloane").addHandler(Ear())

    def config(token="refresh-secret-0000"):
        return isolated(whoop_client_id="cid", whoop_client_secret="shh", whoop_refresh_token=token,
                        whoop_api_base=f"http://127.0.0.1:{server.server_port}", timezone="America/Denver",
                        embed_cache_dir=str(folder))

    store = FakeStore()
    clock = lambda: datetime(2026, 9, 29, 9, 0, tzinfo=DEN)  # noqa: E731
    ctx = SkillContext(store=store, config=config(), clock=clock)
    skill = build(ctx)
    check("on with its three settings", isinstance(skill, Whoop), True)
    reg = Registry([skill], ctx)

    check("FACTS before anything's fetched: nothing, and nobody asked", (await skill.facts(), Stub.api_calls), ([], []))
    answer = await reg.route("how did I sleep?")
    check("said", answer.speech,
          "Recovery is 64%, yellow, and you slept 7 hours 30 minutes, 91% of what you needed. Strain so far is 8.4.")
    check("listed", answer.detail.splitlines(),
          ["- Recovery: 64% (yellow), HRV 58 ms, resting HR 52", "- Sleep: 7h 30m, performance 91%",
           "- Strain so far today: 8.4"])
    saved = folder / "whoop.json"
    check("the new refresh token is saved at once, 600",
          (json.loads(saved.read_text())["refresh_token"], stat.S_IMODE(os.stat(saved).st_mode)),
          ("refresh-secret-0001", 0o600))
    check("one refresh for the four reads, not four", Stub.refreshes, ["refresh-secret-0000"])
    calls = len(Stub.api_calls)
    await reg.command("whoop", "")
    check("within 15 minutes: the cache", len(Stub.api_calls), calls)
    check("FACTS, from the cache", await skill.facts(),
          ["- WHOOP today: recovery 64% (yellow), HRV 58 ms, resting HR 52, last night's sleep 7h 30m "
           "(91% of need), strain so far 8.4"])
    check("his workouts, into the workouts table, in his day",
          sorted(store.workouts.values()), sorted([("run", 40, 6437, datetime(2026, 9, 28).date()),
                                                   ("lift", 45, None, datetime(2026, 9, 29).date())]))

    # -- the heartbeat syncs workouts without anyone asking -------------------------------------------
    store.workouts.clear()
    now[0] += 16 * 60
    calls = len(Stub.api_calls)
    check("the heartbeat says nothing, but syncs",
          (await skill.nudges(), len(Stub.api_calls) > calls, len(store.workouts)), ([], True, 2))

    # -- a restart: the token on the volume, not the used-up one in .env -----------------------------
    now[0] += 16 * 60
    again = build(SkillContext(store=store, config=config(), clock=clock))
    panel = await again.panel()
    check("after a restart, the saved token works (the .env one is spent)",
          (panel["recovery"], Stub.refreshes[-1]), (64, "refresh-secret-0001"))
    check("the panel", (panel["zone"], panel["hrv"], panel["sleep_minutes"], panel["strain"]), ("yellow", 58, 450, 8.4))
    check("workouts once, however often it syncs", len(store.workouts), 2)

    # -- an access token Whoop retired early: one fresh one, then on ---------------------------------
    now[0] += 16 * 60
    Stub.retire_next = True
    check("a 401: refreshed once, and answered", (await again.panel())["recovery"], 64)

    # -- he runs whoop_auth.py again: the new .env token wins over the old file -----------------------
    Stub.current = "refresh-secret-9000"
    now[0] += 16 * 60
    fresh = build(SkillContext(store=store, config=config("refresh-secret-9000"), clock=clock))
    check("a new consent starts over", ((await fresh.panel())["recovery"], Stub.refreshes[-1]),
          (64, "refresh-secret-9000"))

    # -- Whoop refuses: said plainly; old numbers only while they're recent ----------------------------
    Stub.refuse = True
    now[0] += 16 * 60
    fresh.tokens.access = ""
    answer = await fresh.report()
    check("refused: the last numbers, marked", "isn't answering" in answer.detail, True)
    now[0] += 7 * 3600
    check("hours later: said plainly", (await fresh.report()).speech, "I can't reach Whoop right now. Try again in a few minutes.")
    check("and FACTS drops them", await fresh.facts(), [])

    everything = " ".join(heard) + json.dumps(await fresh.panel())
    check("no token ever in a log line or a panel", "secret" in everything, False)
    server.shutdown()


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("whoop: rotating tokens kept across restarts, the numbers, FACTS, the panel and workouts")
