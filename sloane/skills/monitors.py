"""Monitors: things he asked her to watch for, told to him once, when they happen.

    "let me know when NVDA goes above 150"  ·  "alert me when bitcoin drops below 60k"
    "let me know when https://store.example/airpods says in stock"  ·  "tell me if https://x.example changes"
    "let me know when the iPhone 18 release date is announced"  ·  "keep an eye on the Broncos game time"
    /monitor NVDA above 150  ·  /monitor <page> for <words>  ·  /monitor <anything>
    /monitors                what she's watching  ·  /monitor stop 2 (or words from it)

Three kinds, from his words, by rule (no model):

* price: a ticker or a name the markets skill knows, and a line. "Above" or "below"
  comes from his verb ("drops below", "climbs past"), or, for "hits 70k", from where
  it is now. The markets skill's delayed quotes, every 15 minutes.
* page: a web address, and either words to wait for ("for in stock") or any change.
  Its visible text, hourly. Only public addresses: nothing on the box or its network
  (every redirect checked too), http(s) on the usual ports, at most 2 MB.
* question: anything else, as a question for the web ("has this happened yet?"),
  answered by the same lookup she uses in a reply, every MONITOR_QUESTION_HOURS; at
  most MONITOR_QUESTIONS at once, since each check is a lookup on his plan.

The `monitors` job checks them in waking hours (quiet hours and a focus block hold
it) and says each once, then it's over. A monitor that hasn't happened in
MONITOR_DAYS ends, and she says so. What a page or the web says is outside text: a
message built from it is sent tainted, and FACTS carries only his words and her
numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext
from sloane.skills.markets import Markets, MarketsUnavailable, level, money, name, symbol_for, ticker

log = logging.getLogger(__name__)

PRICE_MINUTES = 15
PAGE_MINUTES = 60
MAX_PAGE_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0
QUESTIONS_PER_RUN = 2
AGENT = "Mozilla/5.0 (compatible; Sloane/1.0)"

_ASK = re.compile(
    r"^\s*(?:hey\s+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:(?:let|tell)\s+me\s+know\s+(?:when|if|once)|(?:alert|notify|ping|text|message)\s+me\s+(?:when|if|once|the\s+moment)"
    r"|keep\s+an\s+eye\s+on|watch\s+for)\s+(?P<what>.{3,})$", re.I | re.S)
_STOP = re.compile(r"^(?:stop|end|cancel|delete|remove|off)\s+(?P<which>.+)$", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_PHRASE = re.compile(r"\b(?:for|says|shows|contains|mentions|has|reads)\s+[\"“'‘]?(?P<p>[^\"”'’]{2,80}?)[\"”'’]?\s*[.!?]*$", re.I)
_PRICE = re.compile(
    r"^(?P<what>[\w&^.=$' -]{1,30}?)(?:'s)?\s+(?:stock\s+|price\s+|shares\s+)?"
    r"(?:(?P<verb>is|goes|gets|drops|falls|dips|sinks|rises|climbs|jumps|hits|reaches|crosses|trades|breaks|passes)\s+)?"
    r"(?P<dir>above|over|below|under|past|to|at|back\s+above|back\s+below)?\s*\$?(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k)?"
    r"\s*(?:dollars|bucks|usd)?\s*[.!?]*$", re.I)
_DOWN = {"drops", "falls", "dips", "sinks"}
_UP = {"rises", "climbs", "jumps"}
_TAGS = re.compile(r"<(script|style|noscript|template)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class Refused(ValueError):
    """An address she won't fetch."""


