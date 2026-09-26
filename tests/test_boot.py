"""The whole process, booted the way the box boots it, against a fake Telegram.

What an install depends on and no other suite covers end to end: the app starts
on a migrated database, the poller answers his messages, she says hello once,
the scheduler loads every job, the stale sync catches up, and /health is ok.
No model is called: every message here is answered without one.

DESTRUCTIVE: deletes hello:* keys and this test's messages, and resets
entity_sync's last run.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import EXTERNAL

FAILURES: list[str] = []
CHAT = 9_100_042


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Telegram:
    """Just enough of the Bot API: queued updates out, every call recorded."""

    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def texts(self) -> list[dict]:
        with self.lock:
            return [p for m, p in self.calls if m == "sendMessage"]

    def handle(self, method: str, payload: dict):
        with self.lock:
            self.calls.append((method, payload))
            if method != "getUpdates":
                return {"message_id": len(self.calls)}
            offset = int(payload.get("offset") or 1)
            out = []
            for i, text in enumerate(self.queue):
                out.append({"update_id": offset + i, "message": {
                    "message_id": offset + i, "date": int(time.time()),
                    "chat": {"id": CHAT, "type": "private"}, "from": {"id": CHAT}, "text": text}})
            self.queue.clear()
        if not out:
            time.sleep(0.2)  # a short long poll
        return out


def serve(telegram: Telegram) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            body = json.dumps({"ok": True, "result": telegram.handle(method, payload)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # the poller hanging up mid-poll at shutdown

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_for(condition, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def main() -> None:
    import psycopg
    from psycopg.rows import dict_row

    database_url = os.environ["DATABASE_URL"]

    def sql(statement: str, *args) -> list[dict]:
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
            cur = conn.execute(statement, args)
            return cur.fetchall() if cur.description else []

    def reset():
        sql("delete from nudges_said where key like 'hello:%%'")
        sql("delete from messages where chat_id = %s", CHAT)

    reset()
    sql("update jobs set last_run_at = null, last_status = null where name = 'entity_sync'")

    telegram = Telegram()
    server = serve(telegram)

    # The environment an install writes, minus every real service: no model key
    # (nothing here needs one), no Canvas, no calendar.
    for field in EXTERNAL:
        os.environ[field.upper()] = ""
    os.environ.update({
        "DATABASE_URL": database_url,
        "TELEGRAM_BOT_TOKEN": "123:boot",
        "TELEGRAM_CHAT_ID": str(CHAT),
        "TELEGRAM_POLL_TIMEOUT": "1",
        "MAIN_PROVIDER": "groq",
        "PIPER_VOICE": "",
    })
    os.chdir(tempfile.mkdtemp())  # no stray .env

    import sloane.telegram
    from sloane.agent import Agent
    from sloane.config import settings
    from sloane.jobs.scheduler import Scheduler
    from sloane.memory.embed import Embedder

    sloane.telegram.API = f"http://127.0.0.1:{server.server_port}"
    settings.cache_clear()

    async def cold(*args, **kwargs):
        return False

    Embedder.warm = cold  # a 130 MB download isn't what this tests
    Agent.prewarm = cold
    start = Scheduler.start

    async def start_quietly(self):
        # Every job's cron is parsed and loaded as on the box, then unloaded,
        # so nothing fires on its own mid-test.
        await start(self)
        state["skipped"] = list(self.skipped)
        state["loaded"] = len(self._sched.get_jobs())
        self._sched.remove_all_jobs()

    state: dict = {}
    Scheduler.start = start_quietly

    from fastapi.testclient import TestClient

    from sloane.hello import FIRST
    from sloane.main import create_app

    with TestClient(create_app()) as client:
        check("she says hello once she's up", wait_for(lambda: any(
            p.get("text") == FIRST for p in telegram.texts())), True)
        health = client.get("/health").json()
        check("/health", (health["ok"], health["database"], health["bot"], {"colleges", "deca"} <= set(health["skills"])),
              (True, "up", "polling", True))
        check("every job loads", (state.get("skipped"), state.get("loaded", 0) > 5), ([], True))
        check("the stale sync caught up", wait_for(lambda: sql(
            "select last_run_at from jobs where name = 'entity_sync'")[0]["last_run_at"] is not None), True)

        before = len(telegram.texts())
        telegram.queue.extend(["/help", "/status"])
        check("both answered", wait_for(lambda: len(telegram.texts()) >= before + 2), True)
        replies = telegram.texts()[before:]
        help_reply = next((p for p in replies if "Mostly, just talk to me" in p.get("text", "")), None)
        check("/help arrives formatted", (help_reply or {}).get("parse_mode"), "HTML")
        check("/help's tags are Telegram's", "<b>" in (help_reply or {}).get("text", ""), True)
        stored = sql("select direction, count(*) as n from messages where chat_id = %s "
                     "group by direction order by direction", CHAT)
        check("his messages and her replies are logged", {r["direction"]: r["n"] for r in stored}.get("in"), 2)

    # A restart of the same code: no second hello.
    telegram.calls.clear()
    with TestClient(create_app()):
        time.sleep(1.5)
    check("a restart says nothing", [p.get("text") for p in telegram.texts()], [])

    server.shutdown()
    reset()


if os.environ.get("DATABASE_URL"):
    main()

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("boot: the app starts, polls, says hello once, loads its jobs and catches up the sync")
