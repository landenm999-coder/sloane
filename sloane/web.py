"""The control room: /app, one page where he talks to her and runs everything she does.

    Talk     the same conversation as Telegram -- she's one person on both.
             Commands, skill phrases, reminders, actions: all of it works here,
             through the same `Bot.respond`, and the exchange lands in the same
             log, so CONVERSATION, recall and the nightly learner see it too.
             Typed, or spoken into the microphone (transcribed like a Telegram
             voice note); the page can read her replies aloud in the browser.
    Today    the day as a timeline (classes, shifts, reminders, focus blocks,
             what's due) with a line at now, what's waiting on him (approvals,
             alerts, failed jobs, overdue work, workshop builds), and panels:
             weather, countdowns, the workshop, habits, focus, grades, the
             week, what she learned today, colleges, and every skill's own.
    The orb  what she's doing right now (GET /api/activity): idle, thinking,
             building, needs you; and on the page, listening and speaking.
    Memory   what she's learned about him (forget any), loose ends (close
             them), and her diary of the last week.
    Workshop what she's building on herself (sloane/workshop.py).
    Engine   jobs (run one now), trust (take one back), the models and what
             they've cost, her settings as they stand, and which panels show
             (kept in dashboard_prefs, so they follow him between devices).

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
          "manifest.json": "application/manifest+json", "icon-180.png": "image/png", "icon-192.png": "image/png",
          "icon-512.png": "image/png"}
# Onest, served from here (the page loads nothing from outside). OFL: fonts/LICENSE.txt.
FONTS = frozenset({"onest-400.woff2", "onest-500.woff2", "onest-600.woff2"})
# What the widgets' buttons may run through /api/do: adding, ticking, starting and stopping --
# each one a command he could type, handled the same way. Nothing that reaches outside (no /sync,
# /brief, /inbox), and nothing about her permissions (/trust, /revoke).
BUTTONS = frozenset({"did", "habit", "list", "focus", "watch", "unwatch", "workout", "spent", "budget",
                     "countdown", "birthday", "client", "news", "done", "remind", "remember", "bank",
                     "research", "monitor"})
MAX_BUTTON = 300
_BUTTON = re.compile(r"^/([a-z]+)(?:\s|$)", re.I)
# What a browser's recorder makes (Chrome and Firefox: WebM or Ogg; Safari: MP4),
# as the extension the transcriber reads the format from.
AUDIO = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a",
         "audio/mpeg": "mp3", "audio/wav": "wav"}
MAX_AUDIO = 8 * 1024 * 1024
# What the page asks her to say at once: a sentence or two of her speech.
MAX_SPOKEN = 600

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "media-src 'self' blob:; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; "
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


def _focus_blocks(panels: dict, zone: ZoneInfo, day: date) -> list[dict]:
    """A focus session running now, for the timeline. (The study plan's stretches stay in its panel.)"""
    blocks: list[dict] = []

    def block(title: str, sub: str, start_iso: str, end_iso: str) -> None:
        try:
            start, end = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
        except (TypeError, ValueError):
            return
        if start.astimezone(zone).date() != day:
            return
        first = _minutes(start, zone)
        last = _minutes(end, zone) if end.astimezone(zone).date() == day else 24 * 60
        blocks.append({"kind": "focus", "title": title, "sub": sub,
                       "time": f"{_clock(start, zone)}–{_clock(end, zone)}", "start": first, "end": max(last, first),
                       "all_day": False})

    focus = panels.get("focus") if isinstance(panels.get("focus"), dict) else {}
    running = focus.get("running")
    if isinstance(running, dict):
        block(f"Focus: {running.get('what') or 'focus'}", "running now", running.get("started_at"), running.get("ends_at"))
    return blocks


def week(data: dict, zone: ZoneInfo, today: date) -> list[dict]:
    """Monday to Sunday, and how packed each day is: its shifts, events and due work."""
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    counts = {d: 0 for d in days}

    def local(moment: Any) -> date | None:
        return moment.astimezone(zone).date() if isinstance(moment, datetime) else None

    for s in data.get("shifts") or []:
        on = local(s.get("starts_at"))
        if on in counts and not s.get("cancelled"):
            counts[on] += 1
    for e in data.get("events") or []:
        first, last = local(e.get("starts_at")), local(e.get("ends_at"))
        if first is None:
            continue
        if last is None or last < first:
            last = first
        elif e.get("all_day") and last > first:
            last -= timedelta(days=1)  # an all-day event ends at the next midnight
        for d in days:
            if first <= d <= last:
                counts[d] += 1
    for a in data.get("assignments") or []:
        on = local(a.get("due_at"))
        if on in counts:
            counts[on] += 1
    return [{"date": d.isoformat(), "day": f"{d:%a}", "count": counts[d],
             "level": 0 if not counts[d] else 1 if counts[d] <= 2 else 2 if counts[d] <= 4 else 3,
             "today": d == today} for d in days]


def work(shifts: list[dict], zone: ZoneInfo, now: datetime) -> dict | None:
    """The next shift (or the one he's on), and his hours this week and last."""
    live = [s for s in shifts or [] if isinstance(s.get("starts_at"), datetime) and isinstance(s.get("ends_at"), datetime)
            and not s.get("cancelled")]
    if not live:
        return None
    monday = now.astimezone(zone).date() - timedelta(days=now.astimezone(zone).weekday())

    def hours(first: date, *, until: datetime | None = None) -> float:
        total = 0.0
        for s in live:
            if first <= s["starts_at"].astimezone(zone).date() < first + timedelta(days=7):
                end = s["ends_at"] if until is None else min(s["ends_at"], until)
                total += max(0.0, (end - s["starts_at"]).total_seconds() / 3600)
        return round(total, 1)

    current = next((s for s in live if s["starts_at"] <= now < s["ends_at"]), None)
    upcoming = next((s for s in sorted(live, key=lambda s: s["starts_at"]) if s["starts_at"] > now), None)

    def shift(s: dict | None) -> dict | None:
        return None if s is None else {"starts_at": s["starts_at"], "ends_at": s["ends_at"],
                                       "when": _when(s["starts_at"], zone, now.astimezone(zone).date())}

    return {"now": shift(current), "next": shift(upcoming), "week_hours": hours(monday),
            "done_hours": hours(monday, until=now), "last_week_hours": hours(monday - timedelta(days=7))}


def agenda(data: dict, tz: str, day: date) -> dict:
    """One day for the page's timeline: what's on his calendar, his shifts, reminders, a focus session running.

    His life, not school's: deadlines are in the School panel, and her study plan's suggested
    stretches in Tonight's plan (/today on Telegram still lists both).
    """
    from sloane import views

    zone = ZoneInfo(tz)
    items, clashes = views.day_items(day, assignments=[], shifts=data.get("shifts") or [],
                                     events=data.get("events") or [], tz=tz)
    for r in data.get("reminders") or []:
        moment = r.get("due_at")
        if isinstance(moment, datetime) and moment.astimezone(zone).date() == day:
            minute = _minutes(moment, zone)
            items.append({"kind": "reminder", "title": str(r.get("text") or ""), "sub": "", "time": _clock(moment, zone),
                          "start": minute, "end": minute, "all_day": False, "id": str(r["id"]) if r.get("id") else None})
    items.extend(_focus_blocks(data.get("panels") or {}, zone, day))
    items.sort(key=lambda i: (not i["all_day"], i["start"], i["kind"] != "shift"))
    for item in items:
        item["title"] = item["title"][:160]
        item["sub"] = item["sub"][:80]
    return {"items": items, "clashes": clashes}


async def overview(store, config, state: dict, now: datetime) -> dict:  # noqa: ANN001
    """Everything the page shows, in one read. A failed part is named, not fatal."""
    from sloane import dashboard, views
    from sloane.reminders import repeat_spoken

    zone = ZoneInfo(config.timezone)
    now = now.astimezone(zone)
    today = now.date()
    monday = today - timedelta(days=today.weekday())
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
        # This week for the heat strip; last week to next week for the work panel.
        "week_shifts": store.shifts_between(monday - timedelta(days=7), monday + timedelta(days=13)),
        "week_events": store.events_between(monday, monday + timedelta(days=6)),
        "week_due": store.assignments_due(monday, monday + timedelta(days=6)),
        "used": store.usage_today(),
        "prefs": store.dashboard_prefs(),
        "emails": store.recent_emails(12),
    }
    settled = await asyncio.gather(*reads.values(), return_exceptions=True)
    extra: dict[str, Any] = {}
    for name, result in zip(reads, settled):
        if isinstance(result, BaseException):
            log.warning("control room read %s failed: %s", name, result)
            data["unreadable"].append(name)
            extra[name] = {"database": False, "used": {}, "prefs": None}.get(name, [])
        else:
            extra[name] = result

    upcoming = state["scheduler"].next_runs() if "scheduler" in state else {}
    tomorrow = today + timedelta(days=1)
    due = []
    for a in data.get("assignments") or []:
        due.append({"title": a.get("title"), "course": a.get("course"),
                    "when": _when(a.get("due_at"), zone, today), "at": a.get("due_at")})
    this_week = [s for s in extra["week_shifts"] if monday <= s["starts_at"].astimezone(zone).date() <= monday + timedelta(days=6)]
    used = extra["used"] or {}
    started = state.get("started_at")
    return {
        "now": {"clock": _clock(now, zone), "date": f"{now:%A, %B} {now.day}", "minutes": _minutes(now, zone),
                "tz": config.timezone},
        "name": config.address_as or "Landen",
        "unreadable": data["unreadable"],
        "alerts": [{"message": a.get("message")} for a in data.get("alerts") or []],
        # Anything broken, and any job whose last run failed. A failed job isn't an alert until
        # the watchdog's hour of grace, but the page shouldn't say "all normal".
        "health": {"broken": len(data.get("alerts") or []) + len(data["unreadable"]),
                   "failed_jobs": [j["name"] for j in extra["jobs"] if j.get("last_status") == "failed"]},
        "agenda": {"today": agenda(data, config.timezone, today), "tomorrow": agenda(data, config.timezone, tomorrow)},
        "schedule": {
            "today": [line.removeprefix("• ") for line in views.day_lines(
                today, assignments=data["assignments"], shifts=data["shifts"], events=data["events"],
                tz=config.timezone)],
            "tomorrow": [line.removeprefix("• ") for line in views.day_lines(
                tomorrow, assignments=data["assignments"], shifts=data["shifts"], events=data["events"],
                tz=config.timezone)],
        },
        "due": due,
        # Still to hand in this week, from now to Sunday night.
        "due_week": sum(1 for a in extra["week_due"] if isinstance(a.get("due_at"), datetime) and a["due_at"] >= now),
        "week": week({"shifts": this_week, "events": extra["week_events"], "assignments": extra["week_due"]}, zone, today),
        "work": work(extra["week_shifts"], zone, now),
        "engine": {"calls_today": sum(used.values()), "job_calls": used.get("job", 0),
                   "job_budget": config.daily_job_budget, "bulk_calls": used.get("bulk", 0),
                   "bulk_budget": config.daily_bulk_budget,
                   "up_since": datetime.fromtimestamp(started, zone) if started else None},
        "overdue": [{"title": o.get("title"), "course": o.get("course"), "when": _when(o.get("due_at"), zone, today)}
                    for o in data.get("overdue") or []],
        "grades": [{"course": c.get("name"), "score": c.get("current_score"), "grade": c.get("current_grade")}
                   for c in data.get("courses") or [] if c.get("current_score") is not None],
        "reminders": [{"id": str(r["id"]), "text": r.get("text"), "when": _when(r.get("due_at"), zone, today),
                       "at": r.get("due_at"),
                       "repeats": repeat_spoken(r["repeat"], r["due_at"].astimezone(zone)) if r.get("repeat") else ""}
                      for r in data.get("reminders") or []],
        "promises": [{"what": c.get("what"), "to": c.get("person"), "when": _when(c.get("due_at"), zone, today)}
                     for c in data.get("commitments") or []],
        "inbox": inbox(extra["emails"], config, zone, now),
        "proposals": [{"id": str(p["id"]), "preview": p.get("preview"), "status": p.get("status")}
                      for p in extra["proposals"]],
        # Each skill's panel: its lines (what /tv and Telegram show) and, beside them, the
        # structured fields the page draws from. The page falls back to the lines.
        "panels": [{**{k: v for k, v in panel.items() if k not in ("title", "lines", "error")},
                    "skill": name, "title": panel.get("title") or name, "lines": [str(x) for x in panel.get("lines") or []]}
                   for name, panel in (data.get("panels") or {}).items()
                   if isinstance(panel, dict) and panel.get("lines") and not panel.get("error")],
        "prefs": prefs(extra["prefs"]),
        "learned": [{"key": f["key"], "value": f["value"], "source": f.get("source") or "",
                     "today": isinstance(f.get("updated_at"), datetime) and f["updated_at"].astimezone(zone).date() == today}
                    for f in extra["learned"]],
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
            "quick": config.quick_provider.strip(),
            "local": config.local_model if config.local_base_url and config.local_model else "",
            "voice": config.speak_provider + (f" ({config.piper_voice})" if config.piper_voice else ""),
            "quiet": f"{config.quiet_start_hour}:00–{config.quiet_end_hour}:{config.quiet_end_minute:02d}",
            "web_lookup": config.web_lookup,
            "skills": skills.names if skills is not None else [],
        },
        "talk": {
            # The microphone shows only when a voice note can become words.
            "hears": bool(getattr(state.get("responder"), "hears", False)),
            # Read aloud in the browser's own voice: British when she is.
            "lang": "en-GB" if "en_GB" in (config.piper_voice or "") else "en-US",
        },
    }


