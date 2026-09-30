"""Bank: his accounts, read-only, through SimpleFIN: balances, what he spends, and his portfolio.

    /bank          balances, this week's spending, the portfolio
    /bank sync     ask the bank now (at most every half hour)
    "how much money do I have?"  ·  "what's my balance?"  ·  "how much did I spend this week?"
    "how's my portfolio?"  ·  "how are my investments doing?"

SimpleFIN Bridge (simplefin.org, about $15 a year) connects to his bank, his card
and Fidelity, and hands out read-only data: accounts, balances, transactions and
holdings. The protocol has no way to move money, and neither does this skill;
moving money is a hard line anyway. Off unless SIMPLEFIN_ACCESS_URL is set
(scripts/simplefin_auth.py claims it from his setup token; DEPLOY 7k). That URL
carries the credentials: it is a password, so it lives only in .env, httpx gets
the credentials as auth apart from the address, and only the host is ever logged.

Syncs: the `bank_sync` job every three hours in waking hours and /bank sync
(SimpleFIN asks for 24 requests a day at most). The first sync brings 89 days of
transactions, later ones the last month. Everything is stored (sql/036) so the
page, FACTS and his questions never wait on the bank.

Descriptions are the bank's words, so they are INGESTED text: stored untrusted,
shown through safe_field, never in FACTS. The kind of each account and the
category of each transaction are hers, by rule (no model): transfers between
his own accounts and card payments are not spending.

The portfolio is his investment accounts' holdings, priced with the same delayed
quotes as the markets skill, for today's change; FACTS uses only quotes already
cached. The money skill counts the bank's spending with what he logs by hand
(cash), against his weekly budget.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as clock
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext
from sloane.skills.markets import _SYMBOL, Markets, MarketsUnavailable

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 45.0         # SimpleFIN asks the bank while we wait
MIN_SYNC_SECONDS = 30 * 60     # /bank sync at most this often
FIRST_DAYS = 89                # the first sync's backfill (SimpleFIN allows 90 at a time)
LATER_DAYS = 31
STALE_HOURS = 12               # older than this, the page says so
RECENT_DAYS = 14               # the latest transactions shown come from this far back at most
MAX_CENTS = 10**13
AGENT = "Mozilla/5.0 (compatible; Sloane/1.0)"

_CREDIT = re.compile(r"\b(?:credit|card|visa|mastercard|amex|american\s+express|discover|sapphire|freedom|"
                     r"quicksilver|savor|venture|platinum|signature)\b", re.I)
_INVEST = re.compile(r"\b(?:brokerage|individual|ira|roth|401\s?\(?k\)?|403\s?\(?b\)?|hsa|investments?|investing|"
                     r"529|tod|joint\s+wros|stocks?|crypto)\b", re.I)
_INVEST_ORG = re.compile(r"fidelity|vanguard|schwab|robinhood|e\*?trade|webull|merrill|interactive\s+brokers|"
                         r"betterment|wealthfront|acorns|stash|coinbase|m1\s+finance|public\.com", re.I)
_CASH = re.compile(r"\b(?:checking|savings|cash\s+management|spend|money\s+market\s+account)\b", re.I)
_LOAN = re.compile(r"\b(?:loan|mortgage|heloc|lease)\b", re.I)

# Not spending: money moving between his own accounts, a card being paid off, an investment bought.
_TRANSFER = re.compile(
    r"\b(?:transfer|xfer|trnsfr|tfr|payment\s*-?\s*thank\s*you|thank\s+you\s+payment|autopay|auto\s*pay|"
    r"automatic\s+payment|epay|e-payment|online\s+(?:banking\s+)?payment|mobile\s+payment|card\s+payment|"
    r"crd\s+pmt|credit\s+card\s+(?:payment|pmt)|payment\s+to\s+(?:chase|amex|american\s+express|discover|capital\s+one|"
    r"citi|card)|to\s+(?:savings|checking|brokerage)|from\s+(?:savings|checking|brokerage)|fidelity|vanguard|"
    r"schwab|robinhood|coinbase|acorns|venmo\s+cashout|cash\s*out)\b", re.I)
_INCOME = re.compile(r"\b(?:payroll|direct\s+dep(?:osit)?|dir\s+dep|salary|paycheck|interest\s+(?:paid|payment|earned)|"
                     r"dividend|refund|cashback|cash\s+back|reward)\b", re.I)
CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((name, re.compile(rx, re.I)) for name, rx in (
    ("food", r"uber\s*eats|doordash|grubhub|postmates|starbucks|dutch\s+bros|mcdonald|chick-?fil-?a|chipotle|"
             r"taco\s+bell|wendy|burger|subway|domino|pizza|panera|cafe|coffee|restaurant|grill|king\s+soopers|"
             r"safeway|whole\s+foods|trader\s+joe|sprouts|kroger|aldi|albertsons|7-?eleven|sonic|in-n-out|"
             r"raising\s+cane|panda\s+express|jersey\s+mike|qdoba|noodles|smoothie|jamba|boba|dunkin|krispy|"
             r"good\s+times|freddy|culver|five\s+guys|shake\s+shack|wingstop|chili|applebee|olive\s+garden|"
             r"grocery|market|bakery|deli|bagel|donut|tea"),
    ("gas", r"shell|exxon|chevron|conoco|phillips\s+66|sinclair|maverik|loaf\s+n\s+jug|kum\s*&\s*go|circle\s+k|"
            r"valero|\bbp\b|marathon|speedway|quiktrip|\bgas\b|fuel"),
    ("car", r"autozone|jiffy|valvoline|car\s*wash|parking|parkmobile|\bdmv\b|geico|progressive|state\s+farm|"
            r"allstate|tire|o'?reilly|napa\s+auto|toll|e-?470"),
    ("subscriptions", r"netflix|spotify|hulu|disney\s*\+|disney\s*plus|apple\.com|icloud|youtube|prime\s+video|"
                      r"amazon\s+prime|\bmax\.com|hbo|paramount|peacock|audible|xbox|playstation|nintendo|chatgpt|"
                      r"openai|anthropic|claude\.ai|whoop|patreon|twitch"),
    ("business", r"vercel|github|figma|namecheap|godaddy|workspace|gsuite|adobe|canva|notion|squarespace|wix|"
                 r"shopify|\baws\b|amazon\s+web|digitalocean|oracle|cloudflare|stripe|simplefin"),
    ("school", r"bookstore|chegg|college\s*board|school|dcsd|pearson|mcgraw|quizlet|deca|tuition|textbook"),
    ("shopping", r"amazon|amzn|target|walmart|best\s+buy|apple\s+store|ebay|etsy|nike|adidas|zara|h&m|uniqlo|"
                 r"\bross\b|tj\s*maxx|marshalls|kohl|nordstrom|old\s+navy|\bgap\b|foot\s+locker|dick'?s|costco|"
                 r"home\s+depot|lowe'?s|ikea|sephora|ulta"),
    ("fun", r"\bamc\b|regal|cinemark|fandango|ticketmaster|stubhub|seatgeek|steam|epic\s+games|bowling|topgolf|"
            r"dave\s*&\s*buster|arcade|concert|theatre|theater|golf|ski|climbing"),
    ("travel", r"\buber\b|\blyft\b|\brtd\b|transit|airline|united|southwest|frontier|delta|american\s+air|"
               r"hotel|airbnb|marriott|hilton"),
    ("health", r"\bcvs\b|walgreens|pharmacy|planet\s+fitness|24\s+hour\s+fitness|crunch|dentist|doctor|clinic|"
               r"urgent\s+care|hospital|optometr"),
))

_BALANCE = re.compile(
    r"^\s*(?:hey\s+)?(?:how\s+much\s+(?:money\s+)?(?:do\s+i\s+have|have\s+i\s+got|is\s+in\s+my\s+(?:bank|checking|account|"
    r"accounts|savings))|what'?s\s+(?:my|in\s+my)\s+(?:bank\s+)?(?:balance|balances|checking|savings|bank\s+account)|"
    r"what'?s\s+my\s+(?:bank\s+)?balance|what\s+is\s+my\s+(?:bank\s+)?balance|check\s+my\s+(?:bank|balance|accounts?)|"
    r"my\s+(?:bank\s+)?balance)(?:\s+(?:right\s+now|now|today|left))?\s*[?.!]*\s*$", re.I)
_SPENDING = re.compile(
    r"^\s*(?:hey\s+)?(?:how\s+much\s+(?:have\s+i|did\s+i|i'?ve)\s+spen[dt]|what\s+(?:have\s+i|did\s+i)\s+spen[dt]\s+"
    r"(?:money\s+)?on|what'?s\s+my\s+spending|where'?s\s+my\s+money\s+going)"
    r"(?:\s+(?P<when>this\s+week|this\s+month|today|so\s+far(?:\s+this\s+(?:week|month))?|lately))?\s*[?.!]*\s*$", re.I)
_PORTFOLIO = re.compile(
    r"^\s*(?:hey\s+)?(?:how'?s|how\s+is|how\s+are|how'?re|what'?s|what\s+is|check)\s+(?:my\s+)?(?:portfolio|"
    r"investments|stocks\s+in\s+fidelity|fidelity(?:\s+account)?|brokerage|retirement|roth(?:\s+ira)?|ira)"
    r"(?:\s+(?:doing|at|worth|looking))?(?:\s+today|\s+right\s+now)?\s*[?.!]*\s*$", re.I)


class BankUnavailable(RuntimeError):
    pass


def cents(value: Any) -> int | None:
    """SimpleFIN's "-33.29" (a string) into -3329. None if it isn't a number."""
    try:
        n = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not n.is_finite():
        return None
    out = int((n * 100).to_integral_value())
    return out if abs(out) < MAX_CENTS else None


def dollars(amount: int, *, sign: bool = False) -> str:
    """-3329 -> "-$33.29"; 124000 -> "$1,240"."""
    body = f"${abs(amount) / 100:,.0f}" if amount % 100 == 0 or abs(amount) >= 1_000_000 else f"${abs(amount) / 100:,.2f}"
    if amount < 0:
        return "−" + body
    return ("+" if sign else "") + body


def kind_of(name: str, org: str, balance: int | None, holdings: list) -> str:
    """cash, credit, investment or loan: her guess from what the bank calls it."""
    if holdings:
        return "investment"
    if _LOAN.search(name):
        return "loan"
    if _CREDIT.search(name):
        return "credit"
    if _CASH.search(name):
        return "cash"
    if _INVEST.search(name) or _INVEST_ORG.search(org):
        return "investment"
    return "cash"


def category_of(description: str, amount: int, kind: str) -> str:
    if _TRANSFER.search(description):
        return "transfer"
    if amount > 0:
        # Money in: pay, interest, a refund; on a card, anything coming in pays it down.
        return "income" if kind == "cash" or _INCOME.search(description) else "transfer"
    for name, pattern in CATEGORIES:
        if pattern.search(description):
            return name
    return "other"


def split_access(url: str) -> tuple[str, tuple[str, str] | None]:
    """The access URL without its credentials, and the credentials. Raises ValueError for one that
    isn't https (plain http only to this machine, for tests)."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if not host or parts.scheme not in ("https", "http") or (parts.scheme == "http" and host not in ("127.0.0.1", "localhost")):
        raise ValueError("SIMPLEFIN_ACCESS_URL isn't an https address")
    netloc = host + (f":{parts.port}" if parts.port else "")
    base = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    auth = (unquote(parts.username or ""), unquote(parts.password or "")) if parts.username else None
    return base, auth


