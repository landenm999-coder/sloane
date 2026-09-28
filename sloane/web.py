"""The control room: /app, one page where he talks to her and runs everything she does.

    Talk     the same conversation as Telegram -- she's one person on both.
             Commands, skill phrases, reminders, actions: all of it works here,
             through the same `Bot.respond`, and the exchange lands in the same
             log, so CONVERSATION, recall and the nightly learner see it too.
    Today    the day on a rail, what needs him (alerts, approvals), what's due,
             reminders, grades, and every skill's panel.
    Memory   what she's learned about him (forget any), loose ends (close
             them), and her diary of the last week.
    Engine   jobs (run one now), trust (take one back), the models and what
             they've cost, and her settings as they stand.

Who may use it: whoever has DASHBOARD_TOKEN, a password of 32+ characters
(unset or shorter, /app is off). The login page trades it for a session
cookie: HttpOnly, SameSite=Strict, signed with the token (so changing the
token logs every session out), good for 30 days. Every change -- a message,
a button -- also needs the `X-Sloane: 1` header, which a page on another
site can't send without a CORS preflight this app never grants: that is the
cross-site-request defence, on top of SameSite. Wrong guesses from one
address are slowed after five.

Reach it like Capture: on the box's loopback, published to his own devices
with `tailscale serve` (DEPLOY section 7e). The page loads nothing from
outside (CSP 'self'), so it works on a tailnet with no internet.

No SQL here (invariant 2): it reads through the Store, acts through the
Agency, the Scheduler and the Bot. It never builds a Reply (invariant 6);
it only carries one to the browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from sloane.memory.store import remember
from sloane.telegram import STREAM_MIN_CHARS, Outlet, _shown

log = logging.getLogger(__name__)

UI = Path(__file__).resolve().parent / "webui"
COOKIE = "sloane_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_TOKEN = 32
MAX_MESSAGE = 4000
# A streamed reply is sent to the page at most this often.
PARTIAL_EVERY = 0.12
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_JOB = re.compile(r"^[a-z_]{1,40}$")
ASSETS = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml",
          "manifest.json": "application/manifest+json"}

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


# -- sessions --------------------------------------------------------------------------

def enabled(config) -> bool:  # noqa: ANN001
    return len(config.dashboard_token or "") >= MIN_TOKEN


def _sign(token: str, issued: int) -> str:
    return hmac.new(token.encode(), f"sloane-session:{issued}".encode(), hashlib.sha256).hexdigest()


def session_value(token: str, now: float) -> str:
    issued = int(now)
    return f"{issued}.{_sign(token, issued)}"


def valid_session(token: str, value: str | None, now: float) -> bool:
    """A cookie this token signed, in the last 30 days (and not from the future)."""
    if not token or not value or "." not in value:
        return False
    issued_raw, _, signature = value.partition(".")
    if not issued_raw.isdigit():
        return False
    issued = int(issued_raw)
    if not (now - SESSION_SECONDS <= issued <= now + 60):
        return False
    return hmac.compare_digest(signature, _sign(token, issued))


class Guesses:
    """Wrong passwords per address: after five in ten minutes, a ten-minute wait."""

    LIMIT = 5
    WINDOW = 600.0

    def __init__(self) -> None:
        self._seen: dict[str, list[float]] = {}

    def blocked(self, who: str, now: float) -> bool:
        recent = [t for t in self._seen.get(who, []) if now - t < self.WINDOW]
        self._seen[who] = recent
        return len(recent) >= self.LIMIT

    def wrong(self, who: str, now: float) -> None:
        self._seen.setdefault(who, []).append(now)

    def right(self, who: str) -> None:
        self._seen.pop(who, None)


# -- the chat outlet ---------------------------------------------------------------------

class _WebLive:
    """A reply being written, sent to the page as it grows (Bot.respond's `live`)."""

    def __init__(self, outlet: "WebOutlet") -> None:
        self.outlet = outlet
        self.shown = ""
        self.last = 0.0
        self.started = False
        self.typing = False

    async def start_typing(self, after: float = 0.0) -> None:
        self.typing = True
        if after:
            async def later() -> None:
                await asyncio.sleep(after)
                if self.typing:
                    self.outlet.emit({"t": "typing"})
            self.outlet.later(later())
        else:
            self.outlet.emit({"t": "typing"})

    def stop_typing(self) -> None:
        self.typing = False

    async def update(self, raw: str) -> None:
        from sloane.contract import partial_reply

        text = _shown(*partial_reply(raw))
        if len(text) < STREAM_MIN_CHARS or text == self.shown:
            return
        now = time.monotonic()
        if self.started and now - self.last < PARTIAL_EVERY:
            return
        self.started, self.shown, self.last = True, text, now
        self.outlet.emit({"t": "partial", "text": text})

    async def finish(self, reply) -> bool:  # noqa: ANN001 - contract.Reply
        """The finished reply replaces the draft. The page always has one to replace."""
        self.stop_typing()
        await self.outlet.send(reply)
        return True


class WebOutlet(Outlet):
    """Where a dashboard exchange's replies go: a queue the chat response streams from."""

    def __init__(self, store, chat_id: int) -> None:  # noqa: ANN001
        self.store = store
        self.chat_id = chat_id
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()

    def emit(self, event: dict) -> None:
        self.queue.put_nowait(event)

    def later(self, coro) -> None:  # noqa: ANN001
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def live(self) -> _WebLive:
        return _WebLive(self)

    async def send(self, reply, *, as_voice: bool = False) -> None:  # noqa: ANN001
        speech = (reply.speech or "").strip()
        detail = (reply.detail or "").strip()
        self.emit({"t": "reply", "speech": speech, "detail": "" if detail == speech else detail,
                   "outside": bool(reply.tainted)})
        # The same log Telegram writes: her side of the conversation she reads back.
        text = speech or detail or "(no reply)"
        if detail and detail != speech:
            text = f"{text}\n\n{detail}"
        await remember("outbound message", self.store.log_message(
            chat_id=self.chat_id, direction="out", kind="text", body=text[:4000], trusted=not reply.tainted))


# -- reading ----------------------------------------------------------------------------

def _clock(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _when(moment: datetime | None, zone: ZoneInfo, today: date) -> str:
    """'Today 5:00 PM', 'Tomorrow 7:00 AM', 'Fri 3:30 PM', 'Oct 12 9:00 AM'."""
    if not isinstance(moment, datetime):
        return ""
    local = moment.astimezone(zone)
    days = (local.date() - today).days
    if days == 0:
        day = "Today"
    elif days == 1:
        day = "Tomorrow"
    elif days == -1:
        day = "Yesterday"
    elif 1 < days < 7:
        day = f"{local:%a}"
    else:
        day = f"{local:%b} {local.day}"
    return f"{day} {_clock(local, zone)}"


def _minutes(moment: datetime, zone: ZoneInfo) -> int:
    local = moment.astimezone(zone)
    return local.hour * 60 + local.minute


def rail(data: dict, zone: ZoneInfo, today: date) -> list[dict]:
    """Today's items on the day rail: blocks (shifts, events) and marks (due, reminders)."""
    items: list[dict] = []

    def on_today(moment: Any) -> bool:
        return isinstance(moment, datetime) and moment.astimezone(zone).date() == today

    for s in data.get("shifts") or []:
        start, end = s.get("starts_at"), s.get("ends_at")
        if on_today(start) and isinstance(end, datetime) and not s.get("cancelled"):
            items.append({"kind": "shift", "label": "Work", "start": _minutes(start, zone),
                          "end": _minutes(end, zone) if on_today(end) else 24 * 60})
    for e in data.get("events") or []:
        start, end = e.get("starts_at"), e.get("ends_at")
        if on_today(start) and not e.get("all_day"):
            finish = _minutes(end, zone) if on_today(end) else _minutes(start, zone) + 60
            items.append({"kind": "event", "label": str(e.get("title") or "Event")[:60],
                          "start": _minutes(start, zone), "end": max(finish, _minutes(start, zone) + 15)})
    for a in data.get("assignments") or []:
        if on_today(a.get("due_at")):
            items.append({"kind": "due", "label": str(a.get("title") or "Due")[:60],
                          "start": _minutes(a["due_at"], zone)})
    for r in data.get("reminders") or []:
        if on_today(r.get("due_at")):
            items.append({"kind": "reminder", "label": str(r.get("text") or "")[:60],
                          "start": _minutes(r["due_at"], zone)})
    return sorted(items, key=lambda i: i["start"])


async def overview(store, config, state: dict, now: datetime) -> dict:  # noqa: ANN001
    """Everything the page shows, in one read. A failed part is named, not fatal."""
    from sloane import dashboard, views
    from sloane.reminders import repeat_spoken

    zone = ZoneInfo(config.timezone)
    now = now.astimezone(zone)
    today = now.date()
    skills = state.get("skills")
    data = await dashboard.collect(store, config, skills, now)
    reads = {
        "proposals": store.open_proposals(),
        "trust": store.trust_ledger(),
        "jobs": store.jobs(),
        "learned": store.learned_facts(),
        "loose": store.open_follow_ups(),
        "diary": store.recent_diary(7),
        "usage": store.usage_summary(24),
        "database": store.healthy(),
    }
    settled = await asyncio.gather(*reads.values(), return_exceptions=True)
    extra: dict[str, Any] = {}
    for name, result in zip(reads, settled):
        if isinstance(result, BaseException):
            log.warning("control room read %s failed: %s", name, result)
            data["unreadable"].append(name)
            extra[name] = False if name == "database" else []
        else:
            extra[name] = result

    upcoming = state["scheduler"].next_runs() if "scheduler" in state else {}
    tomorrow = today + timedelta(days=1)
    due = []
    for a in data.get("assignments") or []:
        due.append({"title": a.get("title"), "course": a.get("course"),
                    "when": _when(a.get("due_at"), zone, today)})
    return {
        "now": {"clock": _clock(now, zone), "date": f"{now:%A, %B} {now.day}", "minutes": _minutes(now, zone),
                "tz": config.timezone},
        "name": config.address_as or "Landen",
        "unreadable": data["unreadable"],
        "alerts": [{"message": a.get("message")} for a in data.get("alerts") or []],
        "rail": rail(data, zone, today),
        "schedule": {
            "today": [line.removeprefix("• ") for line in views.day_lines(
                today, assignments=data["assignments"], shifts=data["shifts"], events=data["events"],
                tz=config.timezone)],
            "tomorrow": [line.removeprefix("• ") for line in views.day_lines(
                tomorrow, assignments=data["assignments"], shifts=data["shifts"], events=data["events"],
                tz=config.timezone)],
        },
        "due": due,
        "overdue": [{"title": o.get("title"), "course": o.get("course"), "when": _when(o.get("due_at"), zone, today)}
                    for o in data.get("overdue") or []],
        "grades": [{"course": c.get("name"), "score": c.get("current_score"), "grade": c.get("current_grade")}
                   for c in data.get("courses") or [] if c.get("current_score") is not None],
        "reminders": [{"id": str(r["id"]), "text": r.get("text"), "when": _when(r.get("due_at"), zone, today),
                       "repeats": repeat_spoken(r["repeat"], r["due_at"].astimezone(zone)) if r.get("repeat") else ""}
                      for r in data.get("reminders") or []],
        "promises": [{"what": c.get("what"), "to": c.get("person"), "when": _when(c.get("due_at"), zone, today)}
                     for c in data.get("commitments") or []],
        "proposals": [{"id": str(p["id"]), "preview": p.get("preview"), "status": p.get("status")}
                      for p in extra["proposals"]],
        "panels": [{"skill": name, "title": panel.get("title") or name, "lines": [str(x) for x in panel.get("lines") or []]}
                   for name, panel in (data.get("panels") or {}).items()
                   if isinstance(panel, dict) and panel.get("lines") and not panel.get("error")],
        "learned": [{"key": f["key"], "value": f["value"], "source": f.get("source") or ""} for f in extra["learned"]],
        "loose": [{"id": str(r["id"]), "summary": r["summary"],
                   "when": f"{r['due_on']:%a %b} {r['due_on'].day}" if r.get("due_on") else ""} for r in extra["loose"]],
        "diary": [{"when": f"{d['occurred_at'].astimezone(zone):%A %b} {d['occurred_at'].astimezone(zone).day}",
                   "text": re.sub(r"^Diary, [^:]*: ", "", d.get("content") or "")} for d in extra["diary"]],
        "jobs": [{"name": j["name"], "status": j.get("last_status") or "never run",
                  "last": _when(j.get("last_run_at"), zone, today), "error": (j.get("last_error") or "")[:200],
                  "next": _when(upcoming.get(j["name"]), zone, today)} for j in extra["jobs"]],
        "trust": [{"action": t["action"], "target": t["target"], "state": "hard line" if t.get("hard_line") else t["state"],
                   "streak": t.get("clean_streak") or 0, "hard": bool(t.get("hard_line"))} for t in extra["trust"]],
        "usage": [{"lane": u.get("purpose"), "provider": u.get("provider"), "calls": int(u.get("calls") or 0),
                   "failures": int(u.get("failures") or 0)} for u in extra["usage"]],
        "system": {
            "database": bool(extra["database"]),
            "telegram": "bot" in state,
            "main": config.main_provider,
            "bulk": config.bulk_provider,
            "local": config.local_model if config.local_base_url and config.local_model else "",
            "voice": config.speak_provider + (f" ({config.piper_voice})" if config.piper_voice else ""),
            "quiet": f"{config.quiet_start_hour}:00–{config.quiet_end_hour}:{config.quiet_end_minute:02d}",
            "web_lookup": config.web_lookup,
            "skills": skills.names if skills is not None else [],
        },
    }


# -- routes -------------------------------------------------------------------------------

def install(app: FastAPI, state: dict, store, config) -> None:  # noqa: ANN001, C901
    """Mount /app and /api on the app. Off (404) without a DASHBOARD_TOKEN."""
    guesses = Guesses()

    def page(name: str) -> HTMLResponse:
        return HTMLResponse((UI / name).read_text(), headers=HEADERS)

    def signed_in(request: Request) -> bool:
        return enabled(config) and valid_session(config.dashboard_token, request.cookies.get(COOKIE), time.time())

    def refuse(request: Request, *, change: bool) -> Response | None:
        """None if this request may go ahead; else the answer that stops it."""
        if not enabled(config):
            return JSONResponse({"error": "the dashboard is off (set DASHBOARD_TOKEN)"}, status_code=404)
        if not signed_in(request):
            return JSONResponse({"error": "sign in"}, status_code=401)
        if change and request.headers.get("x-sloane") != "1":
            return JSONResponse({"error": "missing X-Sloane header"}, status_code=403)
        return None

    def owner() -> int:
        return int(config.telegram_chat_id or 0)

    @app.get("/app")
    async def app_page(request: Request) -> Response:
        if not enabled(config):
            return HTMLResponse("The dashboard is off. Set DASHBOARD_TOKEN in .env (DEPLOY section 7e).",
                                status_code=404, headers=HEADERS)
        if not signed_in(request):
            return RedirectResponse("/app/login", status_code=303)
        return page("index.html")

    @app.get("/app/login")
    async def login_page(request: Request) -> Response:
        if not enabled(config):
            return HTMLResponse("The dashboard is off.", status_code=404, headers=HEADERS)
        # Fixed strings only: nothing from the request is echoed into the page.
        said = ""
        if request.query_params.get("wrong"):
            said = '<p class="error" role="alert">That isn\'t the password.</p>'
        elif request.query_params.get("wait"):
            said = '<p class="error" role="alert">Too many tries. Wait ten minutes, then try again.</p>'
        html = (UI / "login.html").read_text().replace("<!--MESSAGE-->", said)
        return HTMLResponse(html, headers=HEADERS)

    @app.post("/app/login")
    async def login(request: Request) -> Response:
        if not enabled(config):
            return HTMLResponse("The dashboard is off.", status_code=404, headers=HEADERS)
        who = request.client.host if request.client else "?"
        now = time.time()
        if guesses.blocked(who, now):
            return RedirectResponse("/app/login?wait=1", status_code=303)
        raw = (await request.body())[:2048].decode(errors="replace")
        given = (parse_qs(raw).get("token") or [""])[0].strip()
        if not hmac.compare_digest(given.encode(), config.dashboard_token.encode()):
            guesses.wrong(who, now)
            await asyncio.sleep(1.0)
            return RedirectResponse("/app/login?wrong=1", status_code=303)
        guesses.right(who)
        response = RedirectResponse("/app", status_code=303)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, session_value(config.dashboard_token, now), max_age=SESSION_SECONDS,
                            httponly=True, samesite="strict", secure=secure, path="/")
        return response

    @app.post("/app/logout")
    async def logout(request: Request) -> Response:
        stop = refuse(request, change=True)
        if stop is not None:
            return stop
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/app/static/{name}")
    async def asset(name: str) -> Response:
        if name not in ASSETS:
            return Response(status_code=404)
        return Response((UI / name).read_bytes(), media_type=ASSETS[name],
                        headers={**HEADERS, "Cache-Control": "no-cache"})

    @app.get("/api/overview")
    async def api_overview(request: Request) -> Response:
        stop = refuse(request, change=False)
        if stop is not None:
            return stop
        body = await overview(store, config, state, datetime.now(ZoneInfo(config.timezone)))
        return JSONResponse(jsonable_encoder(body), headers={"Cache-Control": "no-store"})

    @app.get("/api/history")
    async def api_history(request: Request) -> Response:
        stop = refuse(request, change=False)
        if stop is not None:
            return stop
        zone = ZoneInfo(config.timezone)
        rows = await store.recent_messages(owner(), datetime.now(zone) - timedelta(days=3), 60) if owner() else []
        today = datetime.now(zone).date()
        return JSONResponse([{"from": "him" if r["direction"] == "in" else "her",
                              "text": r.get("body") or "", "when": _when(r.get("at"), zone, today),
                              "forwarded": r.get("kind") == "forward",
                              "outside": r.get("direction") == "out" and r.get("trusted") is False}
                             for r in rows], headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def api_chat(request: Request) -> Response:
        stop = refuse(request, change=True)
        if stop is not None:
            return stop
        try:
            text = str((await request.json()).get("text") or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            return JSONResponse({"error": "say something"}, status_code=400)
        if len(text) > MAX_MESSAGE:
            return JSONResponse({"error": f"keep it under {MAX_MESSAGE} characters"}, status_code=413)
        bot = state.get("responder")
        if bot is None:
            return JSONResponse({"error": "she isn't ready yet"}, status_code=503)
        # Logged first, like a Telegram message: it's his words, in the one conversation.
        await store.log_message(chat_id=owner(), direction="in", kind="text", body=text)
        outlet = WebOutlet(store, owner())

        async def run() -> None:
            try:
                await bot.respond(owner(), text, outlet, channel="dashboard")
            except Exception as exc:  # noqa: BLE001 - said on the page, never a hung spinner
                log.exception("dashboard message failed")
                outlet.emit({"t": "error", "text": f"That hit an error: {type(exc).__name__}."})
            finally:
                outlet.emit({"t": "done"})

        task = asyncio.get_running_loop().create_task(run())
        state.setdefault("chats", set()).add(task)
        task.add_done_callback(state["chats"].discard)

        async def events():  # noqa: ANN202
            while True:
                event = await outlet.queue.get()
                yield json.dumps(event) + "\n"
                if event["t"] == "done":
                    return

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def act(request: Request, doing):  # noqa: ANN001, ANN202
        stop = refuse(request, change=True)
        if stop is not None:
            return stop
        try:
            message, ok = await doing()
        except Exception as exc:  # noqa: BLE001
            log.exception("dashboard action failed")
            return JSONResponse({"ok": False, "message": f"That hit an error: {type(exc).__name__}."}, status_code=500)
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)

    @app.post("/api/reminders/{reminder_id}/cancel")
    async def cancel_reminder(reminder_id: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            if not _UUID.match(reminder_id):
                return "No such reminder.", False
            row = await store.cancel_reminder(reminder_id)
            if row is None:
                return "That one already went out.", False
            return (f"Stopped: {row['text']}. It won't repeat." if row.get("repeat") else f"Cancelled: {row['text']}."), True
        return await act(request, doing)

    @app.post("/api/proposals/{proposal_id}/{decision}")
    async def decide(proposal_id: str, decision: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            agency = getattr(state.get("responder"), "agency", None)
            if agency is None:
                return "Actions are off (no TELEGRAM_CHAT_ID).", False
            if not _UUID.match(proposal_id) or decision not in ("approve", "deny"):
                return "No such decision.", False
            outcome = await agency.decide(proposal_id, decision)
            return outcome.message, outcome.status not in ("stale", "ignored")
        return await act(request, doing)

    @app.post("/api/trust/revoke")
    async def revoke(request: Request) -> Response:
        async def doing():  # noqa: ANN202
            agency = getattr(state.get("responder"), "agency", None)
            if agency is None:
                return "Actions are off (no TELEGRAM_CHAT_ID).", False
            body = await request.json()
            action, target = str(body.get("action") or ""), str(body.get("target") or "")
            if not action or not target:
                return "Which one?", False
            return (await agency.revoke(action, target)).message, True
        return await act(request, doing)

    @app.post("/api/memory/forget")
    async def forget(request: Request) -> Response:
        async def doing():  # noqa: ANN202
            key = str((await request.json()).get("key") or "")
            if not key.startswith("learned."):
                return "Only what she learned can be forgotten here.", False
            row = await store.unpin_state(key)
            return (f"Forgotten: {row['value']}.", True) if row else ("That one's already forgotten.", False)
        return await act(request, doing)

    @app.post("/api/followups/{item_id}/close")
    async def close_loose_end(item_id: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            if not _UUID.match(item_id):
                return "No such loose end.", False
            row = await store.close_follow_up(item_id)
            return (f"Closed: {row['summary']}.", True) if row else ("That one's already closed.", False)
        return await act(request, doing)

    @app.post("/api/jobs/{name}/run")
    async def run_job(name: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            if "scheduler" not in state or not _JOB.match(name):
                return "No such job.", False
            result = await state["scheduler"].run(name)
            said = result.reply.speech if result.reply else ""
            return (f"{name}: {result.reason}" + (f" — {said}" if said else "")), bool(result.ran)
        return await act(request, doing)

    # -- the workshop (sloane/workshop.py) ---------------------------------------------------------

    def shop():  # noqa: ANN202
        return state.get("workshop")

    @app.get("/api/workshop")
    async def api_workshop(request: Request) -> Response:
        stop = refuse(request, change=False)
        if stop is not None:
            return stop
        workshop = shop()
        zone = ZoneInfo(config.timezone)
        today = datetime.now(zone).date()
        rows = await store.workshop_items(limit=80)
        items = [{
            "id": str(r["id"]), "title": r["title"], "request": r["request"], "origin": r["origin"],
            "kind": r["kind"], "status": r["status"], "plan": r.get("plan") or "", "summary": r.get("summary") or "",
            "files": list(r.get("files") or []), "checks": r.get("checks") or "", "pr_url": r.get("pr_url") or "",
            "error": r.get("error") or "", "feedback": r.get("feedback") or "",
            "when": _when(r.get("updated_at"), zone, today),
        } for r in rows]
        return JSONResponse({
            "building_on": bool(workshop is not None and workshop.ready),
            "upgrader": bool(workshop is not None and workshop.upgrader_installed()),
            "model": config.workshop_model, "nightly": config.workshop_nightly,
            "items": items,
        }, headers={"Cache-Control": "no-store"})

    @app.post("/api/workshop/ideas")
    async def workshop_idea(request: Request) -> Response:
        async def doing():  # noqa: ANN202
            workshop = shop()
            if workshop is None:
                return "The workshop isn't running.", False
            text = str((await request.json()).get("text") or "").strip()
            if not text or len(text) > 2000:
                return "Say what you want her to have (under 2,000 characters).", False
            row = await workshop.add_idea(text)
            return f"In the workshop: {row['title']}. She's planning it.", True
        return await act(request, doing)

    @app.post("/api/workshop/{item_id}/{verb}")
    async def workshop_verb(item_id: str, verb: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            workshop = shop()
            if workshop is None or not _UUID.match(item_id):
                return "No such item.", False
            try:
                body = await request.json()
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            if verb == "plan":
                row = await workshop.plan(item_id)
                return ("Planned." if row is not None and row.get("plan") else "She couldn't plan it just now."), \
                    row is not None
            if verb == "build":
                now = bool(body.get("now"))
                row = await workshop.queue(item_id, now=now)
                if row is None:
                    return "That one can't be built from where it is.", False
                if not workshop.ready:
                    return "Queued. Building needs the GitHub token on the box.", True
                return (f"Building {row['title']} now." if now else f"{row['title']} is queued for tonight."), True
            if verb == "accept":
                return await workshop.accept(item_id)
            if verb == "deny":
                return await workshop.deny(item_id, str(body.get("why") or ""))
            if verb == "undo":
                return await workshop.undo(item_id)
            if verb == "drop":
                row = await workshop.drop(item_id)
                return ("Dropped." if row is not None else "That one's already moved on."), row is not None
            return "No such action.", False
        return await act(request, doing)