def inbox(rows: list, config, zone: ZoneInfo, now: datetime) -> dict:  # noqa: ANN001
    """What her inbox triage kept from the last three days, newest first. Senders and subjects are
    other people's words: flattened to a line each (ingest.safe_field) and shown as text."""
    from sloane.ingest import safe_field

    since = now - timedelta(days=3)
    items = []
    for r in rows:
        at = r.get("received_at") or r.get("triaged_at")
        if r.get("category") not in INBOX_SHOWN or not isinstance(at, datetime) or at < since:
            continue
        thread = str(r.get("thread_id") or "")
        items.append({"from": safe_field(r.get("sender_name") or r.get("sender") or "someone", limit=60),
                      "subject": safe_field(r.get("subject") or "(no subject)", limit=140),
                      "category": r["category"], "when": _when(at, zone, now.date()),
                      "draft": r.get("proposal_status") or "",
                      # Gmail's own id for the thread (hex), so the row opens it there; nothing else passes.
                      "thread": thread if _THREAD.match(thread) else ""})
    return {"ready": bool(config.gmail_refresh_token), "items": items[:8],
            "waiting": sum(1 for i in items if i["category"] in ("urgent", "reply"))}


INBOX_SHOWN = ("urgent", "reply", "fyi")
_THREAD = re.compile(r"^[0-9a-f]{8,32}$")


