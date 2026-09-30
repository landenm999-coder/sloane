"""Monitors: his words to a price line, a page or a web question; checked on their own pace; said once;
private addresses refused; the job holds in quiet hours and focus. Stub Yahoo, a stub page, a fake lookup.

DESTRUCTIVE: truncates monitors.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

import sloane.skills.markets as markets
import sloane.skills.monitors as monitors
from sloane.skills import Registry, Skill, SkillContext
from sloane.skills.monitors import Monitors, Refused, parse, public, visible

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- his words --------------------------------------------------------------------------------------
check("a price line, his verb deciding the way", [parse(t) for t in (
    "NVDA goes above 150", "bitcoin drops below 60k", "Tesla hits 300", "the S&P 500 is over 6,000", "$AMD climbs to 200")],
    [{"kind": "price", "symbol": "NVDA", "line": 150.0, "direction": "above"},
     {"kind": "price", "symbol": "BTC-USD", "line": 60000.0, "direction": "below"},
     {"kind": "price", "symbol": "TSLA", "line": 300.0, "direction": None},
     {"kind": "price", "symbol": "^GSPC", "line": 6000.0, "direction": "above"},
     {"kind": "price", "symbol": "AMD", "line": 200.0, "direction": "above"}])
check("a page, with words to wait for or not", [parse(t) for t in (
    "https://store.example/airpods says in stock", "https://x.example/page changes", 'https://a.example/p for "Sold out"')],
    [{"kind": "page", "url": "https://store.example/airpods", "phrase": "in stock"},
     {"kind": "page", "url": "https://x.example/page", "phrase": None},
     {"kind": "page", "url": "https://a.example/p", "phrase": "Sold out"}])
check("anything else is a question for the web", [parse(t)["kind"] for t in (
    "the iPhone 18 release date is announced", "the Broncos game time is set", "my cousin gets 5 points")],
    ["question", "question", "question"])
check("a page's text as read", visible("<html><style>x{}</style><script>var a=1</script><p>In&nbsp;stock &amp; ready</p></html>"),
      "In stock & ready")
for host, port in (("127.0.0.1", None), ("localhost", None), ("10.0.0.5", None), ("169.254.169.254", None),
                   ("::1", None), ("8.8.8.8", 8000)):
    try:
        public(host, port)
        FAILURES.append(f"{host}:{port} should be refused")
    except Refused:
        pass


# -- stubs ----------------------------------------------------------------------------------------
PRICES = {"NVDA": [121.4, 151.2], "TSLA": [244.1]}


class Yahoo(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):  # noqa: N802
        symbol = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        if symbol not in PRICES:
            return self.reply(404, {"chart": {"result": None, "error": {"code": "Not Found"}}})
        series = PRICES[symbol]
        price = series[min(Yahoo.calls, len(series) - 1)] if symbol == "NVDA" else series[0]
        if symbol == "NVDA":
            Yahoo.calls += 1
        self.reply(200, {"chart": {"result": [{"meta": {"currency": "USD", "regularMarketPrice": price,
                                                        "chartPreviousClose": 120.0},
                                               "indicators": {"quote": [{"close": [120.0, price]}]}}], "error": None}})

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class Page(Yahoo):
    text = "<p>AirPods Pro: Sold out</p>"

    def do_GET(self):  # noqa: N802
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = Page.text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(handler) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Router:
    def __init__(self):
        self.answers = ["NO\nNothing announced yet (Apple Newsroom, https://apple.example)",
                        "YES\nApple announced it for Sept 18 (Apple Newsroom, https://apple.example/news)"]
        self.asked = []

    async def research(self, query):
        self.asked.append(query)
        return self.answers.pop(0)


class Focus(Skill):
    name = "focusing"
    why = None

    async def hold(self):
        return self.why


async def integration() -> None:
    from sloane.jobs.briefs import JobContext, monitors as monitors_job
    from sloane.jobs.governor import Governor
    from sloane.memory.store import Store

    yahoo, page = serve(Yahoo), serve(Page)
    base = f"http://127.0.0.1:{page.server_port}"
    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      markets_api_base=f"http://127.0.0.1:{yahoo.server_port}", monitor_questions=1)
    now = {"at": datetime(2026, 9, 30, 12, 5, tzinfo=DEN)}
    # The stub page lives on this machine, which the real check refuses: let loopback through
    # for the test, except the address the redirect points at.
    real_public = monitors.public
    monitors.public = lambda host, port: None if port == page.server_port else real_public(host, port)
    # Quotes are cached on a monotonic clock: it follows the test's time.
    real_clock = markets.clock
    markets.clock = types.SimpleNamespace(monotonic=lambda: now["at"].timestamp())
    try:
        async with Store(config) as store:
            await store._exec("truncate monitors")
            router = Router()
            ctx = SkillContext(store=store, config=config, router=router, clock=lambda: now["at"])
            skill, focus = Monitors(ctx), Focus(ctx)
            reg = Registry([skill, focus], ctx)
            said: list[tuple[str, bool]] = []

            async def say(text, tainted=False):
                said.append((text, tainted))

            job = JobContext(store=store, agent=None, governor=Governor(store, config), config=config, say=say,
                             skills=reg)

            check("a price line", (await reg.route("let me know when NVDA goes above 150")).speech,
                  "I'll tell you when Nvidia goes above $150.00. It's $121.40 now.")
            check("already there", (await reg.command("monitor", "NVDA below 130")).speech,
                  "Nvidia is already below that: $121.40.")
            check("a page, waiting for words",
                  (await reg.route(f"alert me when {base}/airpods says in stock")).speech, "Watching 127.0.0.1 for \"in stock\".")
            check("a redirect to a private address is refused",
                  (await reg.command("monitor", f"{base}/moved changes")).speech,
                  "I couldn't open that page (only the usual web ports). Check the address?")
            check("the box itself is never fetched", (await reg.command("monitor", "http://127.0.0.1:8000/app changes")).speech,
                  "I won't watch that address: only the usual web ports.")
            check("a question for the web", (await reg.route("let me know when the iPhone 18 release date is announced")).speech,
                  "I'll keep an eye out: the iPhone 18 release date is announced. I'll tell you when it happens.")
            check("questions are capped", (await reg.command("monitor", "the Broncos game time is set")).speech,
                  "I'm already watching 1 things on the web, the most at once. /monitors lists them; stop one first.")
            facts = await skill.facts()
            check("FACTS: his words and her numbers", facts[0].startswith("- MONITORS he set") and "now $121.40" in facts[0], True)

            # First run: nothing has happened.
            result = await monitors_job(job, now["at"])
            check("first run: nothing yet", (result.ran, said, len(router.asked)), (True, [], 1))

            # Quiet hours and a focus block hold it.
            check("quiet hours hold it", (await monitors_job(job, now["at"].replace(hour=2))).ran, False)
            focus.why = "he's focusing until 1:00 PM"
            check("a focus block holds it", (await monitors_job(job, now["at"])).reason, "held: he's focusing until 1:00 PM")
            focus.why = None

            # Fifteen minutes on: the price has crossed, the page too; the question isn't due.
            now["at"] += timedelta(minutes=16)
            Page.text = "<p>AirPods Pro: In stock</p>"
            await monitors_job(job, now["at"])
            check("each on its own pace: the price (every 15 minutes) now, not the page (hourly) or the question",
                  said, [("📈 Nvidia is above $150.00: $151.20 now (+26.0% today).", False)])
            said.clear()
            await monitors_job(job, now["at"] + timedelta(minutes=16))
            check("and never again", said, [])

            # Six hours on: the question's check says yes; the evidence is web text.
            now["at"] += timedelta(hours=6)
            await monitors_job(job, now["at"])
            check("the page, and the question that happened: its evidence is web text, said tainted", sorted(said),
                  sorted([(f"👀 127.0.0.1 now says \"in stock\": {base}/airpods", False),
                          ("👀 It happened: the iPhone 18 release date is announced\n\n"
                           "Apple announced it for Sept 18 (Apple Newsroom, https://apple.example/news)", True)]))
            check("nothing open now", await store.open_monitors(), [])

            # He stops one; one runs out.
            said.clear()
            await reg.command("monitor", "the Broncos game time is set")
            check("stop by words", (await reg.command("monitor", "stop broncos")).speech,
                  "Stopped watching for this: the Broncos game time is set.")
            await reg.command("monitor", "Tesla hits 999")
            now["at"] += timedelta(days=31)
            await monitors_job(job, now["at"])
            check("after thirty days it ends, and says so", said,
                  [("I stopped watching for this after 30 days: Tesla hits 999. It never happened.", False)])
            panel = await skill.panel()
            check("the panel: what ended lately, and how", [(i["said"], i["state"]) for i in panel["items"]][:1],
                  [("Tesla hits 999", "Ran out")])
            await store._exec("truncate monitors")
    finally:
        monitors.public = real_public
        markets.clock = real_clock
        yahoo.shutdown()
        page.shutdown()


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("monitors: his words to a price, a page or a question; said once; private addresses refused; quiet and focus hold")
