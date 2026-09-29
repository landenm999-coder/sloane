"""News: the top headlines, and the topics he follows.

    /news  ·  /news tech  ·  /news broncos
    /news follow formula 1  ·  /news unfollow formula 1  ·  /news topics
    "what's in the news?"  ·  "any news?"  ·  "what's the news on the broncos?"

Google News RSS for his edition (NEWS_REGION), no key. A headline is someone
else's words: every one goes through `ingest.safe_field`, an answer that reads
them is `tainted` (stored untrusted, a placeholder in CONVERSATION), and none
ever goes in FACTS. She reads headlines; she doesn't vouch for them.

A feed is fetched at most every 15 minutes. If Google News is down, the last
headlines are used for up to three hours (marked as such), and a failed fetch
isn't retried for five minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as clock
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote as urlquote

import httpx

from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

log = logging.getLogger(__name__)

SKILL = "news"
FRESH_SECONDS = 15 * 60
STALE_OK_SECONDS = 3 * 3600
RETRY_AFTER_SECONDS = 5 * 60
TIMEOUT_SECONDS = 6.0
MAX_BYTES = 1_500_000
MAX_TOPICS = 5
MAX_TOPIC = 40
SHOWN = 6
# Google's own sections, for the words he'd use for them.
SECTIONS = {
    "world": "WORLD", "national": "NATION", "us": "NATION", "business": "BUSINESS", "money": "BUSINESS",
    "tech": "TECHNOLOGY", "technology": "TECHNOLOGY", "science": "SCIENCE", "health": "HEALTH",
    "sports": "SPORTS", "sport": "SPORTS", "entertainment": "ENTERTAINMENT",
}

_ASK = re.compile(
    r"^\s*(?:hey\s+)?(?:(?:what'?s|whats|what\s+is)\s+(?:in\s+)?the\s+news|any\s+(?:big\s+)?news|(?:what'?s|whats|what\s+is)\s+"
    r"(?:happening|going\s+on)\s+in\s+the\s+world|(?:give\s+me|read\s+me|tell\s+me)\s+(?:the\s+)?(?:news|headlines)|"
    r"(?:the\s+)?(?:top\s+)?(?:news|headlines)(?:\s+today)?|what\s+are\s+the\s+headlines)"
    r"(?:\s+(?:on|about|in|for)\s+(?P<topic>[\w&'. -]{2,40}?))?(?:\s+today|\s+right\s+now)?\s*[?.!]*\s*$",
    re.I,
)


class NewsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Headline:
    title: str
    source: str
    url: str
    at: datetime | None


def parse_rss(raw: bytes) -> list[Headline]:
    """Google News RSS into headlines: one line each, sources apart, links http(s) only."""
    root = ET.fromstring(raw)
    out: list[Headline] = []
    for item in root.iter("item"):
        title = safe_field(item.findtext("title") or "", limit=200)
        source = safe_field(item.findtext("source") or "", limit=60)
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        link = (item.findtext("link") or "").strip()
        if not re.match(r"^https?://[^\s<>\"']+$", link):
            link = ""
        at = None
        try:
            at = parsedate_to_datetime(item.findtext("pubDate") or "")
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            at = None
        if title:
            out.append(Headline(title=title, source=source, url=link, at=at))
    return out


def ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return ""
    minutes = max(0, int((now - at).total_seconds() // 60))
    if minutes < 60:
        return f"{max(1, minutes)}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def topic_name(raw: str) -> str:
    """"The Broncos" -> "broncos": what's searched for, and how it's said back."""
    name = safe_field(re.sub(r"[^\w&'. -]", " ", raw), limit=MAX_TOPIC).strip(" .-").lower()
    return re.sub(r"^(?:the|my|our)\s+", "", name)


class News(Skill):
    name = SKILL
    help = ("`/news` · `/news <topic>` · `/news follow <topic>` — headlines (or ask \"what's in the news?\")",)
    commands = frozenset({"news"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self._cache: dict[str, tuple[float, list[Headline]]] = {}
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    # -- fetching ----------------------------------------------------------------------------

    def _url(self, topic: str) -> str:
        base = self.ctx.config.news_api_base.rstrip("/")
        region = (self.ctx.config.news_region or "US").strip().upper()[:2]
        edition = f"hl=en-{region}&gl={region}&ceid={region}:en"
        if not topic:
            return f"{base}/rss?{edition}"
        if topic in SECTIONS:
            return f"{base}/rss/headlines/section/topic/{SECTIONS[topic]}?{edition}"
        return f"{base}/rss/search?q={urlquote(topic)}&{edition}"

    async def headlines(self, topic: str = "") -> tuple[list[Headline], bool]:
        """(headlines, whether they're old). Raises NewsUnavailable."""
        hit = self._cache.get(topic)
        if hit and clock.monotonic() - hit[0] < FRESH_SECONDS:
            return hit[1], False
        async with self._lock:
            now = clock.monotonic()
            hit = self._cache.get(topic)
            if hit and now - hit[0] < FRESH_SECONDS:
                return hit[1], False
            if now >= self._retry_at:
                try:
                    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
                        response = await client.get(self._url(topic), headers={"User-Agent": "Mozilla/5.0 Sloane/1.0"})
                    if response.status_code != 200:
                        raise NewsUnavailable(f"Google News answered HTTP {response.status_code}")
                    if len(response.content) > MAX_BYTES:
                        raise NewsUnavailable("the feed was too big")
                    fresh = parse_rss(response.content)
                    self._cache[topic] = (now, fresh)
                    self._retry_at = 0.0
                    return fresh, False
                except (httpx.HTTPError, ET.ParseError, NewsUnavailable) as exc:
                    log.warning("news fetch failed: %s", exc)
                    self._retry_at = now + RETRY_AFTER_SECONDS
            if hit and now - hit[0] < STALE_OK_SECONDS:
                return hit[1], True
            raise NewsUnavailable("no headlines right now")

    async def topics(self) -> list[str]:
        raw = await self.ctx.store.get_skill_setting(SKILL, "topics")
        try:
            names = json.loads(raw) if raw else []
        except ValueError:
            names = []
        return [topic_name(n) for n in names if isinstance(n, str) and topic_name(n)][:MAX_TOPICS]

    # -- words -------------------------------------------------------------------------------

    async def read(self, topic: str = "") -> Answer:
        topic = topic_name(topic)
        try:
            items, old = await self.headlines(topic)
        except NewsUnavailable:
            return Answer("I can't reach the news right now. Try again in a few minutes.")
        if not items:
            return Answer(f"Nothing in the news about {topic} right now." if topic else "The news feed came back empty.")
        now = datetime.now(timezone.utc)
        first = items[0]
        said = f"{'On ' + topic + ', the' if topic else 'The'} top story{', from ' + first.source if first.source else ''}: {first.title}."
        if len(items) > 1:
            said += f" Also{', from ' + items[1].source if items[1].source else ''}: {items[1].title}."
        lines = [f"- {h.title}" + (f" ({h.source}{', ' + ago(h.at, now) if h.at else ''})" if h.source else "")
                 + (f" [link]({h.url})" if h.url else "") for h in items[:SHOWN]]
        if old:
            lines.append("\n(The latest I could get; the feed isn't answering right now.)")
        return Answer(said, "Headlines, as the outlets wrote them:\n" + "\n".join(lines), tainted=True)

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        low = rest.lower()
        verb, _, what = low.partition(" ")
        if verb in {"follow", "add"} and what:
            names = await self.topics()
            topic = topic_name(what)
            if not topic:
                return Answer("Follow what? Try /news follow formula 1.")
            if topic in names:
                return Answer(f"You already follow {topic}.")
            if len(names) >= MAX_TOPICS:
                return Answer(f"You follow {MAX_TOPICS} topics already. /news unfollow one first.")
            await self.ctx.store.set_skill_setting(SKILL, "topics", json.dumps(names + [topic]))
            return Answer(f"Following {topic}. It's on your news panel now.")
        if verb in {"unfollow", "drop", "remove"} and what:
            names = await self.topics()
            topic = topic_name(what)
            if topic not in names:
                return Answer(f"You don't follow {topic}.")
            await self.ctx.store.set_skill_setting(SKILL, "topics", json.dumps([n for n in names if n != topic]) or None)
            return Answer(f"Stopped following {topic}.")
        if low in {"topics", "following"}:
            names = await self.topics()
            return Answer(f"You follow {', '.join(names)}." if names else "You don't follow any topics. Try /news follow tech.")
        return await self.read(rest)

    async def match(self, text: str) -> Answer | None:
        m = _ASK.match(text)
        if not m:
            return None
        return await self.read(m.group("topic") or "")

    async def facts(self) -> list[str]:
        return []  # headlines are outside text: never FACTS

    async def panel(self) -> dict | None:
        now = datetime.now(timezone.utc)
        items: list[dict] = []
        old = False
        try:
            top, old = await self.headlines("")
            items += [{"title": h.title, "source": h.source, "url": h.url, "ago": ago(h.at, now), "topic": ""}
                      for h in top[:5]]
        except NewsUnavailable:
            pass
        for topic in (await self.topics())[:2]:
            try:
                found, stale = await self.headlines(topic)
            except NewsUnavailable:
                continue
            old = old or stale
            seen = {i["title"] for i in items}
            items += [{"title": h.title, "source": h.source, "url": h.url, "ago": ago(h.at, now), "topic": topic}
                      for h in found if h.title not in seen][:2]
        if not items:
            return {"title": "News", "lines": ["Can't reach the news right now."], "items": [], "down": True}
        return {"title": "News", "outside": True, "old": old, "items": items,
                "lines": [f"{i['title']} ({i['source']})" if i["source"] else i["title"] for i in items]}


def build(ctx: SkillContext) -> Skill:
    return News(ctx)
