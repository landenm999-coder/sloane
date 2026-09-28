"""P2: governor, briefs and scheduler, against a live Postgres. No model.

The agent is a stand-in that records what it was asked, so these tests cover
the plumbing -- who is allowed to speak, when, what gets recorded -- without
spending a model call or depending on what a model says.

DESTRUCTIVE: resets the jobs and usage tables. Use a throwaway database.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.contract import Reply
from sloane.jobs.briefs import HANDLERS, SPEAKING, JobContext
from sloane.jobs.governor import Governor
from sloane.jobs.scheduler import Scheduler
from sloane.memory.store import Store

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=DEN)


class FakeAgent:
    """Answers instantly and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def answer(self, question, *, channel="", today=None, ingested="", persist=True):
        self.asked.append(channel)
        self.kept = persist
        return Reply(speech="Two things are due today.", detail="- Stat p. 214")


class Outbox:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Reply] = []
        self._fail = fail

    async def __call__(self, reply: Reply) -> None:
        if self._fail:
            raise RuntimeError("telegram is down")
        self.sent.append(reply)


async def main() -> None:
    config = isolated(
        database_url=os.environ["DATABASE_URL"],
        timezone="America/Denver",
        daily_job_budget=3,
    )

    async with Store(config) as store:
        await store._exec("truncate usage_log restart identity")
        await store._exec(
            "update jobs set runs = 0, last_run_at = null, last_status = null, "
            "last_error = null, enabled = true"
        )

        def context(outbox=None, agent=None):
            return JobContext(
                store=store, agent=agent or FakeAgent(),
                governor=Governor(store, config), config=config, send=outbox,
            )

        # -- quiet hours gate speaking, not running --------------------------
        gov = Governor(store, config)
        check("00:15 is quiet", gov.in_quiet_hours(local(0, 15)), True)
        check("06:29 is still quiet", gov.in_quiet_hours(local(6, 29)), True)
        check("06:30 is not", gov.in_quiet_hours(local(6, 30)), False)
        check("the 6:35 brief is allowed to speak", bool(gov.may_send(local(6, 35))), True)
        check("the 10 PM wrap is allowed to speak", bool(gov.may_send(local(22))), True)

        outbox, agent = Outbox(), FakeAgent()
        held = await HANDLERS["morning_brief"](context(outbox, agent), local(3))
        check("a brief at 3 AM does not run", held.ran, False)
        check("and says why", "quiet hours" in held.reason, True)
        check("nothing was sent", outbox.sent, [])
        check("and the model was never called", agent.asked, [])

        # -- reflection runs inside quiet hours, and never speaks ------------
        outbox = Outbox()
        night = await HANDLERS["reflection"](context(outbox), local(0, 15))
        check("reflection runs at 00:15", night.ran, True)
        check("and sends nothing", outbox.sent, [])
        check("and reports what it did", "open loops" in night.reason, True)
        check("reflection is not a speaking job", "reflection" in SPEAKING, False)
        check("nor is the sync", "entity_sync" in SPEAKING, False)

        # -- a brief in waking hours is delivered ----------------------------
        outbox, agent = Outbox(), FakeAgent()
        morning = await HANDLERS["morning_brief"](context(outbox, agent), local(6, 35))
        check("the morning brief runs", morning.ran, True)
        check("and is delivered", len(outbox.sent), 1)
        check("with the agent's reply", outbox.sent[0].speech, "Two things are due today.")
        check("tagged as a job turn, not a user turn", agent.asked, ["job:morning_brief"])

        # -- VOICE_BRIEFS routes a brief to the voice sender ----------------
        spoken_out = Outbox()
        voiced = JobContext(store=store, agent=FakeAgent(), governor=Governor(store, config),
                            config=isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                                            daily_job_budget=3, voice_briefs="morning_brief"),
                            send=Outbox(), speak=spoken_out)
        await HANDLERS["morning_brief"](voiced, local(6, 35))
        check("a voice brief goes to the voice sender", len(spoken_out.sent), 1)
        await HANDLERS["wrap"](voiced, local(22))
        check("others stay text", len(spoken_out.sent), 1)

        # -- a delivery failure is recorded, not raised ----------------------
        broken = await HANDLERS["wrap"](context(Outbox(fail=True)), local(22))
        check("a dead Telegram does not raise out of the job", broken.ran, True)
        check("but it is not counted as sent", broken.sent, False)
        check("and says so", "delivery failed" in broken.reason, True)

        # -- the budget defers scheduled work, with a reason -----------------
        for _ in range(20):
            await store.log_usage(provider="claude_code", purpose="reply")
            await store.log_usage(provider="groq", purpose="speak")
        check("his own chats and voice notes never spend the job budget",
              bool(await gov.may_spend("reply")), True)
        for _ in range(3):
            await store.log_usage(provider="claude_code", purpose="job")
        spent = await gov.may_spend("reply")
        check("at the cap, scheduled work is refused", bool(spent), False)
        check("and the refusal explains itself", "budget is spent" in spent.reason, True)
        over = await HANDLERS["pre_shift"](context(Outbox()), local(14, 45))
        check("so the pre-shift brief defers", over.ran, False)
        check("with the budget as the reason", "budget" in over.reason, True)
        check(
            "bulk has its own ceiling and is unaffected",
            bool(await gov.may_spend("bulk")), True,
        )
        await store._exec("truncate usage_log restart identity")

        # -- unreadable accounting must not cost a brief ---------------------
        class BrokenStore:
            async def usage_today(self):
                raise RuntimeError("supabase hiccup")

        check(
            "if usage cannot be read, the call is allowed",
            bool(await Governor(BrokenStore(), config).may_spend()), True,
        )

        # -- a job's model calls are accounted as the job's ------------------
        from sloane.router import SCHEDULED, Router

        class Echo:
            async def complete(self, system, prompt, *, max_tokens=0):
                from sloane.providers.base import Completion, Usage
                return Completion(text="ok", usage=Usage(provider="claude_code"))

        purposes = []

        async def sink(usage, purpose, ok, error, degraded):
            purposes.append(purpose)

        rt = Router(config, usage_sink=sink, factory=lambda n, c, b: Echo())
        await rt.reply("s", "p")
        token = SCHEDULED.set(True)
        await rt.reply("s", "p")
        SCHEDULED.reset(token)

        class Asker:
            async def answer(self, question, *, channel="", today=None, ingested=""):
                await rt.reply("s", "p")
                return Reply(speech="ok", detail="ok")

        runner = Scheduler(context(Outbox(), Asker()))
        await runner.run("wrap", local(22))
        check("his question is 'reply'; a job's call is 'job'", purposes, ["reply", "job", "job"])

        # -- she thinks: one thing worth saying, or nothing ----------------------
        await store._exec("update jobs set runs = 0, last_run_at = null")
        await store._exec("truncate usage_log restart identity")

        class Quiet(FakeAgent):
            async def answer(self, question, *, channel="", today=None, ingested="", persist=True):
                self.asked.append(channel)
                self.kept = persist
                return Reply(speech="NOTHING.", detail="NOTHING.")

        friday = datetime(2026, 9, 25, tzinfo=DEN)  # a Friday
        thinker, outbox = FakeAgent(), Outbox()
        school = await HANDLERS["think"](context(outbox, thinker), friday.replace(hour=10, minute=25))
        check("not in class", (school.ran, school.reason, thinker.asked), (False, "he's at school", []))
        await store._exec("delete from shifts where starts_at::date = '2026-09-25'")
        await store.add_shift(friday.replace(hour=15), friday.replace(hour=19))
        working = await HANDLERS["think"](context(outbox, thinker), friday.replace(hour=16, minute=25))
        check("not on a shift", (working.ran, working.reason), (False, "he's at work"))
        quiet = Quiet()
        nothing = await HANDLERS["think"](context(outbox, quiet), friday.replace(hour=20, minute=25))
        check("nothing worth saying: nothing sent", (nothing.ran, nothing.sent, outbox.sent, nothing.reason), (True, False, [], "nothing worth saying"))
        check("and a silent turn is no memory", getattr(quiet, "kept", None), False)
        saturday = friday + timedelta(days=1)
        spoke = await HANDLERS["think"](context(outbox, thinker), saturday.replace(hour=12, minute=25))
        check("something worth saying is said, once", (spoke.sent, [r.speech for r in outbox.sent]),
              (True, ["Two things are due today."]))
        check("as a job turn", thinker.asked, ["job:think"])
        off = await HANDLERS["think"](JobContext(store=store, agent=thinker, governor=Governor(store, config),
                                                 config=isolated(database_url=os.environ["DATABASE_URL"], think=False),
                                                 send=outbox), friday.replace(hour=20, minute=25))
        check("THINK=false turns it off", (off.ran, off.reason), (False, "THINK is off"))
        check("it is a speaking job's cousin, not in SPEAKING (it may stay silent)", "think" in SPEAKING, False)
        await store._exec("delete from shifts where starts_at::date = '2026-09-25'")
        await store._exec("truncate usage_log restart identity")

        # -- the scheduler -----------------------------------------------------
        sched = Scheduler(context(Outbox()))
        await sched.start()
        check(
            "every seeded job is scheduled",
            sorted(sched.registered),
            sorted(["morning_brief", "pre_shift", "post_shift", "wrap",
                    "reflection", "entity_sync", "inbox", "reminders", "watchdog",
                    "weekly_review", "backup", "heartbeat", "learn", "think"]),
        )
        nxt = sched.next_runs()

        # A stale or missing sync is caught up at startup, a fresh one isn't.
        synced: list[str] = []
        real_sync = HANDLERS["entity_sync"]

        async def fake_sync(ctx, now=None):
            from sloane.jobs.briefs import JobResult
            synced.append("sync")
            return JobResult("entity_sync", ran=True)

        HANDLERS["entity_sync"] = fake_sync
        try:
            await store._exec("update jobs set last_run_at = null, last_status = null where name = 'entity_sync'")
            await sched.catch_up("entity_sync", timedelta(hours=4))
            check("never synced: it runs at startup", synced, ["sync"])
            await sched.catch_up("entity_sync", timedelta(hours=4))
            check("just synced: not again", synced, ["sync"])
            await store._exec("update jobs set last_run_at = now() - interval '5 hours' where name = 'entity_sync'")
            await sched.catch_up("entity_sync", timedelta(hours=4))
            check("five hours old: runs again", synced, ["sync", "sync"])
            await store._exec("update jobs set last_status = 'failed' where name = 'entity_sync'")
            await sched.catch_up("entity_sync", timedelta(hours=4))
            check("last one failed: runs again", synced, ["sync", "sync", "sync"])
            check("an unknown job is left alone", await sched.catch_up("nope", timedelta(hours=4)), None)
        finally:
            HANDLERS["entity_sync"] = real_sync

        # Cron's weekday numbers, not APScheduler's: 1-5 is Monday to Friday.
        from sloane.jobs.scheduler import crontab_trigger

        def fires(expr, n=7):
            trig, at, days = crontab_trigger(expr, DEN), datetime(2026, 9, 20, tzinfo=DEN), []
            for _ in range(n):
                at = trig.get_next_fire_time(None, at)
                days.append(at.strftime("%a"))
                at = at + timedelta(minutes=1)
            return days

        check("1-5 is Monday to Friday", fires("45 14 * * 1-5", 5), ["Mon", "Tue", "Wed", "Thu", "Fri"])
        check("0 and 7 are both Sunday", (fires("0 9 * * 0", 1), fires("0 9 * * 7", 1)), (["Sun"], ["Sun"]))
        check("names still work", fires("0 9 * * sat", 1), ["Sat"])
        check("hour steps are untouched", fires("0 */12 * * *", 2), ["Sun", "Sun"])
        check("a range from Sunday works", fires("0 9 * * 0-4", 5), ["Sun", "Mon", "Tue", "Wed", "Thu"])
        check("a weekday step is cron's", fires("0 9 * * 1-5/2", 3), ["Mon", "Wed", "Fri"])
        check("*/2 counts from Sunday", fires("0 9 * * */2", 4), ["Sun", "Tue", "Thu", "Sat"])
        check("7 is Sunday in a range", fires("0 9 * * 5-7", 3), ["Sun", "Fri", "Sat"])
        check("a list mixes", fires("0 9 * * 0,3,6", 3), ["Sun", "Wed", "Sat"])
        check("names and numbers mix in cron's numbering", fires("0 9 * * 1,fri", 2), ["Mon", "Fri"])
        check("sun-sat is every day", len(set(fires("0 9 * * sun-sat", 7))), 7)
        try:
            crontab_trigger("0 9 1 * 1", DEN)
            FAILURES.append("both day fields set should be refused, not silently narrowed")
        except ValueError:
            pass
        check(
            "the morning brief fires at 6:35 local",
            nxt["morning_brief"].astimezone(DEN).strftime("%H:%M"), "06:35",
        )
        check(
            "the pre-shift brief fires at 2:45 local",
            nxt["pre_shift"].astimezone(DEN).strftime("%H:%M"), "14:45",
        )
        check(
            "and only on a weekday",
            nxt["pre_shift"].astimezone(DEN).weekday() < 5, True,
        )
        sched.stop()

        # A run is recorded, whatever happens to it.
        await sched.run("reflection", now=local(0, 15))
        rec = {j["name"]: j for j in await store.jobs()}
        check("a successful run is recorded ok", rec["reflection"]["last_status"], "ok")
        check("and counted", rec["reflection"]["runs"], 1)

        await sched.run("morning_brief", now=local(3))
        rec = {j["name"]: j for j in await store.jobs()}
        check("a held-back run is recorded as deferred", rec["morning_brief"]["last_status"], "deferred")
        check("with its reason kept", "quiet hours" in (rec["morning_brief"]["last_error"] or ""), True)

        # A job that raises must not take the scheduler with it.
        async def explode(ctx, now=None):
            raise RuntimeError("canvas returned garbage")

        HANDLERS["wrap"], original = explode, HANDLERS["wrap"]
        try:
            failed = await sched.run("wrap", now=local(22))
        finally:
            HANDLERS["wrap"] = original
        check("a raising job returns instead of propagating", failed.ran, False)
        rec = {j["name"]: j for j in await store.jobs()}
        check("and is recorded as failed", rec["wrap"]["last_status"], "failed")
        check("with the error kept", "canvas returned garbage" in rec["wrap"]["last_error"], True)

        # Rows the code cannot run are skipped loudly, never silently.
        await store._exec("insert into jobs (name, cron) values ('ghost_job', '0 9 * * *') on conflict do nothing")
        await store._exec("update jobs set enabled = false where name = 'wrap'")
        await store._exec("insert into jobs (name, cron) values ('bad_cron', 'not a cron') on conflict do nothing")
        HANDLERS["bad_cron"] = original
        try:
            picky = Scheduler(context(Outbox()))
            await picky.start()
            picky.stop()
        finally:
            del HANDLERS["bad_cron"]
        skipped = " ".join(picky.skipped)
        check("a job with no handler is skipped", "ghost_job (no handler)" in skipped, True)
        check("a disabled job is skipped", "wrap (disabled)" in skipped, True)
        check("a malformed cron is skipped, not fatal", "bad_cron (bad cron)" in skipped, True)
        check("and the good ones still register", "morning_brief" in picky.registered, True)

        await store._exec("delete from jobs where name in ('ghost_job', 'bad_cron')")
        await store._exec("update jobs set enabled = true")


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("jobs: quiet hours, budget, delivery, recording and scheduling all pass")