# -- the panels ------------------------------------------------------------------------------

# Every panel, in the grid's order, and whether each is on until he says. His life first; school
# is one small panel (deadlines, grades), with grades, the week's heat and colleges apart, off.
# A panel with nothing in it yet still shows, saying how to fill it (webui/app.js EMPTY), so the
# grid is never bare. Any skill not named here comes after these, on, drawn from its lines.
PANELS: dict[str, bool] = {
    "weather": True, "whoop": True, "workouts": True, "markets": True, "inbox": True, "habits": True,
    "news": True, "focus": True, "money": True, "portfolio": True, "research": True, "monitors": True,
    "countdowns": True, "lists": True, "birthdays": True,
    "clients": True, "school": True, "workshop": True, "learned": True,
    "grades": False, "week": False, "colleges": False, "cards": False, "deca": False, "engine": False,
    "work": False, "plan": False, "memory": False, "bank": False,
}
PHONE_PANELS = ("whoop", "markets", "habits")
PHONE_COUNT = 3
MAX_PANELS = 40
_PANEL = re.compile(r"^[a-z_]{1,40}$")


def prefs(row: Any) -> dict:
    """His panel choices, and the defaults for everything he hasn't chosen."""
    phone = [p for p in (row["phone"] if row else []) or []][:PHONE_COUNT]
    return {"shown": list(row["shown"]) if row else [], "hidden": list(row["hidden"]) if row else [],
            "phone": phone or list(PHONE_PANELS), "defaults": PANELS, "phone_count": PHONE_COUNT}


