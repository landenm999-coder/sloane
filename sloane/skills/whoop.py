"""Whoop: recovery, last night's sleep, today's strain, and his workouts.

    /whoop  ·  "how did I sleep?"  ·  "what's my recovery?"  ·  "how recovered am I?"
    "what's my strain?"

Read-only, from Whoop's developer API (v2). Off unless WHOOP_CLIENT_ID,
WHOOP_CLIENT_SECRET and WHOOP_REFRESH_TOKEN are set (scripts/whoop_auth.py
mints the token; DEPLOY 7j).

Whoop replaces the refresh token every time it's used, so the one in .env works
exactly once. After that she keeps the current one in WHOOP_TOKEN_FILE (on the
box's volume, chmod 600), written before it's relied on. The file remembers a
fingerprint of the .env token it grew from, so running whoop_auth.py again (a
new .env token) starts it over. Tokens never go in the database, a backup, a
log line or a reply.

Numbers from the API are structured values, so they may go in FACTS, but only
from the cache (fetched at most every 15 minutes): FACTS never waits on Whoop.
Today's HRV and resting heart rate are measured against his own average over
the recoveries before today (up to BASELINE_DAYS of them). Workouts Whoop
records are logged in the workouts skill's table, once each, with their strain
and heart rate. The heartbeat asks every quarter hour, so they arrive whether
or not the control room is open, and a new one is told to him once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time as clock
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from sloane.skills import Answer, Nudge, Skill, SkillContext

log = logging.getLogger(__name__)

FRESH_SECONDS = 15 * 60
STALE_OK_SECONDS = 6 * 3600
FACTS_FRESH_SECONDS = 2 * 3600
RETRY_AFTER_SECONDS = 5 * 60
TIMEOUT_SECONDS = 8.0
# Whoop sits behind Cloudflare, which turns Python's own user-agents away (403, "error code: 1010").
AGENT = "Mozilla/5.0 (compatible; Sloane/1.0)"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout read:profile"
BASELINE_DAYS = 24   # the recoveries before today that make his average (Whoop pages at 25)
BASELINE_MIN = 3     # fewer than this is not an average yet
STRAIN_MAX = 21.0

_ASK = re.compile(
    r"^\s*(?:hey\s+)?(?:how\s+(?:did|'d)\s+i\s+sleep|how'?d\s+i\s+sleep|how\s+was\s+my\s+sleep|"
    r"(?:what'?s|whats|what\s+is|how'?s|how\s+is)\s+my\s+(?:recovery|strain|hrv|sleep(?:\s+score)?|whoop)|"
    r"how\s+recovered\s+am\s+i|am\s+i\s+recovered|my\s+(?:recovery|whoop))"
    r"(?:\s+(?:today|last\s+night|this\s+morning))?\s*[?.!]*\s*$",
    re.I,
)


class WhoopUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Day:
    recovery: int | None
    hrv: float | None
    rhr: float | None
    sleep_minutes: int | None
    sleep_performance: int | None
    strain: float | None
    workouts: tuple[dict, ...] = ()
    hrv_avg: float | None = None
    rhr_avg: float | None = None
    avg_days: int = 0
    sleep_need_minutes: int | None = None


def zone(recovery: int | None) -> str:
    if recovery is None:
        return ""
    return "green" if recovery >= 67 else "yellow" if recovery >= 34 else "red"


def hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def said_hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    hours = f"{h} hour{'s' if h != 1 else ''}"
    return f"{hours} {m} minute{'s' if m != 1 else ''}" if m else hours


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first(body: dict) -> dict:
    records = (body or {}).get("records") or []
    return records[0] if records and isinstance(records[0], dict) else {}


def _when(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _scored(record: Any) -> dict:
    return (record.get("score") or {}) if isinstance(record, dict) and record.get("score_state") == "SCORED" else {}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if len(values) >= BASELINE_MIN else None


def parse_day(recovery: dict, cycle: dict, sleep: dict, workouts: dict) -> Day:
    """Whoop's four answers into the numbers she uses. Unscored parts are None.

    `recovery` is newest first: the first is today's, the rest (up to BASELINE_DAYS) are
    his baseline."""
    rec = _first(recovery)
    rec_score = _scored(rec)
    before = [_scored(r) for r in ((recovery or {}).get("records") or [])[1:BASELINE_DAYS + 1]]
    hrvs = [v for v in (_num(b.get("hrv_rmssd_milli")) for b in before) if v]
    rhrs = [v for v in (_num(b.get("resting_heart_rate")) for b in before) if v]
    cyc = _first(cycle)
    cyc_score = cyc.get("score") or {} if cyc.get("score_state") == "SCORED" else {}
    nights = [r for r in (sleep or {}).get("records") or [] if isinstance(r, dict) and not r.get("nap")]
    night = nights[0] if nights else {}
    sleep_score = night.get("score") or {} if night.get("score_state") == "SCORED" else {}
    stages = sleep_score.get("stage_summary") or {}
    in_bed, awake = _num(stages.get("total_in_bed_time_milli")), _num(stages.get("total_awake_time_milli"))
    asleep = round((in_bed - (awake or 0)) / 60000) if in_bed else None
    needed = sleep_score.get("sleep_needed") or {}
    need_parts = [_num(needed.get(k)) for k in ("baseline_milli", "need_from_sleep_debt_milli",
                                                 "need_from_recent_strain_milli", "need_from_recent_nap_milli")]
    need = round(sum(p for p in need_parts if p is not None) / 60000) if need_parts[0] else None
    moves = []
    for w in (workouts or {}).get("records") or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        start, end = _when(w.get("start")), _when(w.get("end"))
        if start is None or end is None or end <= start:
            continue
        score = w.get("score") or {}
        heart, strain, meters = (_num(score.get(k)) for k in ("average_heart_rate", "strain", "distance_meter"))
        moves.append({"id": str(w["id"])[:80], "sport": str(w.get("sport_name") or "workout")[:40], "start": start,
                      "minutes": min(1440, max(1, round((end - start).total_seconds() / 60))),
                      "meters": meters if meters and 0 < meters <= 500000 else None,
                      "strain": round(strain, 1) if strain is not None and 0 <= strain <= STRAIN_MAX else None,
                      "heart": round(heart) if heart and 20 <= heart <= 250 else None})
    recovery_score = _num(rec_score.get("recovery_score"))
    performance = _num(sleep_score.get("sleep_performance_percentage"))
    return Day(
        recovery=round(recovery_score) if recovery_score is not None else None,
        hrv=_num(rec_score.get("hrv_rmssd_milli")),
        rhr=_num(rec_score.get("resting_heart_rate")),
        sleep_minutes=asleep,
        sleep_performance=round(performance) if performance is not None else None,
        strain=_num(cyc_score.get("strain")),
        workouts=tuple(moves),
        hrv_avg=_mean(hrvs),
        rhr_avg=_mean(rhrs),
        avg_days=max(len(hrvs), len(rhrs)),
        sleep_need_minutes=need if need and 60 <= need <= 16 * 60 else None,
    )


def token_path(config) -> Path:  # noqa: ANN001
    if config.whoop_token_file:
        return Path(config.whoop_token_file)
    base = Path(config.embed_cache_dir) if config.embed_cache_dir else Path.home() / ".cache" / "sloane"
    return base / "whoop.json"


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class Tokens:
    """The current refresh token, rotated on every use, kept in a file that survives restarts."""

    def __init__(self, config) -> None:  # noqa: ANN001
        self.config = config
        self.path = token_path(config)
        self.seed = fingerprint(config.whoop_refresh_token)
        self.access = ""
        self.expires = 0.0
        self._lock = asyncio.Lock()

    def refresh_token(self) -> str:
        """The file's token if it grew from this .env token; the .env one otherwise (a fresh consent)."""
        try:
            saved = json.loads(self.path.read_text())
            if saved.get("seed") == self.seed and saved.get("refresh_token"):
                return str(saved["refresh_token"])
        except (OSError, ValueError, AttributeError):
            pass
        return self.config.whoop_refresh_token

    def save(self, refresh: str) -> None:
        """Atomically, 600, before the new token is relied on."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".whoop.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"seed": self.seed, "refresh_token": refresh, "saved_at": datetime.now().isoformat()}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def bearer(self, client: httpx.AsyncClient, *, renew: bool = False) -> str:
        async with self._lock:
            if self.access and not renew and clock.monotonic() < self.expires:
                return self.access
            response = await client.post(f"{self.config.whoop_api_base.rstrip('/')}/oauth/oauth2/token", data={
                "grant_type": "refresh_token", "refresh_token": self.refresh_token(),
                "client_id": self.config.whoop_client_id, "client_secret": self.config.whoop_client_secret,
                "scope": "offline"})
            if response.status_code != 200:
                raise WhoopUnavailable(f"Whoop refused the token refresh (HTTP {response.status_code}); "
                                       "run scripts/whoop_auth.py again")
            body = response.json()
            access, refresh = body.get("access_token"), body.get("refresh_token")
            if not access:
                raise WhoopUnavailable("Whoop's token answer had no access token")
            if refresh:
                try:
                    self.save(str(refresh))
                except OSError as exc:
                    # Kept in memory for this run; the next restart needs whoop_auth.py again.
                    log.error("whoop: couldn't save the new refresh token to %s: %s", self.path, exc)
                    self.config = self.config.model_copy(update={"whoop_refresh_token": str(refresh)})
                    self.seed = fingerprint(str(refresh))
            self.access = str(access)
            self.expires = clock.monotonic() + max(60, int(body.get("expires_in") or 3600) - 120)
            return self.access


class Whoop(Skill):
    name = "whoop"
    help = ("`/whoop` — recovery, sleep and strain (or ask \"how did I sleep?\")",)
    commands = frozenset({"whoop"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self.tokens = Tokens(ctx.config)
        self._cache: tuple[float, Day] | None = None
        self._retry_at = 0.0
        self._lock = asyncio.Lock()
        self._new: list[dict] = []   # workouts logged since the heartbeat last asked

    # -- fetching -----------------------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        url = f"{self.ctx.config.whoop_api_base.rstrip('/')}/developer/v2{path}"
        for attempt in (0, 1):
            token = await self.tokens.bearer(client, renew=attempt == 1)
            response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401 and attempt == 0:
                continue  # an access token Whoop has retired early: one fresh one, then give up
            if response.status_code != 200:
                raise WhoopUnavailable(f"Whoop answered HTTP {response.status_code} for {path}")
            return response.json()
        raise WhoopUnavailable("Whoop keeps refusing the token")

    async def day(self) -> tuple[Day, bool]:
        """(today's numbers, whether they're old). Raises WhoopUnavailable."""
        if self._cache and clock.monotonic() - self._cache[0] < FRESH_SECONDS:
            return self._cache[1], False
        async with self._lock:
            now = clock.monotonic()
            if self._cache and now - self._cache[0] < FRESH_SECONDS:
                return self._cache[1], False
            if now >= self._retry_at:
                try:
                    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": AGENT}) as client:
                        # One token first, so the four reads don't race to rotate it.
                        await self.tokens.bearer(client)
                        parts = await asyncio.gather(
                            self._get(client, "/recovery", {"limit": BASELINE_DAYS + 1}),
                            self._get(client, "/cycle", {"limit": 1}),
                            self._get(client, "/activity/sleep", {"limit": 3}),
                            self._get(client, "/activity/workout", {"limit": 10}))
                    fresh = parse_day(*parts)
                    self._cache = (now, fresh)
                    self._retry_at = 0.0
                    self._new.extend(await self._log_workouts(fresh))
                    return fresh, False
                except (httpx.HTTPError, ValueError, WhoopUnavailable) as exc:
                    log.warning("whoop fetch failed: %s", exc)
                    self._retry_at = now + RETRY_AFTER_SECONDS
            if self._cache and now - self._cache[0] < STALE_OK_SECONDS:
                return self._cache[1], True
            raise WhoopUnavailable("no Whoop numbers right now")

    async def _log_workouts(self, day: Day) -> list[dict]:
        """Into the workouts table, once each (its source id), with a rescore's new strain.
        The ones new to the table. Never costs the numbers."""
        if not day.workouts or not hasattr(self.ctx.store, "add_workout"):
            return []
        from sloane.skills.workouts import _kind

        zone_ = self.ctx.now().tzinfo
        new = []
        try:
            for w in day.workouts:
                sport = w["sport"].replace("-", " ").replace("_", " ").lower()
                kind = _kind(sport) or sport or "workout"
                on = w["start"].astimezone(zone_).date()
                row = await self.ctx.store.add_workout(
                    kind=kind, minutes=w["minutes"], distance_m=w["meters"], done_on=on, logged_at=self.ctx.now(),
                    source="whoop", source_id=w["id"], strain=w["strain"], heart_rate=w["heart"])
                if row is not None:
                    new.append({**w, "kind": kind, "on": on})
        except Exception:  # noqa: BLE001 - the workouts table may be missing; the numbers still count
            log.exception("whoop: couldn't log its workouts")
        return new

    # -- words -----------------------------------------------------------------------------------

    def _said(self, d: Day) -> tuple[str, list[str]]:
        bits, lines = [], []
        if d.recovery is not None:
            bits.append(f"Recovery is {d.recovery}%, {zone(d.recovery)}")
            extra = [f"HRV {round(d.hrv)} ms" + (f" (avg {round(d.hrv_avg)})" if d.hrv_avg else "")
                     if d.hrv is not None else "",
                     f"resting HR {round(d.rhr)}" + (f" (avg {round(d.rhr_avg)})" if d.rhr_avg else "")
                     if d.rhr is not None else ""]
            lines.append(f"- Recovery: {d.recovery}% ({zone(d.recovery)})" + "".join(f", {e}" for e in extra if e))
        if d.sleep_minutes is not None:
            bits.append(f"you slept {said_hm(d.sleep_minutes)}"
                        + (f", {d.sleep_performance}% of what you needed" if d.sleep_performance is not None else ""))
            lines.append(f"- Sleep: {hm(d.sleep_minutes)}" + (f", performance {d.sleep_performance}%"
                                                                 if d.sleep_performance is not None else ""))
        if d.strain is not None:
            lines.append(f"- Strain so far today: {d.strain:.1f}")
        if not bits:
            return (f"Strain so far today is {d.strain:.1f}. Whoop hasn't scored your recovery yet."
                    if d.strain is not None else "Whoop hasn't scored anything yet today."), lines
        speech = bits[0] + (f", and {bits[1]}" if len(bits) > 1 else "") + "."
        if d.strain is not None:
            speech += f" Strain so far is {d.strain:.1f}."
        return speech[0].upper() + speech[1:], lines

    async def report(self) -> Answer:
        try:
            d, old = await self.day()
        except WhoopUnavailable:
            return Answer("I can't reach Whoop right now. Try again in a few minutes.")
        speech, lines = self._said(d)
        if old:
            lines.append("\n(The latest I could get; Whoop isn't answering right now.)")
        return Answer(speech, "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        return await self.report()

    async def match(self, text: str) -> Answer | None:
        return await self.report() if _ASK.match(text) else None

    async def facts(self) -> list[str]:
        """Only what's cached and recent: FACTS never waits on Whoop."""
        if not self._cache or clock.monotonic() - self._cache[0] >= FACTS_FRESH_SECONDS:
            return []
        d = self._cache[1]
        parts = []
        if d.recovery is not None:
            parts.append(f"recovery {d.recovery}% ({zone(d.recovery)})")
        if d.hrv is not None:
            parts.append(f"HRV {round(d.hrv)} ms" + (f" (his average {round(d.hrv_avg)})" if d.hrv_avg else ""))
        if d.rhr is not None:
            parts.append(f"resting HR {round(d.rhr)}" + (f" (his average {round(d.rhr_avg)})" if d.rhr_avg else ""))
        if d.sleep_minutes is not None:
            parts.append(f"last night's sleep {hm(d.sleep_minutes)}"
                         + (f" ({d.sleep_performance}% of need)" if d.sleep_performance is not None else ""))
        if d.strain is not None:
            parts.append(f"strain so far {d.strain:.1f}")
        return [f"- WHOOP today: {', '.join(parts)}"] if parts else []

    async def panel(self) -> dict | None:
        try:
            d, old = await self.day()
        except WhoopUnavailable:
            return {"title": "Whoop", "lines": ["Can't reach Whoop right now."], "down": True}
        _, lines = self._said(d)
        return {"title": "Whoop", "lines": [line.removeprefix("- ") for line in lines], "old": old,
                "recovery": d.recovery, "zone": zone(d.recovery), "hrv": None if d.hrv is None else round(d.hrv),
                "rhr": None if d.rhr is None else round(d.rhr), "sleep_minutes": d.sleep_minutes,
                "sleep_performance": d.sleep_performance, "strain": None if d.strain is None else round(d.strain, 1),
                "strain_max": STRAIN_MAX, "sleep_need_minutes": d.sleep_need_minutes,
                "hrv_avg": None if d.hrv_avg is None else round(d.hrv_avg, 1),
                "rhr_avg": None if d.rhr_avg is None else round(d.rhr_avg, 1), "avg_days": d.avg_days}

    async def nudges(self) -> list[Nudge]:
        """The heartbeat's quarter hour: fetch (so his workouts arrive with the page closed), and
        tell him once about a workout Whoop logged today."""
        try:
            await self.day()
        except WhoopUnavailable:
            return []
        today = self.ctx.today()
        told, self._new = [w for w in self._new if w["on"] == today], []
        out = []
        for w in told:
            bits = [f"{w['minutes']} min"]
            if w["strain"] is not None:
                bits.append(f"strain {w['strain']:.1f}")
            if w["heart"]:
                bits.append(f"avg HR {w['heart']}")
            out.append(Nudge(f"whoop:workout:{w['id']}",
                             f"🏃 Whoop logged your {w['kind']}: {', '.join(bits)}. It's in your workouts."))
        return out


def build(ctx: SkillContext) -> Skill | None:
    c = ctx.config
    if not (c.whoop_client_id and c.whoop_client_secret and c.whoop_refresh_token):
        return None
    return Whoop(ctx)

