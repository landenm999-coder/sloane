"""store.py against a live Postgres. Schema, decay math, idempotence, timezones.

DESTRUCTIVE. Point DATABASE_URL at a throwaway database with sql/001_init.sql
applied; this truncates every data table before it runs. It resets rather than
assuming a fresh database, because the alternative is a second run failing with
ten confusing off-by-one counts instead of one clear message.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import Settings
from sloane.memory.store import Store, as_vector, remember

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def close_to(label: str, got: float, want: float, tol: float = 1e-4) -> None:
    if got is None or abs(got - want) > tol:
        FAILURES.append(f"{label}: got {got!r}, want ~{want} (+-{tol})")


def vec(x: float, y: float) -> list[float]:
    """A 384-dim vector living in the first two dimensions, so cosine is exact."""
    return [x, y] + [0.0] * 382


UTC = timezone.utc

# Everything the suite writes. trust and jobs are seeded by the schema, so they
# are restored rather than emptied.
DATA_TABLES = (
    "messages", "episodes", "working_set", "assignments", "shifts",
    "commitments", "people", "courses", "state", "usage_log",
)


async def reset(store: Store) -> None:
    await store._exec(f"truncate {', '.join(DATA_TABLES)} restart identity cascade")
    await store._exec("update jobs set runs = 0, last_run_at = null, last_status = null")


async def main() -> None:
    config = Settings(
        database_url=os.environ["DATABASE_URL"],
        timezone="America/Denver",
        recency_half_life_days=14.0,
    )
    async with Store(config) as store:
        check("health", await store.healthy(), True)
        await reset(store)

        # -- the six hard lines are present and readable ----------------------
        lines = await store.hard_lines()
        check("six hard lines", len(lines), 6)
        check(
            "submitting schoolwork is a hard line",
            any(r["action"] == "submit" and r["target"] == "schoolwork" for r in lines),
            True,
        )
        row = await store.trust_for("place", "trade")
        check("hard line row is gated", row["state"], "gated")
        check("hard line row is flagged", row["hard_line"], True)

        # -- tier 1 -----------------------------------------------------------
        await store.put_state("work.hours", "3-7 PM Mon-Fri", category="schedule")
        await store.put_state("work.hours", "3-7 PM Mon-Fri (fixed)", category="schedule")
        state = await store.get_state()
        hours = [r for r in state if r["key"] == "work.hours"]
        check("state upsert does not duplicate", len(hours), 1)
        check("state upsert overwrites", hours[0]["value"], "3-7 PM Mon-Fri (fixed)")

        # -- tier 4: courses, then assignments hanging off them ---------------
        await store.upsert_course(name="Stat Reasoning", period=2, teacher="Austin")
        await store.upsert_course(name="Stat Reasoning", period=2, teacher="Austin")
        await store.upsert_course(name="CO History", period=3, teacher="Allen")
        courses = await store.courses()
        check("course upsert is idempotent on (semester, period)", len(courses), 2)
        stat = next(c for c in courses if c["period"] == 2)

        # Friday 2026-09-25, 23:59 local == Saturday 05:59 UTC. A UTC-naive
        # query would file this under Saturday; the timezone cast must not.
        friday_late_local = datetime(2026, 9, 26, 5, 59, tzinfo=UTC)
        await store.upsert_assignment(
            title="Stat p. 214",
            source="canvas",
            external_id="c-1",
            due_at=friday_late_local,
            course_id=str(stat["id"]),
        )
        await store.upsert_assignment(
            title="Stat p. 214 (renamed)",
            source="canvas",
            external_id="c-1",
            due_at=friday_late_local,
            course_id=str(stat["id"]),
        )
        friday = date(2026, 9, 25)
        due = await store.assignments_due(friday, friday)
        check("assignment upsert is idempotent on (source, external_id)", len(due), 1)
        check("assignment upsert updates in place", due[0]["title"], "Stat p. 214 (renamed)")
        check("late-evening local due date stays on its local day", due[0]["course"], "Stat Reasoning")
        check("the joined teacher comes through", due[0]["teacher"], "Austin")
        check(
            "the same assignment is not on the Saturday",
            await store.assignments_due(date(2026, 9, 26), date(2026, 9, 26)),
            [],
        )

        # A submitted assignment drops out of the open query but not the full one.
        await store.upsert_assignment(
            title="Done thing", source="canvas", external_id="c-2",
            due_at=friday_late_local, status="submitted", course_id=str(stat["id"]),
        )
        check("submitted work is not 'due'", len(await store.assignments_due(friday, friday)), 1)
        check(
            "include_done sees it",
            len(await store.assignments_due(friday, friday, include_done=True)),
            2,
        )

        # -- shifts: the generator must be re-runnable ------------------------
        start = datetime(2026, 9, 21, 21, 0, tzinfo=UTC)  # 3 PM Denver
        end = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)     # 7 PM Denver
        await store.add_shift(start, end)
        await store.add_shift(start, end)
        shifts = await store.shifts_between(date(2026, 9, 21), date(2026, 9, 21))
        check("shift generation is idempotent", len(shifts), 1)

        # -- people and commitments ------------------------------------------
        await store.upsert_person("Keegan", relation="friend")
        await store.upsert_person("keegan", relation="friend", email="k@example.test")
        check("person upsert is case-insensitive", len(await store.people()), 1)
        check("person upsert merges fields", (await store.people())[0]["email"], "k@example.test")

        await store.add_commitment("send Keegan the deck")
        check("commitment is open", len(await store.open_commitments()), 1)

        # -- tier 2 rebuild derives open loops from tier 4 --------------------
        await store.rebuild_working_set()
        loops = await store.get_working_set()
        titles = [w["summary"] for w in loops]
        check(
            "the open assignment became an open loop",
            any("Stat p. 214 (renamed)" in t for t in titles),
            True,
        )
        check(
            "the submitted assignment did not",
            any("Done thing" in t for t in titles),
            False,
        )
        before = len(loops)
        await store.rebuild_working_set()
        check("rebuilding twice does not duplicate loops", len(await store.get_working_set()), before)

        # -- tier 3: the decay curve -----------------------------------------
        query = vec(1.0, 0.0)
        now = datetime.now(UTC)
        # A perfect match, but 28 days old: two half-lives, so 1.0 * 0.25.
        await store.add_episode(
            "perfect but old", embedding=vec(1.0, 0.0),
            occurred_at=now - timedelta(days=28),
        )
        # A perfect match at exactly one half-life: 1.0 * 0.5.
        await store.add_episode(
            "perfect, one half-life", embedding=vec(1.0, 0.0),
            occurred_at=now - timedelta(days=14),
        )
        # A weaker match (cosine 0.8) from today: barely decayed.
        await store.add_episode(
            "weaker but fresh", embedding=vec(0.8, 0.6), occurred_at=now,
        )

        hits = await store.search_episodes(query, limit=10)
        by_text = {h["text"]: h for h in hits}

        close_to("cosine of an identical vector is 1", by_text["perfect but old"]["similarity"], 1.0)
        close_to("cosine of (0.8,0.6) against (1,0) is 0.8", by_text["weaker but fresh"]["similarity"], 0.8)

        close_to("two half-lives discount to 0.25", by_text["perfect but old"]["score"], 0.25, tol=1e-3)
        close_to("one half-life discounts to 0.5", by_text["perfect, one half-life"]["score"], 0.5, tol=1e-3)
        close_to("a fresh hit is barely discounted", by_text["weaker but fresh"]["score"], 0.8, tol=1e-3)

        # The ordering is the whole point of the decay.
        check(
            "a fresh weaker match outranks a stale perfect one",
            [h["text"] for h in hits][:3],
            ["weaker but fresh", "perfect, one half-life", "perfect but old"],
        )

        # min_score filters on the decayed score, not raw similarity.
        filtered = await store.search_episodes(query, min_score=0.4)
        check(
            "min_score cuts by decayed score",
            sorted(h["text"] for h in filtered),
            ["perfect, one half-life", "weaker but fresh"],
        )

        # An episode with no embedding must never surface in a similarity search.
        await store.add_episode("no embedding here", embedding=None)
        check(
            "unembedded episodes stay out of search",
            any(h["text"] == "no embedding here" for h in await store.search_episodes(query, limit=50)),
            False,
        )
        check(
            "but they are still recent history",
            any(r["text"] == "no embedding here" for r in await store.recent_episodes(5)),
            True,
        )

        # -- transport: the long-poll cursor ---------------------------------
        check("first offset is 1", await store.next_update_offset(), 1)
        check("a new update is accepted", await store.log_message(update_id=41, chat_id=7), True)
        check("a replayed update is rejected", await store.log_message(update_id=41, chat_id=7), False)
        check("the cursor advances past the high-water mark", await store.next_update_offset(), 42)

        # -- usage accounting ------------------------------------------------
        await store.log_usage(provider="claude_code", purpose="reply", prompt_tokens=100,
                              completion_tokens=20, latency_ms=900)
        await store.log_usage(provider="claude_code", purpose="reply", ok=False, error="down")
        await store.log_usage(provider="anthropic", purpose="reply", prompt_tokens=90,
                              completion_tokens=15, degraded_from="claude_code")
        summary = {(r["provider"], r["purpose"]): r for r in await store.usage_summary()}
        cc = summary[("claude_code", "reply")]
        check("usage counts calls", cc["calls"], 2)
        check("usage counts failures", cc["failures"], 1)
        check("usage sums prompt tokens", cc["prompt_tokens"], 100)
        check("degradation is visible in usage", summary[("anthropic", "reply")]["degraded"], 1)

        # -- jobs ------------------------------------------------------------
        jobs = await store.jobs()
        check("five jobs seeded", len(jobs), 5)
        await store.mark_job("morning_brief", status="ok")
        brief = next(j for j in await store.jobs() if j["name"] == "morning_brief")
        check("marking a job records the run", brief["runs"], 1)
        check("marking a job records the status", brief["last_status"], "ok")

        # -- invariant 5: a failing write must not propagate ------------------
        async def boom():
            raise RuntimeError("supabase hiccup")

        check("remember() swallows a failed write", await remember("test write", boom()), None)


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("store: schema, decay, idempotence, timezone and accounting all pass")