def _moment(raw: Any) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class Parsed:
    accounts: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    holdings: dict[str, list[dict]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def parse_accounts(body: dict, zone: Any) -> Parsed:
    """SimpleFIN's /accounts answer into rows. Anything malformed is skipped, never guessed at."""
    out = Parsed()
    for e in list(body.get("errors") or []) + [x.get("msg") if isinstance(x, dict) else x for x in body.get("errlist") or []]:
        if e:
            out.errors.append(safe_field(str(e), limit=160))
    for acc in body.get("accounts") or []:
        if not isinstance(acc, dict) or not acc.get("id"):
            continue
        account_id = str(acc["id"])[:120]
        org = acc.get("org") if isinstance(acc.get("org"), dict) else {}
        org_name = safe_field(str(org.get("name") or org.get("domain") or ""), limit=60)
        name = safe_field(str(acc.get("name") or "Account"), limit=80) or "Account"
        raw_holdings = [h for h in acc.get("holdings") or [] if isinstance(h, dict)]
        balance = cents(acc.get("balance"))
        kind = kind_of(name, org_name, balance, raw_holdings)
        out.accounts.append({
            "id": account_id, "org": org_name, "name": name, "currency": str(acc.get("currency") or "USD")[:8],
            "kind": kind, "balance_cents": balance, "available_cents": cents(acc.get("available-balance")),
            "balance_at": _moment(acc.get("balance-date"))})
        for t in acc.get("transactions") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            amount = cents(t.get("amount"))
            when = _moment(t.get("posted")) or _moment(t.get("transacted_at"))
            if amount is None or when is None:
                continue
            description = safe_field(str(t.get("description") or t.get("payee") or t.get("memo") or ""), limit=120)
            out.transactions.append({
                "account_id": account_id, "id": str(t["id"])[:120], "posted_on": when.astimezone(zone).date(),
                "amount_cents": amount, "description": description, "pending": bool(t.get("pending")),
                "category": category_of(description, amount, kind)})
        if kind == "investment":
            rows = []
            for n, h in enumerate(raw_holdings):
                symbol = str(h.get("symbol") or "").strip().upper().lstrip("$")
                try:
                    shares = float(Decimal(str(h.get("shares")))) if h.get("shares") not in (None, "") else None
                except (InvalidOperation, ValueError):
                    shares = None
                rows.append({"id": str(h.get("id") or f"h{n}")[:120],
                             "symbol": symbol if _SYMBOL.match(symbol) else None,
                             "description": safe_field(str(h.get("description") or ""), limit=80),
                             "shares": shares, "market_value_cents": cents(h.get("market_value")),
                             "cost_basis_cents": cents(h.get("cost_basis"))})
            out.holdings[account_id] = rows
    return out


def basis(holding: dict) -> int | None:
    """The position's cost, total. Some brokerages send it per share: whichever reading is nearer
    what the position is worth is the one they meant."""
    cost, value, shares = holding.get("cost_basis_cents"), holding.get("market_value_cents"), holding.get("shares")
    if not cost or not value:
        return None
    if shares and shares > 0 and abs(cost * shares - value) < abs(cost - value):
        return round(cost * shares)
    return cost


class Bank(Skill):
    name = "bank"
    help = ("`/bank` — your balances, spending and portfolio (or ask \"how much money do I have?\"); `/bank sync`",)
    commands = frozenset({"bank"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self.base, self.auth = split_access(ctx.config.simplefin_access_url)
        self._lock = asyncio.Lock()
        self._synced = 0.0         # monotonic, the last sync that worked
        self._tried = -1e12        # monotonic, the last sync tried
        self._error = ""
        self.market = Markets(ctx)  # its own cache; the same delayed quotes as the markets skill

    # -- the sync -----------------------------------------------------------------------------

    async def sync(self, *, why: str = "job") -> tuple[bool, str]:
        """(worked, what happened). His /bank sync waits MIN_SYNC_SECONDS between tries."""
        async with self._lock:
            now = clock.monotonic()
            if why == "him" and now - self._tried < MIN_SYNC_SECONDS:
                wait = int((MIN_SYNC_SECONDS - (now - self._tried)) // 60) + 1
                return False, f"I asked the bank a few minutes ago. Try again in {wait} min."
            self._tried = now
            today = self.ctx.today()
            first = not await self.ctx.store.bank_accounts()
            start = datetime.combine(today - timedelta(days=FIRST_DAYS if first else LATER_DAYS), datetime.min.time(),
                                     self.ctx.now().tzinfo)
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": AGENT}) as client:
                    response = await client.get(f"{self.base}/accounts", auth=self.auth,
                                                params={"start-date": int(start.timestamp()), "pending": 1})
                if response.status_code in (401, 403):
                    raise BankUnavailable("SimpleFIN refused the access URL (run scripts/simplefin_auth.py again)")
                if response.status_code == 402:
                    raise BankUnavailable("SimpleFIN says the subscription needs paying")
                if response.status_code != 200:
                    raise BankUnavailable(f"SimpleFIN answered HTTP {response.status_code}")
                parsed = parse_accounts(response.json(), self.ctx.now().tzinfo)
            except (httpx.HTTPError, ValueError, BankUnavailable) as exc:
                # Never the exception's own text for an httpx error: keep the address out of the log.
                self._error = str(exc) if isinstance(exc, BankUnavailable) else f"couldn't reach SimpleFIN ({type(exc).__name__})"
                log.warning("bank sync failed (%s): %s", urlsplit(self.base).hostname, self._error)
                return False, self._error
            if not parsed.accounts:
                self._error = parsed.errors[0] if parsed.errors else "SimpleFIN sent no accounts"
                log.warning("bank sync: %s", self._error)
                return False, self._error
            await self.ctx.store.bank_save(parsed.accounts, parsed.transactions, parsed.holdings, today)
            self._synced, self._error = clock.monotonic(), ""
            for e in parsed.errors:
                log.info("bank sync note from SimpleFIN: %s", e)
            n = len(parsed.accounts)
            return True, f"{n} account{'s' if n != 1 else ''}, {len(parsed.transactions)} transactions"

    # -- what the tables say --------------------------------------------------------------------

    async def _numbers(self) -> dict | None:
        """Everything the words, FACTS and the page use, from SQL. None before the first sync."""
        accounts = [dict(a) for a in await self.ctx.store.bank_accounts()]
        if not accounts:
            return None
        today = self.ctx.today()
        monday = today - timedelta(days=today.weekday())
        cash = [a for a in accounts if a["kind"] == "cash"]
        credit = [a for a in accounts if a["kind"] == "credit"]

        def have(a: dict) -> int:
            return a["available_cents"] if a["available_cents"] is not None else (a["balance_cents"] or 0)

        first = today.replace(day=1)
        rows = [dict(t) for t in await self.ctx.store.bank_transactions_between(
            min(first, monday, today - timedelta(days=RECENT_DAYS)), today) if t["kind"] in ("cash", "credit")]

        def spent(t: dict) -> bool:
            return t["amount_cents"] < 0 and t["category"] not in ("transfer", "income")

        by_cat: dict[str, int] = {}
        for t in rows:
            if spent(t) and t["posted_on"] >= first:
                by_cat[t["category"]] = by_cat.get(t["category"], 0) - t["amount_cents"]
        days = [0] * 7
        for t in rows:
            if spent(t) and t["posted_on"] >= monday:
                days[(t["posted_on"] - monday).days] -= t["amount_cents"]
        seen = max((a["seen_at"] for a in accounts if a.get("seen_at")), default=None)
        return {
            "accounts": accounts, "cash": sum(have(a) for a in cash), "has_cash": bool(cash),
            "owed": -sum(a["balance_cents"] or 0 for a in credit), "has_credit": bool(credit),
            "week": await self.ctx.store.bank_spent_between(monday, today),
            "month": await self.ctx.store.bank_spent_between(today.replace(day=1), today),
            "by_cat": sorted(by_cat.items(), key=lambda kv: -kv[1]), "days": days,
            "recent": rows[:8],
            "seen": seen, "investments": [a for a in accounts if a["kind"] == "investment"],
        }

    async def portfolio(self, *, wait: bool = True) -> dict | None:
        """His investment accounts: worth, today's change from delayed quotes, and each holding.
        `wait=False` uses only quotes already cached (FACTS never waits on a quote)."""
        accounts = [dict(a) for a in await self.ctx.store.bank_accounts() if a["kind"] == "investment"]
        if not accounts:
            return None
        holdings = [dict(h) for h in await self.ctx.store.bank_holdings()]
        symbols = sorted({h["symbol"] for h in holdings if h["symbol"] and h["shares"]})
        quotes: dict[str, Any] = {}
        old = False
        if symbols:
            if wait:
                try:
                    got, old = await self.market.quotes(symbols)
                    quotes = {q.symbol: q for q in got}
                except MarketsUnavailable:
                    old = True
            else:
                quotes = self.market.cached(symbols)
        drift = {a["id"]: 0 for a in accounts}
        change = 0.0
        rows = []
        for h in holdings:
            q = quotes.get(h["symbol"] or "")
            shares = float(h["shares"]) if h["shares"] is not None else None
            value = h["market_value_cents"]
            pct = None
            if q is not None and shares:
                live = round(shares * q.price * 100)
                if value is not None and h["account_id"] in drift:
                    drift[h["account_id"]] += live - value
                value = live
                if q.previous:
                    change += shares * (q.price - q.previous) * 100
                    pct = q.change
            cost = basis(h)
            rows.append({"symbol": h["symbol"], "name": h["description"] or h["symbol"] or "Holding",
                         "value_cents": value, "change_pct": pct, "account": h["account"],
                         "gain_cents": value - cost if value is not None and cost else None})
        total = sum((a["balance_cents"] or 0) + drift.get(a["id"], 0) for a in accounts)
        gains = [r["gain_cents"] for r in rows if r["gain_cents"] is not None]
        rows.sort(key=lambda r: -(r["value_cents"] or 0))
        for r in rows:
            r["weight"] = (r["value_cents"] or 0) / total if total else 0
        start = total - change
        return {"value_cents": total, "change_cents": round(change) if quotes else None,
                "change_pct": change / start * 100 if quotes and start else None,
                "gain_cents": sum(gains) if gains else None, "holdings": rows, "old": old,
                "accounts": [{"name": a["name"], "org": a["org"], "value_cents": (a["balance_cents"] or 0) + drift[a["id"]]}
                             for a in accounts]}

    def _when(self, seen: datetime | None) -> str:
        if seen is None:
            return "never"
        local = seen.astimezone(self.ctx.now().tzinfo)
        day = dates.spoken(local.date(), self.ctx.today())
        return f"{'' if day == 'today' else day + ' '}{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"

    # -- words ----------------------------------------------------------------------------------

    async def report(self) -> Answer:
        n = await self._numbers()
        if n is None:
            return Answer("I haven't heard from your bank yet. Try /bank sync." if not self._error else
                          "I can't reach your bank right now. Try again in a bit.")
        said = []
        if n["has_cash"]:
            said.append(f"You have {dollars(n['cash'])} in checking and savings")
        if n["has_credit"] and n["owed"] > 0:
            said.append(f"owe {dollars(n['owed'])} on your card{'s' if len([a for a in n['accounts'] if a['kind'] == 'credit']) > 1 else ''}")
        speech = (" and ".join(said) + ". ") if said else ""
        speech += f"You've spent {dollars(n['week'])} this week."
        lines = [f"- {a['org'] + ' ' if a['org'] and a['org'] not in a['name'] else ''}{a['name']} ({a['kind']}): "
                 f"{dollars(a['balance_cents'] or 0)}" for a in n["accounts"]]
        lines.append(f"\nSpent this week: {dollars(n['week'])}; this month: {dollars(n['month'])}")
        lines += [f"  • {c}: {dollars(v)}" for c, v in n["by_cat"][:5]]
        p = await self.portfolio()
        if p:
            lines.append(f"\nPortfolio: {dollars(p['value_cents'])}"
                         + (f", today {dollars(p['change_cents'], sign=True)} ({p['change_pct']:+.2f}%)"
                            if p["change_cents"] is not None and p["change_pct"] is not None else ""))
        lines.append(f"\nFrom your bank as of {self._when(n['seen'])}.")
        return Answer(speech[0].upper() + speech[1:], "\n".join(lines))

    async def spending(self, when: str) -> Answer:
        n = await self._numbers()
        if n is None:
            return Answer("I haven't heard from your bank yet. Try /bank sync.")
        month = "month" in when
        total = n["month"] if month else n["week"]
        top = ", ".join(f"{c} {dollars(v)}" for c, v in n["by_cat"][:3])
        speech = f"You've spent {dollars(total)} this {'month' if month else 'week'} from your bank and cards."
        detail = f"This month by category: {top}." if top else ""
        return Answer(speech, detail)

    async def portfolio_answer(self) -> Answer:
        p = await self.portfolio()
        if p is None:
            return Answer("I don't see an investment account. Connect Fidelity in SimpleFIN and I'll pick it up.")
        speech = f"Your portfolio is at {dollars(p['value_cents'])}"
        if p["change_cents"] is not None and p["change_pct"] is not None:
            speech += f", {'up' if p['change_cents'] >= 0 else 'down'} {dollars(abs(p['change_cents']))} today ({abs(p['change_pct']):.1f}%)"
        lines = [f"- {h['symbol'] or h['name']}: {dollars(h['value_cents'] or 0)}"
                 + (f" ({h['change_pct']:+.2f}% today)" if h["change_pct"] is not None else "") for h in p["holdings"][:10]]
        if p["gain_cents"] is not None:
            lines.append(f"\nGain since bought: {dollars(p['gain_cents'], sign=True)}")
        return Answer(speech + ".", "\n".join(lines) + "\n\nDelayed quotes. Not advice.")

    async def command(self, name: str, rest: str) -> Answer | None:
        if rest.strip().lower() in {"sync", "refresh", "update", "now"}:
            ok, what = await self.sync(why="him")
            if not ok:
                return Answer(what if what.startswith("I asked") else f"The bank didn't answer: {what}.")
            n = await self._numbers()
            tail = f" You have {dollars(n['cash'])} in checking and savings." if n and n["has_cash"] else ""
            return Answer(f"Synced: {what}.{tail}")
        return await self.report()

    async def match(self, text: str) -> Answer | None:
        if _BALANCE.match(text):
            return await self.report()
        m = _SPENDING.match(text)
        if m:
            return await self.spending((m.group("when") or "").lower())
        if _PORTFOLIO.match(text):
            return await self.portfolio_answer()
        return None

    # -- FACTS, the panel, the heartbeat ---------------------------------------------------------

    async def facts(self) -> list[str]:
        n = await self._numbers()
        if n is None:
            return []
        today = self.ctx.today()
        monday = today - timedelta(days=today.weekday())
        parts = []
        if n["has_cash"]:
            parts.append(f"checking and savings {dollars(n['cash'])}")
        if n["has_credit"]:
            parts.append(f"cards owe {dollars(max(0, n['owed']))}")
        parts.append(f"spent {dollars(n['week'])} since Mon {monday:%b} {monday.day} and {dollars(n['month'])} this month")
        if n["by_cat"]:
            parts.append("this month by category: " + ", ".join(f"{c} {dollars(v)}" for c, v in n["by_cat"][:4]))
        lines = [f"- BANK (read-only, synced {self._when(n['seen'])}): " + "; ".join(parts)]
        p = await self.portfolio(wait=False)
        if p:
            line = f"- PORTFOLIO: {dollars(p['value_cents'])}"
            if p["change_cents"] is not None and p["change_pct"] is not None:
                line += f", today {dollars(p['change_cents'], sign=True)} ({p['change_pct']:+.2f}%)"
            lines.append(line)
        return lines

    async def panel(self) -> dict | None:
        n = await self._numbers()
        if n is None:
            return {"title": "Bank", "lines": ["Waiting for the first sync."], "waiting": True,
                    "error": self._error or None}
        today = self.ctx.today()
        history = await self.ctx.store.bank_history(today - timedelta(days=90))
        by_day: dict[str, dict[str, int]] = {}
        for h in history:
            by_day.setdefault(h["on_day"].isoformat(), {})[h["kind"]] = int(h["cents"])
        stale = n["seen"] is not None and (self.ctx.now() - n["seen"]).total_seconds() > STALE_HOURS * 3600
        monday = today - timedelta(days=today.weekday())
        return {
            "title": "Bank", "synced": self._when(n["seen"]), "stale": stale, "error": self._error or None,
            "lines": [f"Cash {dollars(n['cash'])}", f"Spent this week {dollars(n['week'])}"],
            "cash_cents": n["cash"] if n["has_cash"] else None, "owed_cents": n["owed"] if n["has_credit"] else None,
            "spent_week_cents": n["week"], "spent_month_cents": n["month"],
            "categories": [{"name": c, "cents": v} for c, v in n["by_cat"][:6]],
            "days": [{"day": f"{monday + timedelta(days=i):%a}", "cents": c, "today": monday + timedelta(days=i) == today}
                     for i, c in enumerate(n["days"])],
            "recent": [{"what": t["description"] or "Transaction", "cents": t["amount_cents"], "category": t["category"],
                        "day": dates.spoken(t["posted_on"], today).replace("today", "Today").replace("yesterday", "Yesterday")
                        if (today - t["posted_on"]).days < 2 else f"{t['posted_on']:%a %b} {t['posted_on'].day}",
                        "pending": t["pending"]} for t in n["recent"][:8]],
            "cash_history": [{"on": d, "cents": k.get("cash", 0) + k.get("credit", 0)} for d, k in by_day.items()
                             if "cash" in k],
            "invest_history": [{"on": d, "cents": k["investment"]} for d, k in by_day.items() if "investment" in k],
            "portfolio": await self.portfolio(),
        }

    async def nudges(self) -> list[Nudge]:
        low = self.ctx.config.bank_low_balance
        if not low or low <= 0:
            return []
        n = await self._numbers()
        if n is None or not n["has_cash"] or n["cash"] >= round(low * 100):
            return []
        return [Nudge(f"bank:low:{self.ctx.today()}",
                      f"💳 Checking and savings are down to {dollars(n['cash'])}. Easy on the card till payday.")]


def build(ctx: SkillContext) -> Skill | None:
    url = ctx.config.simplefin_access_url
    if not url:
        return None
    try:
        return Bank(ctx)
    except ValueError as exc:
        log.warning("bank is off: %s", exc)
        return None
