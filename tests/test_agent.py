"""The agent's view of *now*. No database, no model.

Two bugs this pins:

* "Today" came from the container's clock. Docker runs in UTC, and from 6 PM to
  midnight in Parker it is already tomorrow in UTC -- so "what's due tonight?"
  at 8 PM searched tomorrow and missed tonight's 11:59 PM homework.
* The prompt never stated the date. The Claude CLI injects it into its own
  system prompt, which hid the problem; on Groq or the API a model asked about
  "Friday" had to guess what today was.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.agent import Agent
from sloane.memory.embed import EmbedUnavailable
from sloane.providers.base import Completion, Provider, Usage
from sloane.router import Router

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class EmptyStore:
    """Every read returns nothing; every write is accepted and forgotten."""

    def __init__(self) -> None:
        self.seen_dates: list[date] = []

    async def assignments_due(self, start, end, **_):
        self.seen_dates.append(start)
        return []

    async def get_state(self): return []
    async def get_working_set(self): return []
    async def overdue_assignments(self): return []
    async def shifts_between(self, start, end): return []
    async def courses(self): return []
    async def open_commitments(self): return []
    async def events_between(self, start, end): return []
    async def reminders_between(self, start, end): return []
    async def search_episodes(self, *a, **k): return []
    async def add_episode(self, *a, **k): return "x"
    async def log_usage(self, **k): return None


class NoEmbedder:
    async def embed_one(self, text): raise EmbedUnavailable("not here")
    async def embed(self, texts): raise EmbedUnavailable("not here")


class Recorder(Provider):
    name = "recorder"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system, prompt, *, max_tokens=1024):
        self.prompts.append(prompt)
        return Completion(
            text='{"speech": "ok", "detail": "ok"}',
            usage=Usage(provider=self.name),
        )


def turn(tz: str, today: date | None = None) -> tuple[str, EmptyStore]:
    config = isolated(timezone=tz, main_provider="claude_code")
    recorder = Recorder()
    router = Router(config, factory=lambda n, c, b: recorder)
    store = EmptyStore()
    agent = Agent(store, config, router=router, embedder=NoEmbedder())
    asyncio.run(agent.answer("what's due tonight?", today=today))
    return recorder.prompts[0], store


# Pretend to be a container: the process clock is UTC.
os.environ["TZ"] = "UTC"
time.tzset()

# UTC+14 and UTC-11 are twenty-five hours apart, so their calendar dates always
# differ. Whichever one the agent is configured for is the only date that may
# appear -- which a container-clock implementation cannot satisfy for both.
for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "America/Denver"):
    local = datetime.now(ZoneInfo(zone)).date()
    prompt, store = turn(zone)
    check(f"{zone}: FACTS are read for the local date", store.seen_dates[0], local)
    now_line = next((l for l in prompt.splitlines() if l.startswith("NOW: ")), "")
    check(f"{zone}: the prompt states NOW", bool(now_line), True)
    check(
        f"{zone}: NOW names the local date",
        f"{local:%B} {local.day}, {local:%Y}" in now_line, True,
    )

east = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
west = datetime.now(ZoneInfo("Pacific/Pago_Pago")).date()
check("the two probe zones really are on different dates", east != west, True)

# NOW sits after the stable blocks and right before the question, so a future
# prompt cache can hold everything above it.
prompt, _ = turn("America/Denver")
lines = prompt.splitlines()
now_at = next(i for i, l in enumerate(lines) if l.startswith("NOW: "))
check(
    "NOW is the block immediately before the question",
    next(l for l in lines[now_at + 1:] if l.strip()), "LANDEN:",
)
check("and carries a time of day", (" AM" in lines[now_at]) or (" PM" in lines[now_at]), True)

# A brief or test that pins a different day gets that day, without a clock time
# that would contradict it.
pinned = date(2026, 9, 25)
prompt, store = turn("America/Denver", today=pinned)
check("a pinned day drives the FACTS window", store.seen_dates[0], pinned)
check(
    "and the prompt names that day, not today",
    "NOW: Friday September 25, 2026 (Landen's local time)" in prompt, True,
)

# A capture made during the chat is not in CONVERSATION (that is Telegram
# only), so it must survive the recall de-dup. It didn't: "what did I just
# capture?" came back with recall=0 while the capture sat in episodes.
from datetime import timedelta  # noqa: E402

denver = ZoneInfo("America/Denver")
now = datetime.now(denver)


class ChatStore(EmptyStore):
    async def recent_messages(self, chat_id, since, limit=20):
        return [
            {"direction": "in", "kind": "text", "body": "hey", "at": now - timedelta(minutes=10), "trusted": True},
            {"direction": "out", "kind": "text", "body": "Evening.", "at": now - timedelta(minutes=10), "trusted": True},
        ]

    async def search_episodes(self, *a, **k):
        return [
            {"id": 1, "occurred_at": now - timedelta(minutes=2), "role": "user", "channel": "capture",
             "trusted": True, "source": "capture", "text": "key time at 9 am", "rrf": 1.0, "score": 1.0},
            {"id": 2, "occurred_at": now - timedelta(minutes=9), "role": "user", "channel": "telegram",
             "trusted": True, "source": None, "text": "zzchatzz echo", "rrf": 0.9, "score": 0.9},
        ]


config = isolated(timezone="America/Denver", main_provider="claude_code", telegram_chat_id=123)
recorder = Recorder()
agent = Agent(ChatStore(), config, router=Router(config, factory=lambda n, c, b: recorder),
              embedder=NoEmbedder())
asyncio.run(agent.answer("what did I just capture?"))
check("a capture from inside the chat window stays in RECALL", "key time at 9 am" in recorder.prompts[0], True)
check("a chat episode CONVERSATION already shows is still dropped", "zzchatzz" in recorder.prompts[0], False)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("agent: today is Landen's today, and the prompt says so")
