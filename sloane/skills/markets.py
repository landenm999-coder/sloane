"""Markets: the tickers he watches, and how they're doing today.

    /watch AAPL  ·  /watch btc, nvda  ·  /unwatch AAPL  ·  /markets
    "how's the market?"  ·  "how are my stocks doing?"  ·  "what's bitcoin at?"
    "how's Tesla doing today?"

Delayed quotes from Yahoo's chart API (stocks, funds, indexes, crypto; no key),
with CoinGecko for crypto when Yahoo won't answer. With nothing on his list she
watches the broad market: the S&P 500, the Nasdaq and Bitcoin. The control room
also shows the market at a glance (OVERVIEW: the big indexes, the VIX, the
10-year yield, gold, oil, Bitcoin and Ether), each with its day's line.

Prices are structured numbers from an API, so they may go in FACTS, but only
from the cache: FACTS never waits on a quote. A quote is fetched at most every
five minutes; if every source is down, the last one is used for up to three
hours and marked as old, and after a failure nothing is retried for five
minutes, so an outage costs one slow answer, not all of them.

She reports prices. She never trades, and never says what to buy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

import httpx

from sloane.skills import Answer, Skill, SkillContext

log = logging.getLogger(__name__)

FRESH_SECONDS = 5 * 60
STALE_OK_SECONDS = 3 * 3600
FACTS_FRESH_SECONDS = 30 * 60
RETRY_AFTER_SECONDS = 5 * 60
TIMEOUT_SECONDS = 5.0
MAX_WATCHED = 12
DEFAULTS = ("^GSPC", "^IXIC", "BTC-USD")
# Most telling first: a narrow widget shows the first few.
OVERVIEW = ("^GSPC", "^IXIC", "^DJI", "BTC-USD", "^TNX", "^VIX", "GC=F", "CL=F", "^RUT", "ETH-USD")
SPARK_POINTS = 96   # a day of five-minute closes, and then some
AGENT = "Mozilla/5.0 (X11; Linux aarch64) Sloane/1.0"

ALIASES = {
    "btc": "BTC-USD", "bitcoin": "BTC-USD", "eth": "ETH-USD", "ether": "ETH-USD", "ethereum": "ETH-USD",
    "sol": "SOL-USD", "solana": "SOL-USD", "doge": "DOGE-USD", "dogecoin": "DOGE-USD", "xrp": "XRP-USD",
    "ada": "ADA-USD", "cardano": "ADA-USD", "ltc": "LTC-USD", "litecoin": "LTC-USD",
    "s&p": "^GSPC", "s&p 500": "^GSPC", "the s&p": "^GSPC", "the s&p 500": "^GSPC", "sp500": "^GSPC", "spx": "^GSPC",
    "nasdaq": "^IXIC", "the nasdaq": "^IXIC", "dow": "^DJI", "the dow": "^DJI", "dow jones": "^DJI",
    "russell": "^RUT", "vix": "^VIX", "gold": "GC=F", "oil": "CL=F", "crude": "CL=F",
    "10 year": "^TNX", "10-year": "^TNX", "the 10 year": "^TNX", "the 10-year": "^TNX", "treasuries": "^TNX",
    "tnx": "^TNX",
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "microsoft": "MSFT", "amazon": "AMZN", "google": "GOOGL",
    "alphabet": "GOOGL", "meta": "META", "facebook": "META", "netflix": "NFLX", "amd": "AMD", "disney": "DIS",
    "nike": "NKE", "costco": "COST", "walmart": "WMT", "spotify": "SPOT", "coinbase": "COIN", "palantir": "PLTR",
}
NAMES = {
    "^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow", "^RUT": "Russell 2000", "^VIX": "VIX",
    "^TNX": "10-yr yield",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ether", "SOL-USD": "Solana", "DOGE-USD": "Dogecoin", "XRP-USD": "XRP",
    "ADA-USD": "Cardano", "LTC-USD": "Litecoin", "GC=F": "Gold", "CL=F": "Oil",
    "AAPL": "Apple", "TSLA": "Tesla", "NVDA": "Nvidia", "MSFT": "Microsoft", "AMZN": "Amazon", "GOOGL": "Google",
    "META": "Meta", "NFLX": "Netflix", "DIS": "Disney", "NKE": "Nike", "COST": "Costco", "WMT": "Walmart",
    "SPOT": "Spotify", "COIN": "Coinbase", "PLTR": "Palantir",
}
# CoinGecko's ids, for the crypto fallback.
COINS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana", "DOGE-USD": "dogecoin",
         "XRP-USD": "ripple", "ADA-USD": "cardano", "LTC-USD": "litecoin"}
INDEXES = frozenset({"^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX"})
YIELDS = frozenset({"^TNX"})   # a percentage, not a price

_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.=^-]{0,14}$")
_MARKET = re.compile(
    r"^\s*(?:hey\s+)?(?:how'?s|how\s+is|how\s+are|how'?re)\s+(?:the\s+)?(?:stock\s+)?(?:markets?|stocks|my\s+stocks|"
    r"my\s+watchlist|crypto|my\s+crypto)(?:\s+doing)?(?:\s+today|\s+right\s+now|\s+this\s+morning)?\s*[?.!]*\s*$",
    re.I,
)
_PRICE = re.compile(
    r"^\s*(?:(?:what'?s|whats|what\s+is|where'?s|where\s+is)\s+(?P<a>[\w&^.=\- ]{1,24}?)\s+(?:at|trading\s+at|sitting\s+at)"
    r"|(?:what'?s|whats|what\s+is)\s+(?:the\s+)?(?:price\s+of|stock\s+price\s+(?:of|for))\s+(?P<b>[\w&^.=\- ]{1,24}?)"
    r"|(?:how'?s|how\s+is)\s+(?P<c>[\w&^.=\- ]{1,24}?)\s+(?:stock\s+)?doing"
    r"|(?:price\s+of|quote\s+(?:for|on))\s+(?P<d>[\w&^.=\- ]{1,24}?))"
    r"(?:\s+(?:stock|shares))?(?:\s+today|\s+right\s+now)?\s*[?.!]*\s*$",
    re.I,
)


class MarketsUnavailable(RuntimeError):
    pass


class UnknownSymbol(ValueError):
    pass


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    previous: float | None
    currency: str
    at: datetime | None
    spark: tuple[float, ...] = ()

    @property
    def change(self) -> float | None:
        if not self.previous:
            return None
        return (self.price - self.previous) / self.previous * 100


def symbol_for(said: str, watched: list[str] | None = None) -> str | None:
    """"bitcoin" -> BTC-USD, "the s&p" -> ^GSPC, "nvda" -> NVDA (when he means a ticker). None if not one."""
    words = re.sub(r"\s+", " ", said.strip().lower()).removesuffix(" stock").removesuffix("'s")
    if words in ALIASES:
        return ALIASES[words]
    raw = said.strip().upper().lstrip("$")
    if watched and raw in watched:
        return raw
    if _SYMBOL.match(raw) and (said.strip().isupper() or said.strip().startswith("$")):
        return raw
    return None


def ticker(said: str) -> str | None:
    """What he typed after /watch: an alias, or anything shaped like a ticker."""
    words = re.sub(r"\s+", " ", said.strip().lower())
    if words in ALIASES:
        return ALIASES[words]
    raw = said.strip().upper().lstrip("$")
    return raw if _SYMBOL.match(raw) else None


def name(symbol: str) -> str:
    return NAMES.get(symbol, symbol)


def money(value: float, currency: str = "USD") -> str:
    sign = "$" if currency == "USD" else ""
    if value >= 1000:
        text = f"{value:,.0f}" if value >= 10000 else f"{value:,.2f}"
    elif value >= 1:
        text = f"{value:.2f}"
    else:
        text = f"{value:.4f}"
    return f"{sign}{text}" + ("" if sign or currency == "USD" else f" {currency}")


def level(q: Quote) -> str:
    """How a price is said: an index is points, a yield a percentage, everything else money."""
    if q.symbol in YIELDS:
        return f"{q.price:.2f}%"
    if q.symbol in INDEXES:
        return f"{q.price:,.0f}" if q.price >= 1000 else f"{q.price:.2f}"
    return money(q.price, q.currency)


def pct(change: float | None) -> str:
    if change is None:
        return ""
    return f"{'+' if change >= 0 else '−'}{abs(change):.2f}%"


def moved(q: Quote) -> str:
    """For speech: "up 0.4%", "down 1.2%", "flat"."""
    c = q.change
    if c is None:
        return "with no change yet"
    if abs(c) < 0.05:
        return "flat"
    return f"{'up' if c > 0 else 'down'} {abs(c):.1f}%"


def parse_chart(body: dict, symbol: str) -> Quote:
    chart = body.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise UnknownSymbol(symbol)
    result = chart["result"][0]
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if not isinstance(price, (int, float)):
        raise UnknownSymbol(symbol)
    previous = meta.get("previousClose") or meta.get("chartPreviousClose")
    closes: list = []
    try:
        closes = (result.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    except (AttributeError, IndexError, TypeError):
        closes = []
    at = meta.get("regularMarketTime")
    return Quote(
        symbol=symbol, price=float(price),
        previous=float(previous) if isinstance(previous, (int, float)) and previous else None,
        currency=str(meta.get("currency") or "USD")[:4],
        at=datetime.fromtimestamp(at, timezone.utc) if isinstance(at, (int, float)) else None,
        spark=tuple(float(c) for c in closes if isinstance(c, (int, float)))[-SPARK_POINTS:],
    )


class Markets(Skill):
    name = "markets"
    help = (
        "`/watch AAPL` · `/unwatch AAPL` · `/markets` — your watchlist's prices (or ask \"how's the market?\")",
    )
    commands = frozenset({"watch", "unwatch", "markets", "stocks"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    # -- fetching ----------------------------------------------------------------------

    async def _chart(self, client: httpx.AsyncClient, symbol: str) -> Quote:
        base = self.ctx.config.markets_api_base.rstrip("/")
        response = await client.get(f"{base}/v8/finance/chart/{urlquote(symbol, safe='')}",
                                    params={"range": "1d", "interval": "5m"}, headers={"User-Agent": AGENT})
        if response.status_code == 404:
            raise UnknownSymbol(symbol)
        if response.status_code != 200:
            raise MarketsUnavailable(f"Yahoo answered HTTP {response.status_code}")
        return parse_chart(response.json(), symbol)

    async def _coins(self, client: httpx.AsyncClient, symbols: list[str]) -> dict[str, Quote]:
        ids = {COINS[s]: s for s in symbols if s in COINS}
        if not ids:
            return {}
        base = self.ctx.config.markets_crypto_base.rstrip("/")
        response = await client.get(f"{base}/simple/price", params={
            "ids": ",".join(ids), "vs_currencies": "usd", "include_24hr_change": "true"})
        if response.status_code != 200:
            raise MarketsUnavailable(f"CoinGecko answered HTTP {response.status_code}")
        out = {}
        for coin, row in (response.json() or {}).items():
            symbol = ids.get(coin)
            price, change = (row or {}).get("usd"), (row or {}).get("usd_24h_change")
            if symbol and isinstance(price, (int, float)):
                previous = price / (1 + change / 100) if isinstance(change, (int, float)) and change > -100 else None
                out[symbol] = Quote(symbol=symbol, price=float(price), previous=previous, currency="USD",
                                    at=datetime.now(timezone.utc))
        return out

    async def quotes(self, symbols: list[str]) -> tuple[list[Quote], bool]:
        """(quotes in order, any of them old). Unknown symbols are left out. Raises MarketsUnavailable."""
        now = clock.monotonic()
        wanted = [s for s in symbols if not (s in self._cache and now - self._cache[s][0] < FRESH_SECONDS)]
        if wanted:
            async with self._lock:
                now = clock.monotonic()
                wanted = [s for s in wanted if not (s in self._cache and now - self._cache[s][0] < FRESH_SECONDS)]
                if wanted and now >= self._retry_at:
                    await self._fetch(wanted, now)
        now = clock.monotonic()
        got, old = [], False
        for s in symbols:
            hit = self._cache.get(s)
            if hit and now - hit[0] < STALE_OK_SECONDS:
                got.append(hit[1])
                old = old or now - hit[0] >= FRESH_SECONDS
        if symbols and not got:
            raise MarketsUnavailable("no quotes right now")
        return got, old

    async def _fetch(self, symbols: list[str], now: float) -> None:
        failed: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                results = await asyncio.gather(*(self._chart(client, s) for s in symbols), return_exceptions=True)
                for symbol, result in zip(symbols, results):
                    if isinstance(result, Quote):
                        self._cache[symbol] = (now, result)
                    elif isinstance(result, UnknownSymbol):
                        log.info("markets: %s isn't a symbol Yahoo knows", symbol)
                    else:
                        failed.append(symbol)
                coins = [s for s in failed if s in COINS]
                if coins:
                    try:
                        for symbol, q in (await self._coins(client, coins)).items():
                            self._cache[symbol] = (now, q)
                            failed.remove(symbol)
                    except (httpx.HTTPError, ValueError, MarketsUnavailable) as exc:
                        log.warning("markets: CoinGecko failed too: %s", exc)
        except httpx.HTTPError as exc:
            failed = symbols
            log.warning("markets: fetch failed: %s", exc)
        if failed:
            self._retry_at = now + RETRY_AFTER_SECONDS
            log.warning("markets: no fresh quote for %s", ", ".join(failed))
        else:
            self._retry_at = 0.0

    def cached(self, symbols: list[str], max_age: float = FACTS_FRESH_SECONDS) -> dict[str, Quote]:
        """The quotes already in hand and recent, without asking anyone."""
        now = clock.monotonic()
        return {s: self._cache[s][1] for s in symbols if s in self._cache and now - self._cache[s][0] < max_age}

    async def check(self, symbol: str) -> bool | None:
        """True: a real symbol. False: Yahoo doesn't know it. None: couldn't ask right now."""
        if symbol in COINS or symbol in NAMES:
            return True
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                q = await self._chart(client, symbol)
        except UnknownSymbol:
            return False
        except (httpx.HTTPError, ValueError, MarketsUnavailable):
            return None
        self._cache[symbol] = (clock.monotonic(), q)
        return True

    async def _symbols(self) -> tuple[list[str], bool]:
        mine = await self.ctx.store.watchlist()
        return (mine, True) if mine else (list(DEFAULTS), False)

    # -- words ---------------------------------------------------------------------------

    async def report(self, symbols: list[str] | None = None) -> Answer:
        if symbols is None:
            symbols, _ = await self._symbols()
        try:
            got, old = await self.quotes(symbols)
        except MarketsUnavailable:
            return Answer("I can't reach the markets right now. Try again in a few minutes.")
        if not got:
            return Answer("I couldn't find a price for that.")
        said = [f"{name(q.symbol)} is {moved(q)}" for q in got[:3]]
        speech = (", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0]) + "."
        if len(got) == 1:
            q = got[0]
            speech = f"{name(q.symbol)} is at {level(q)}, {moved(q)} today."
        detail = "\n".join(f"- {name(q.symbol)}{'' if name(q.symbol) == q.symbol else f' ({q.symbol})'}: "
                           f"{level(q)} {pct(q.change)}".rstrip() for q in got)
        if old:
            detail += "\n\n(The latest I could get; the markets feed isn't answering right now.)"
        return Answer(speech, detail + "\n\nDelayed quotes.")

    async def command(self, name_: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name_ in {"markets", "stocks"} or (name_ == "watch" and not rest):
            return await self.report()
        said = [p for p in re.split(r"\s*(?:,|\band\b)\s*|\s+", rest) if p]
        if name_ == "unwatch":
            gone = []
            for word in said:
                symbol = ticker(word)
                if symbol and await self.ctx.store.unwatch(symbol):
                    gone.append(symbol)
            if not gone:
                return Answer("That isn't on your watchlist. /markets shows what is.")
            return Answer(f"Stopped watching {', '.join(gone)}.")
        mine = await self.ctx.store.watchlist()
        added, unknown, bad = [], [], []
        for word in said:
            symbol = ticker(word)
            if symbol is None:
                bad.append(word)
                continue
            if symbol in mine or symbol in added:
                continue
            if len(mine) + len(added) >= MAX_WATCHED:
                return Answer(f"Your watchlist is full ({MAX_WATCHED}). /unwatch one first.")
            if await self.check(symbol) is False:
                unknown.append(symbol)
                continue
            await self.ctx.store.watch(symbol)
            added.append(symbol)
        parts = []
        if added:
            parts.append(f"Watching {', '.join(name(s) for s in added)}.")
        if unknown:
            parts.append(f"I can't find {', '.join(unknown)}.")
        if bad:
            parts.append(f"{', '.join(bad[:3])} isn't a ticker.")
        if not parts:
            return Answer("You're already watching that.")
        return Answer(" ".join(parts))

    async def match(self, text: str) -> Answer | None:
        if _MARKET.match(text):
            return await self.report()
        m = _PRICE.match(text)
        if not m:
            return None
        said = next(g for g in m.groups() if g)
        symbol = symbol_for(said, await self.ctx.store.watchlist())
        if symbol is None:
            return None  # "how's Keegan doing?"
        return await self.report([symbol])

    # -- FACTS and the panel ------------------------------------------------------------------

    async def facts(self) -> list[str]:
        """Only what's cached and recent: FACTS never waits on a quote."""
        symbols, _ = await self._symbols()
        hits = self.cached(symbols)
        fresh = [hits[s] for s in symbols if s in hits]
        if not fresh:
            return []
        return ["- MARKETS (delayed quotes): " + "; ".join(f"{name(q.symbol)} {level(q)} {pct(q.change)}".rstrip()
                                                       for q in fresh)]

    async def panel(self) -> dict | None:
        symbols, mine = await self._symbols()
        try:
            got, old = await self.quotes(list(dict.fromkeys([*symbols, *OVERVIEW])))
        except MarketsUnavailable:
            return {"title": "Markets", "lines": ["Can't reach the markets right now."], "quotes": [], "overview": [],
                    "mine": mine, "down": True}
        by = {q.symbol: q for q in got}

        def shown(q: Quote) -> dict:
            return {"symbol": q.symbol, "name": name(q.symbol), "price": level(q), "value": q.price,
                    "previous": q.previous, "change": q.change, "spark": list(q.spark)}

        quotes = [by[s] for s in symbols if s in by]
        return {
            "title": "Markets", "mine": mine, "old": old,
            "lines": [f"{name(q.symbol)} {level(q)} {pct(q.change)}".rstrip() for q in quotes],
            "quotes": [shown(q) for q in quotes],
            "overview": [shown(by[s]) for s in OVERVIEW if s in by],
        }


def build(ctx: SkillContext) -> Skill:
    return Markets(ctx)
