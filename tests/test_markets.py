"""Markets against stub Yahoo and CoinGecko servers: the watchlist, answers, FACTS, the panel, outages.

No network. The watchlist lives in a fake store here; its SQL is checked at the
end when DATABASE_URL is set (DESTRUCTIVE: truncates watchlist).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

import sloane.skills.markets as markets
from sloane.actions import check as action_check, available
from sloane.skills import Registry, SkillContext
from sloane.skills.markets import Markets, parse_chart, symbol_for, ticker

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


PRICES = {"^GSPC": (5712.3, 5688.1), "^IXIC": (18190.0, 18300.0), "BTC-USD": (64210.0, 65000.0),
          "AAPL": (227.52, 225.0), "NVDA": (121.4, 121.4)}


def chart(symbol: str) -> dict:
    price, previous = PRICES[symbol]
    return {"chart": {"result": [{
        "meta": {"currency": "USD", "symbol": symbol, "regularMarketPrice": price, "chartPreviousClose": previous,
                 "regularMarketTime": 1790000000},
        "timestamp": [1, 2, 3],
        "indicators": {"quote": [{"close": [previous, None, (previous + price) / 2, price]}]},
    }], "error": None}}


class Yahoo(BaseHTTPRequestHandler):
    asked: list[str] = []
    down = False
    agents: list[str] = []

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path)
        symbol = unquote(path.path.rsplit("/", 1)[-1])
        Yahoo.asked.append(symbol)
        Yahoo.agents.append(self.headers.get("User-Agent", ""))
        if Yahoo.down:
            self.reply(503, {})
        elif symbol not in PRICES:
            self.reply(404, {"chart": {"result": None, "error": {"code": "Not Found"}}})
        else:
            self.reply(200, chart(symbol))

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class Gecko(Yahoo):
    def do_GET(self):  # noqa: N802
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        Gecko.asked.append(query.get("ids", ""))
        self.reply(200, {"bitcoin": {"usd": 63000.0, "usd_24h_change": -2.5}})


Gecko.asked = []


class FakeStore:
    def __init__(self):
        self.symbols: list[str] = []

    async def watchlist(self):
        return list(self.symbols)

    async def watch(self, symbol):
        if symbol in self.symbols:
            return False
        self.symbols.append(symbol)
        return True

    async def unwatch(self, symbol):
        if symbol not in self.symbols:
            return False
        self.symbols.remove(symbol)
        return True

    async def active_session(self, idle_minutes):
        return None


# -- words to symbols ----------------------------------------------------------------------------
check("names people say", [symbol_for(w) for w in ("bitcoin", "the S&P", "Tesla", "NVDA", "$amd", "Keegan", "nvda")],
      ["BTC-USD", "^GSPC", "TSLA", "NVDA", "AMD", None, None])
check("a watched ticker in lower case counts", symbol_for("pltr", ["PLTR"]), "PLTR")
check("after /watch, anything shaped like a ticker", [ticker(w) for w in ("nvda", "brk.b", "btc", "not a ticker!")],
      ["NVDA", "BRK.B", "BTC-USD", None])
q = parse_chart(chart("AAPL"), "AAPL")
check("a quote: price, change, and the day's line without gaps", (q.price, round(q.change, 2), len(q.spark)),
      (227.52, 1.12, 3))


async def main() -> None:
    yahoo = HTTPServer(("127.0.0.1", 0), Yahoo)
    gecko = HTTPServer(("127.0.0.1", 0), Gecko)
    for s in (yahoo, gecko):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    now = [1000.0]
    markets.clock = types.SimpleNamespace(monotonic=lambda: now[0])
    config = isolated(markets_api_base=f"http://127.0.0.1:{yahoo.server_port}",
                      markets_crypto_base=f"http://127.0.0.1:{gecko.server_port}/api/v3")
    store = FakeStore()
    ctx = SkillContext(store=store, config=config)
    skill = Markets(ctx)
    reg = Registry([skill], ctx)

    async def say(text):
        answer = await reg.route(text)
        return None if answer is None else answer.speech

    async def cmd(name, rest=""):
        return await reg.command(name, rest)

    check("before anything's fetched, FACTS has nothing (and asks nobody)", (await skill.facts(), Yahoo.asked), ([], []))

    # -- nothing watched: the broad market --------------------------------------------------------
    answer = await cmd("markets")
    check("the broad market, said", answer.speech, "S&P 500 is up 0.4%, Nasdaq is down 0.6% and Bitcoin is down 1.2%.")
    check("and listed", answer.detail.splitlines()[:3],
          ["- S&P 500 (^GSPC): 5,712 +0.43%", "- Nasdaq (^IXIC): 18,190 −0.60%", "- Bitcoin (BTC-USD): $64,210 −1.22%"])
    check("with a browser's user agent (Yahoo refuses a bare client)", "Mozilla" in Yahoo.agents[-1], True)
    asked = len(Yahoo.asked)
    await say("how's the market doing today?")
    check("asked again within five minutes: from the cache", len(Yahoo.asked), asked)
    check("FACTS, from the cache", await skill.facts(),
          ["- MARKETS (delayed quotes): S&P 500 5,712 +0.43%; Nasdaq 18,190 −0.60%; Bitcoin $64,210 −1.22%"])

    # -- his list -----------------------------------------------------------------------------------
    check("watch two", (await cmd("watch", "aapl, nvda")).speech, "Watching Apple, Nvidia.")
    check("not a real one", (await cmd("watch", "ZZZZ")).speech, "I can't find ZZZZ.")
    check("already", (await cmd("watch", "AAPL")).speech, "You're already watching that.")
    check("the list now", store.symbols, ["AAPL", "NVDA"])
    check("one of them", await say("what's apple at?"), "Apple is at $227.52, up 1.1% today.")
    check("flat is flat", await say("how's NVDA doing"), "Nvidia is at $121.40, flat today.")
    check("not a stock: the agent's", await say("how's Keegan doing?"), None)
    check("nor a lower-case word that isn't his", await say("what's lunch at?"), None)
    check("my stocks", await say("how are my stocks?"), "Apple is up 1.1% and Nvidia is flat.")
    check("unwatch", (await cmd("unwatch", "nvda")).speech, "Stopped watching NVDA.")
    check("unwatch what isn't there", (await cmd("unwatch", "nvda")).speech,
          "That isn't on your watchlist. /markets shows what is.")

    panel = await skill.panel()
    check("the panel: his list, with its line", (panel["mine"], [q["symbol"] for q in panel["quotes"]],
                                                 len(panel["quotes"][0]["spark"])), (True, ["AAPL"], 3))

    # -- outages ----------------------------------------------------------------------------------
    store.symbols = ["AAPL", "BTC-USD"]
    now[0] += 6 * 60
    Yahoo.down = True
    answer = await cmd("markets")
    check("Yahoo down: crypto from CoinGecko, the rest from the last fetch, marked",
          (answer.speech, "isn't answering" in answer.detail, Gecko.asked[-1]),
          ("Apple is up 1.1% and Bitcoin is down 2.5%.", True, "bitcoin"))
    asked = len(Yahoo.asked)
    now[0] += 60
    await cmd("markets")
    check("after a failure, no retry for five minutes", len(Yahoo.asked), asked)
    now[0] += 4 * 3600
    check("hours later, nothing old is passed off as a price: Bitcoin only (CoinGecko)",
          (await cmd("markets")).speech, "Bitcoin is at $63,000, down 2.5% today.")
    check("FACTS drops what's old", await skill.facts(), ["- MARKETS (delayed quotes): Bitcoin $63,000 −2.50%"])
    Yahoo.down = False
    now[0] += 6 * 60
    check("back up", (await cmd("markets")).speech, "Apple is up 1.1% and Bitcoin is down 1.2%.")

    # -- what she may do for him in conversation ----------------------------------------------------
    allowed = available(frozenset({"watch", "unwatch", "workout"}))
    check("she may add to his watchlist, never take off it",
          (action_check("/watch NVDA", allowed), action_check("/unwatch NVDA", allowed)), ("/watch NVDA", None))
    check("she may log a workout, never take one back",
          (action_check("/workout run 3 mi", allowed), action_check("/workout undo", allowed)), ("/workout run 3 mi", None))

    yahoo.shutdown()
    gecko.shutdown()

    if os.environ.get("DATABASE_URL"):
        from sloane.memory.store import Store

        async with Store(isolated(database_url=os.environ["DATABASE_URL"])) as real:
            await real._exec("truncate watchlist")
            check("store: watch", (await real.watch("AAPL"), await real.watch("AAPL"), await real.watch("^GSPC")),
                  (True, False, True))
            check("store: in the order added", await real.watchlist(), ["AAPL", "^GSPC"])
            check("store: unwatch", (await real.unwatch("AAPL"), await real.unwatch("AAPL")), (True, False))
            try:
                await real.watch("drop table;")
                check("a non-ticker is refused by the table itself", True, False)
            except Exception:  # noqa: BLE001
                pass


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("markets: the watchlist, quotes, the cache, FACTS, the panel and outages")