def public(host: str, port: int | None) -> None:
    """Raise Refused unless every address `host` resolves to is on the public internet."""
    if port not in (None, 80, 443):
        raise Refused("only the usual web ports")
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise Refused(f"can't find {host}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise Refused("that address is private")


def visible(page: str) -> str:
    """A page's text as a person reads it: no scripts, styles or tags, entities decoded."""
    text = _TAG.sub(" ", _TAGS.sub(" ", page))
    return _WS.sub(" ", html.unescape(text)).strip()


def parse(text: str) -> dict:
    """What he wants watched: {"kind": "page"|"price"|"question", ...}. By rule, never a model."""
    said = text.strip().strip(" .!?")
    url = _URL.search(said)
    if url:
        address = url.group(0).rstrip(".,;:!?)")
        after = said[url.end():].strip()
        phrase = _PHRASE.search(after) or _PHRASE.search(said[: url.start()].strip())
        return {"kind": "page", "url": address, "phrase": phrase.group("p").strip() if phrase else None}
    m = _PRICE.match(said)
    if m:
        what = m.group("what").strip()
        symbol = symbol_for(what) or (ticker(what) if what.isupper() or what.startswith("$") else None)
        if symbol:
            n = float(m.group("n").replace(",", "")) * (1000 if m.group("k") else 1)
            verb, way = (m.group("verb") or "").lower(), (m.group("dir") or "").lower()
            direction = ("above" if way.endswith(("above", "over", "past")) or verb in _UP
                         else "below" if way.endswith(("below", "under")) or verb in _DOWN else None)
            return {"kind": "price", "symbol": symbol, "line": n, "direction": direction}
    return {"kind": "question", "question": said}


class Monitors(Skill):
    name = "monitors"
    help = ("\"let me know when NVDA goes above 150\" — she watches and tells you once; `/monitors`; `/monitor stop 2`",)
    commands = frozenset({"monitor", "monitors"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self.market = Markets(ctx)

    # -- setting one up ----------------------------------------------------------------------

    async def add(self, text: str) -> Answer:
        said = safe_field(text.strip(" .!?"), limit=200)
        if len(said) < 3:
            return Answer("Watch for what? Try: let me know when NVDA goes above 150.")
        want = parse(said)
        if want["kind"] == "page":
            parts = urlsplit(want["url"])
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return Answer("I can only watch http or https pages.")
            try:
                await asyncio.to_thread(public, parts.hostname, parts.port)
            except Refused as exc:
                return Answer(f"I won't watch that address: {exc}.")
            try:
                page = await self._fetch(want["url"])
            except (Refused, httpx.HTTPError) as exc:
                return Answer(f"I couldn't open that page ({safe_field(str(exc), limit=80) or type(exc).__name__}). "
                              "Check the address?")
            phrase = want["phrase"]
            if phrase and phrase.lower() in page.lower():
                return Answer(f"It already says \"{phrase}\". Nothing to wait for.")
            await self.ctx.store.add_monitor(said=said, kind="page", spec={"url": want["url"], "phrase": phrase},
                                             every_minutes=PAGE_MINUTES, last_value=self._print(page),
                                             created_at=self.ctx.now())
            host = parts.hostname.removeprefix("www.")
            return Answer(f"Watching {host} for \"{phrase}\"." if phrase else f"Watching {host} for any change.",
                          "I check it hourly and tell you once.")
        if want["kind"] == "price":
            symbol = want["symbol"]
            try:
                got, _ = await self.market.quotes([symbol])
            except MarketsUnavailable:
                got = []
            if not got:
                return Answer(f"I can't get a price for {symbol} right now, so I can't watch it yet.")
            q = got[0]
            direction = want["direction"] or ("above" if want["line"] > q.price else "below")
            if (direction == "above" and q.price >= want["line"]) or (direction == "below" and q.price <= want["line"]):
                return Answer(f"{name(symbol)} is already {direction} that: {level(q)}.")
            await self.ctx.store.add_monitor(said=said, kind="price", every_minutes=PRICE_MINUTES,
                                             spec={"symbol": symbol, "line": want["line"], "direction": direction},
                                             last_value=str(q.price), created_at=self.ctx.now())
            return Answer(f"I'll tell you when {name(symbol)} goes {direction} {self._line(symbol, want['line'])}. "
                          f"It's {level(q)} now.")
        if self.ctx.router is None or not self.ctx.config.web_lookup:
            return Answer("I can watch prices and pages, but questions for the web need web lookups, which are off.")
        open_questions = [m for m in await self.ctx.store.open_monitors() if m["kind"] == "question"]
        if len(open_questions) >= self.ctx.config.monitor_questions:
            return Answer(f"I'm already watching {len(open_questions)} things on the web, the most at once. "
                          "/monitors lists them; stop one first.")
        hours = max(1, self.ctx.config.monitor_question_hours)
        await self.ctx.store.add_monitor(said=said, kind="question", spec={"question": want["question"]},
                                         every_minutes=min(1440, hours * 60), created_at=self.ctx.now())
        return Answer(f"I'll keep an eye out: {said}. I'll tell you when it happens.",
                      f"I check the web every {hours} hours for {self.ctx.config.monitor_days} days.")

    def _line(self, symbol: str, line: float) -> str:
        if symbol == "^TNX":
            return f"{line:.2f}%"
        return f"{line:,.0f}" if symbol.startswith("^") else money(line)

    @staticmethod
    def _print(page: str) -> str:
        return hashlib.sha256(page.encode()).hexdigest()[:32]

    async def _fetch(self, url: str) -> str:
        """A public page's visible text, following at most MAX_REDIRECTS redirects, each checked."""
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False,
                                     headers={"User-Agent": AGENT}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise Refused("not a web address")
                await asyncio.to_thread(public, parts.hostname, parts.port)
                async with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise Refused(f"the page answered {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    return visible(body.decode(response.encoding or "utf-8", errors="replace"))
            raise Refused("too many redirects")

    # -- the checks (the monitors job) ----------------------------------------------------------

    async def check(self) -> list[tuple[str, str, bool]]:
        """(monitor id, what to say, tainted) for each that happened or ran out. The job says
        them and then calls `done`, so one that couldn't be said is said next time."""
        now = self.ctx.now()
        out: list[tuple[str, str, bool]] = []
        questions = 0
        for m in await self.ctx.store.open_monitors():
            mid = str(m["id"])
            if now - m["created_at"] >= timedelta(days=self.ctx.config.monitor_days):
                out.append((mid, f"I stopped watching for this after {self.ctx.config.monitor_days} days: {m['said']}. "
                                 "It never happened.", False))
                continue
            if m["checked_at"] is not None and now - m["checked_at"] < timedelta(minutes=m["every_minutes"]):
                continue
            if m["kind"] == "question":
                if questions >= QUESTIONS_PER_RUN:
                    continue
                questions += 1
            try:
                said = await self._check_one(m, now)
            except Exception as exc:  # noqa: BLE001 - one broken monitor never stops the others
                reason = safe_field(str(exc), limit=160) or type(exc).__name__
                log.warning("monitor %s: %s", mid, reason)
                await self.ctx.store.checked_monitor(mid, at=now, error=reason)
                continue
            if said is not None:
                out.append((mid, *said))
        return out

    async def _check_one(self, m: Any, now: datetime) -> tuple[str, bool] | None:
        spec, mid = m["spec"] or {}, str(m["id"])
        if m["kind"] == "price":
            symbol = spec["symbol"]
            got, old = await self.market.quotes([symbol])
            if not got or old:
                await self.ctx.store.checked_monitor(mid, at=now, error="no fresh price")
                return None
            q = got[0]
            await self.ctx.store.checked_monitor(mid, at=now, value=str(q.price))
            crossed = q.price >= spec["line"] if spec["direction"] == "above" else q.price <= spec["line"]
            if not crossed:
                return None
            return (f"📈 {name(symbol)} is {spec['direction']} {self._line(symbol, spec['line'])}: {level(q)} now"
                    + (f" ({'+' if q.change >= 0 else '−'}{abs(q.change):.1f}% today)." if q.change is not None else "."),
                    False)
        if m["kind"] == "page":
            page = await self._fetch(spec["url"])
            fingerprint = self._print(page)
            host = (urlsplit(spec["url"]).hostname or "").removeprefix("www.")
            await self.ctx.store.checked_monitor(mid, at=now, value=fingerprint)
            if spec.get("phrase"):
                if spec["phrase"].lower() in page.lower():
                    return f"👀 {host} now says \"{spec['phrase']}\": {spec['url']}", False
                return None
            if fingerprint != m["last_value"]:
                return f"👀 {host} changed: {spec['url']}", False
            return None
        # A question for the web: the lookup she uses in replies, asked for a yes or no.
        found = await self.ctx.router.research(
            f"Today is {now:%A %B} {now.day}, {now.year}. Has this happened yet, or is it true now: "
            f"\"{spec['question']}\"? Answer on the first line with only YES or NO. On the second line, the "
            "evidence in one sentence, with the source's name and URL.")
        lines = [line.strip() for line in found.strip().splitlines() if line.strip()]
        verdict = lines[0].upper().strip("*.: ") if lines else ""
        await self.ctx.store.checked_monitor(mid, at=now, value=verdict[:8])
        if not verdict.startswith("YES"):
            return None
        evidence = safe_field(lines[1], limit=300) if len(lines) > 1 else ""
        return f"👀 It happened: {m['said']}" + (f"\n\n{evidence}" if evidence else ""), True

    async def done(self, monitor_id: str, text: str) -> None:
        """Said: over, with what she said."""
        why = "expired" if text.startswith("I stopped watching") else "fired"
        await self.ctx.store.end_monitor(monitor_id, at=self.ctx.now(), why=why, fired_text=text[:1000])

    # -- words ----------------------------------------------------------------------------------

    def _state(self, m: Any) -> str:
        spec = m["spec"] or {}
        if m["kind"] == "price" and m["last_value"]:
            try:
                now_at = float(m["last_value"])
                return f"now {self._line(spec['symbol'], now_at)}"
            except ValueError:
                return ""
        if m["checked_at"] is None:
            return "not checked yet"
        local = m["checked_at"].astimezone(self.ctx.now().tzinfo)
        day = dates.spoken(local.date(), self.ctx.today())
        return f"checked {'' if day == 'today' else day + ' '}{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"

    async def listing(self) -> Answer:
        rows = await self.ctx.store.open_monitors()
        if not rows:
            return Answer("I'm not watching for anything.", "Say \"let me know when NVDA goes above 150\", or a page, "
                                                            "or anything you're waiting on.")
        lines = [f"{n}. {m['said']} ({self._state(m)})" for n, m in enumerate(rows, 1)]
        return Answer(f"I'm watching for {len(rows)} thing{'s' if len(rows) != 1 else ''}.",
                      "\n".join(lines) + "\n\n`/monitor stop <number>` stops one.")

    async def stop(self, which: str) -> Answer:
        rows = await self.ctx.store.open_monitors()
        pick = None
        if which.strip().isdigit() and 1 <= int(which) <= len(rows):
            pick = rows[int(which) - 1]
        else:
            words = set(re.findall(r"\w+", which.lower()))
            scored = [(len(words & set(re.findall(r"\w+", m["said"].lower()))), m) for m in rows]
            scored = [s for s in scored if s[0]]
            if scored:
                pick = max(scored, key=lambda s: s[0])[1]
        if pick is None:
            return Answer("I'm not watching for that. /monitors lists what I am.")
        await self.ctx.store.end_monitor(str(pick["id"]), at=self.ctx.now(), why="stopped")
        return Answer(f"Stopped watching for this: {pick['said']}.")

    async def command(self, name_: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name_ == "monitors" or not rest or rest.lower() in {"list", "all"}:
            return await self.listing()
        stopping = _STOP.match(rest)
        if stopping:
            return await self.stop(stopping.group("which"))
        return await self.add(rest)

    async def match(self, text: str) -> Answer | None:
        m = _ASK.match(text.strip())
        return await self.add(m.group("what")) if m else None

    # -- FACTS and the panel ------------------------------------------------------------------------

    async def facts(self) -> list[str]:
        rows = await self.ctx.store.open_monitors()
        if not rows:
            return []
        each = "; ".join(f"'{safe_field(m['said'], limit=90)}' ({self._state(m)})" for m in rows[:8])
        return [f"- MONITORS he set (she tells him once when it happens): {each}"]

    async def panel(self) -> dict | None:
        rows = await self.ctx.store.recent_monitors(self.ctx.now() - timedelta(days=3))
        if not rows:
            return None
        open_rows = [m for m in rows if m["ended_at"] is None]
        return {"title": "Monitors", "lines": [f"{m['said']} ({self._state(m)})" for m in open_rows] or ["Nothing open."],
                "items": [{"n": (open_rows.index(m) + 1) if m in open_rows else None, "said": m["said"], "kind": m["kind"],
                           "state": self._state(m) if m["ended_at"] is None else
                           {"fired": "Happened", "stopped": "Stopped", "expired": "Ran out"}.get(m["ended_why"], "Over"),
                           "open": m["ended_at"] is None, "fired": m["ended_why"] == "fired"}
                          for m in reversed(rows)]}


def build(ctx: SkillContext) -> Skill:
    return Monitors(ctx)