def clean_prefs(body: Any) -> tuple[dict | None, str]:
    """(shown, hidden, phone) from the page, or None and why not."""
    if not isinstance(body, dict):
        return None, "Send the panels as JSON."
    out: dict[str, list[str]] = {}
    for key in ("shown", "hidden", "phone"):
        values = body.get(key) or []
        if not isinstance(values, list) or len(values) > MAX_PANELS:
            return None, f"{key} should be a list of panel names."
        names: list[str] = []
        for value in values:
            if not isinstance(value, str) or not _PANEL.match(value):
                return None, f"{str(value)[:40]!r} isn't a panel."
            if value not in names:
                names.append(value)
        out[key] = names
    if set(out["shown"]) & set(out["hidden"]):
        return None, "A panel can't be both on and off."
    if len(out["phone"]) > PHONE_COUNT:
        return None, f"The phone shows {PHONE_COUNT} panels."
    return out, ""


# -- what she's doing right now: the orb -------------------------------------------------------

# The workshop's five steps, as the orb's progress arc counts them.
STEPS = ("plan", "code", "test", "ci", "ready")
STEP_WORDS = {"plan": "planning", "code": "building", "test": "testing", "ci": "waiting on CI", "ready": "ready"}
# Jobs that only tick (every minute, every quarter hour): never "what she's doing".
TICKS = frozenset({"reminders", "heartbeat", "watchdog"})
DOING = {
    "entity_sync": "Syncing Canvas and the calendar", "morning_brief": "Writing the morning brief",
    "pre_shift": "Getting you ready for work", "post_shift": "Wrapping up your shift", "wrap": "Writing the night wrap",
    "reflection": "Looking back on the day", "inbox": "Reading your inbox", "learn": "Learning from today",
    "backup": "Backing up", "weekly_review": "Writing the weekly review", "think": "Thinking things over",
    "workshop": "In the workshop",
}
NEXT = {
    "entity_sync": "Next check: Canvas", "morning_brief": "Next: the morning brief", "pre_shift": "Next: the pre-shift brief",
    "post_shift": "Next: the post-shift check-in", "wrap": "Next: the night wrap", "reflection": "Next: reflection",
    "inbox": "Next check: the inbox", "learn": "Next: nightly learning", "backup": "Next: the backup",
    "weekly_review": "Next: the weekly review", "think": "Next: a think", "workshop": "Next: the workshop night shift",
}
JOB_NAMES = {
    "entity_sync": "The Canvas and calendar sync", "morning_brief": "The morning brief", "pre_shift": "The pre-shift brief",
    "post_shift": "The post-shift check-in", "wrap": "The night wrap", "reflection": "Reflection",
    "reminders": "Reminders", "heartbeat": "The heartbeat", "inbox": "Inbox triage", "learn": "Nightly learning",
    "backup": "The backup", "watchdog": "The watchdog", "weekly_review": "The weekly review", "think": "Thinking",
    "workshop": "The workshop night shift", "bank_sync": "The bank sync", "monitors": "The monitors",
}
# Workshop statuses the orb looks at.
MOVING = ("ready", "building", "deploying", "accepted", "idea", "planned")


def _said(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(n=count)


async def activity(store, state: dict, config, now: datetime) -> dict:  # noqa: ANN001, C901
    """{state, label, detail, step, steps}: what she's doing now, for the orb. Cheap: polled every 5 s.

    Needs you (a build to accept, an approval, something broken) beats building, which beats a
    job running, which beats idle. The page puts its own states on top: listening, speaking,
    and thinking while a message of his is in flight.
    """
    zone = ZoneInfo(config.timezone)
    now = now.astimezone(zone)
    reads = {"proposals": store.open_proposals(), "alerts": store.open_alerts(), "jobs": store.jobs(),
             "workshop": store.workshop_items(MOVING, limit=40)}
    settled = await asyncio.gather(*reads.values(), return_exceptions=True)
    got: dict[str, list] = {}
    for name, result in zip(reads, settled):
        if isinstance(result, BaseException):
            log.warning("activity read %s failed: %s", name, result)
            got[name] = []
        else:
            got[name] = list(result)
    items = got["workshop"]
    ready = [r for r in items if r["status"] == "ready"]
    failed = [j for j in got["jobs"] if j.get("last_status") == "failed"]
    base = {"step": None, "steps": len(STEPS), "ready": len(ready),
            "waiting": len(ready) + len(got["proposals"]) + len(got["alerts"]) + len(failed)}

    needs = []
    if ready:
        needs.append(_said(len(ready), "A Workshop change is ready to accept", "{n} Workshop changes are ready to accept"))
    if got["proposals"]:
        needs.append(_said(len(got["proposals"]), "An approval is waiting on you", "{n} approvals are waiting on you"))
    for alert in got["alerts"]:
        needs.append(str(alert.get("message") or "Something is broken")[:120])
    for job in failed:
        needs.append(f"{JOB_NAMES.get(job['name'], job['name'].replace('_', ' ').capitalize())} failed")
    if needs:
        more = f" (and {len(needs) - 1} more)" if len(needs) > 1 else ""
        return {**base, "state": "needs", "label": "Needs you", "detail": needs[0] + more,
                "step": len(STEPS) if ready else None}

    phase = getattr(state.get("workshop"), "phase", None) or {}

    def step_of(row: dict) -> str | None:
        if row["status"] in ("deploying", "accepted"):
            return "ready"
        if row["status"] == "building":
            return phase.get(str(row["id"])) or ("ci" if row.get("commit_sha") else "code")
        return "plan" if phase.get(str(row["id"])) == "plan" else None

    for row in sorted(items, key=lambda r: MOVING.index(r["status"])):
        at = step_of(row)
        if at is None:
            continue
        word = "going live" if row["status"] in ("deploying", "accepted") else STEP_WORDS[at]
        title = str(row.get("title") or "a change")
        title = title if len(title) <= 60 else title[:59] + "…"
        return {**base, "state": "building", "label": "Building", "detail": f"Workshop: “{title}”, {word}",
                "step": STEPS.index(at) + 1}

    scheduler = state.get("scheduler")
    running = [n for n in getattr(scheduler, "running", {}) if n not in TICKS]
    if running:
        name = running[0]
        return {**base, "state": "thinking", "label": "Thinking",
                "detail": DOING.get(name, name.replace("_", " ").capitalize())}

    upcoming = []
    if scheduler is not None:
        try:
            upcoming = [(when, name) for name, when in scheduler.next_runs().items()
                        if when is not None and name not in TICKS]
        except Exception:  # noqa: BLE001 - the orb is decoration; never a 500
            log.exception("activity: next runs unreadable")
    if not upcoming:
        return {**base, "state": "idle", "label": "Idle", "detail": "Nothing running"}
    when, name = min(upcoming)
    local = when.astimezone(zone)
    day = "" if local.date() == now.date() else (" tomorrow" if local.date() == now.date() + timedelta(days=1)
                                                  else f" {local:%a}")
    return {**base, "state": "idle", "label": "Idle",
            "detail": f"{NEXT.get(name, 'Next: ' + name.replace('_', ' '))}{day} at {_clock(local, zone)}"}


# -- routes -------------------------------------------------------------------------------

def install(app: FastAPI, state: dict, store, config) -> None:  # noqa: ANN001, C901
    """Mount /app and /api on the app. Off (404) without a DASHBOARD_TOKEN."""
    guesses = Guesses()
    # Since when she's been up: the Engine panel's uptime.
    state.setdefault("started_at", time.time())

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

    @app.get("/")
    async def root() -> Response:
        """The address on its own is the control room (which sends a stranger to its door)."""
        return RedirectResponse("/app", status_code=303)

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

    @app.get("/app/static/fonts/{name}")
    async def font(name: str) -> Response:
        if name not in FONTS:
            return Response(status_code=404)
        return Response((UI / "fonts" / name).read_bytes(), media_type="font/woff2",
                        headers={**HEADERS, "Cache-Control": "public, max-age=604800"})

    @app.get("/api/overview")
    async def api_overview(request: Request) -> Response:
        stop = refuse(request, change=False)
        if stop is not None:
            return stop
        body = await overview(store, config, state, datetime.now(ZoneInfo(config.timezone)))
        return JSONResponse(jsonable_encoder(body), headers={"Cache-Control": "no-store"})

    @app.get("/api/activity")
    async def api_activity(request: Request) -> Response:
        """What she's doing right now, for the orb. Polled every 5 s while the page is open."""
        stop = refuse(request, change=False)
        if stop is not None:
            return stop
        body = await activity(store, state, config, datetime.now(ZoneInfo(config.timezone)))
        return JSONResponse(jsonable_encoder(body), headers={"Cache-Control": "no-store"})

    @app.post("/api/prefs")
    async def api_prefs(request: Request) -> Response:
        """Which panels show, and the phone's three. Kept server-side so every device has them."""
        async def doing():  # noqa: ANN202
            try:
                body = await request.json()
            except ValueError:
                body = None
            chosen, why = clean_prefs(body)
            if chosen is None:
                return why, False
            await store.set_dashboard_prefs(**chosen)
            return "Panels saved.", True
        return await act(request, doing)

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
                              "forwarded": r.get("kind") == "forward", "voice": r.get("kind") == "voice",
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
        return converse(bot, text)

    @app.post("/api/voice")
    async def api_voice(request: Request) -> Response:
        """A voice note from the page's microphone: words by the Telegram voice path's
        transcriber, then the same conversation as a typed message. The audio is
        never stored or logged; the transcript is, like a Telegram voice note's."""
        stop = refuse(request, change=True)
        if stop is not None:
            return stop
        bot = state.get("responder")
        if bot is None:
            return JSONResponse({"error": "she isn't ready yet"}, status_code=503)
        if not getattr(bot, "hears", False):
            return JSONResponse({"error": "voice needs GROQ_API_KEY, or a local Whisper (LOCAL_MODELS.md)"},
                                status_code=400)
        kind = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if kind not in AUDIO:
            return JSONResponse({"error": "that isn't audio I can read"}, status_code=415)
        audio = bytearray()
        async for chunk in request.stream():
            audio += chunk
            if len(audio) > MAX_AUDIO:
                return JSONResponse({"error": "keep voice notes under two minutes"}, status_code=413)
        if not audio:
            return JSONResponse({"error": "nothing was recorded"}, status_code=400)
        try:
            text = (await bot.transcribe(bytes(audio), f"note.{AUDIO[kind]}")).strip()
        except Exception as exc:  # noqa: BLE001 - a failed transcription is said, never a hung spinner
            log.warning("dashboard voice note not transcribed: %s", exc)
            return JSONResponse({"error": "I couldn't make that out. Try again, or type it."}, status_code=502)
        if not text:
            return JSONResponse({"error": "That came back empty. Try again, a little closer?"}, status_code=422)
        text = text[:MAX_MESSAGE]
        await store.log_message(chat_id=owner(), direction="in", kind="voice", body=text)
        # Spoken: he's waiting to hear her, so her fast lane may answer.
        return converse(bot, text, heard=True, quick=True)

    @app.post("/api/speak")
    async def api_speak(request: Request) -> Response:
        """Her voice for a sentence the page shows (Piper or Groq, as on Telegram): WAV.

        The page asks sentence by sentence as her reply streams in, so she starts
        talking before she's finished writing. Accounted as "talk", apart from the
        Telegram voice notes DAILY_SPEAK_BUDGET rations."""
        stop = refuse(request, change=True)
        if stop is not None:
            return stop
        router = state.get("router")
        if router is None:
            return JSONResponse({"error": "her voice isn't ready yet"}, status_code=503)
        try:
            text = str((await request.json()).get("text") or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            return JSONResponse({"error": "nothing to say"}, status_code=400)
        if len(text) > MAX_SPOKEN:
            return JSONResponse({"error": f"keep it under {MAX_SPOKEN} characters"}, status_code=413)
        from sloane.router import NoProviderAvailable

        try:
            audio = await router.speak(text, purpose="talk")
        except NoProviderAvailable as exc:
            log.warning("no voice for the control room: %s", exc)
            return JSONResponse({"error": "her voice is unavailable"}, status_code=503)
        return Response(audio.wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    def converse(bot, text: str, *, heard: bool = False, quick: bool = False) -> StreamingResponse:  # noqa: ANN001
        """Her answer to one message of his (already logged), streamed to the page as NDJSON."""
        outlet = WebOutlet(store, owner())
        if heard:
            outlet.emit({"t": "heard", "text": text})

        async def run() -> None:
            try:
                await bot.respond(owner(), text, outlet, channel="dashboard", quick=quick)
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

    @app.post("/api/do")
    async def do_command(request: Request) -> Response:
        """A widget's button: one of his own commands (BUTTONS), run as if he typed it. His click is
        the ask (invariant 9); no model, and nothing added to the conversation. Her answer is the toast."""
        async def doing():  # noqa: ANN202
            try:
                command = str((await request.json()).get("command") or "").strip()
            except (ValueError, AttributeError):
                command = ""
            found = _BUTTON.match(command)
            if not found or "\n" in command or len(command) > MAX_BUTTON or found.group(1).lower() not in BUTTONS:
                return "That isn't something a button here does.", False
            bot = state.get("responder")
            if bot is None:
                return "She isn't ready yet.", False
            reply = await bot.run_command(command)
            if reply is None:
                return "Nothing answers that.", False
            return reply.speech, True
        return await act(request, doing)

    @app.post("/api/jobs/{name}/run")
    async def run_job(name: str, request: Request) -> Response:
        async def doing():  # noqa: ANN202
            if "scheduler" not in state or not _JOB.match(name):
                return "No such job.", False
            result = await state["scheduler"].run(name)
            said = result.reply.speech if result.reply else ""
            # Said the way a person would: "Inbox triage ran." / "Thinking didn't run: he's at work."
            label = JOB_NAMES.get(name, name.replace("_", " ").capitalize())
            why = "" if result.reason in ("", "ok") else f": {result.reason}"
            text = f"{label} ran{why}." if result.ran else f"{label} didn't run{why or ''}."
            return (text + (f" {said}" if said else "")), bool(result.ran)
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
