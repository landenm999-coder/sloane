"""APScheduler, driven by the `jobs` table.

The cron expressions in `jobs` are written as wall-clock times in Parker --
`35 6 * * *` means 6:35 AM there, not in UTC -- so the scheduler is built in
the configured timezone and they mean what they say across daylight saving.

Three behaviours that matter more than they look:

* **A job that raises never takes the scheduler down.** Every run is wrapped,
  and the outcome lands in `jobs.last_status` / `last_error` so a failure is
  visible in /jobs rather than being a brief that simply stopped coming.
* **Late is fine, very late is not.** A restart at 6:50 still sends the 6:35
  brief -- thirty minutes late is still useful. A restart at noon does not
  send it at all, because a morning brief at lunchtime is noise.
* **No stacking.** If a run is still going when the next one fires, the new one
  is skipped rather than doubled.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sloane.jobs.briefs import HANDLERS, JobContext, JobResult
from sloane.router import SCHEDULED

log = logging.getLogger(__name__)

# How late a missed run may still fire. Past this, it is dropped.
MISFIRE_GRACE_SECONDS = 30 * 60


_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_DOW_PART = re.compile(r"^(\*|\d+)(?:-(\d+))?(?:/(\d+))?$")


def _cron_weekdays(field: str) -> str:
    """Cron's day-of-week field as an explicit list of names.

    Cron counts Sunday = 0 (and 7); APScheduler counts Monday = 0. Expanding
    every number, range and step to names -- `1-5` to mon,...,fri, `*/2` to
    sun,tue,thu,sat, `1-5/2` to mon,wed,fri -- means APScheduler never has to
    interpret a weekday number at all. Names are accepted anywhere cron allows.
    """
    if field == "*":
        return field
    days: set[int] = set()
    for part in field.lower().split(","):
        # Names become cron numbers first, so "1,fri" and "sun-sat" are read
        # the same way as "1,5" and "0-6" rather than half in each numbering.
        for n, day in enumerate(_DOW_NAMES):
            part = part.replace(day, str(n))
        found = _DOW_PART.match(part)
        if not found:
            raise ValueError(f"unreadable day of week {part!r}")
        first, last, step = found.groups()
        lo, hi = (0, 6) if first == "*" else (int(first), int(last) if last else int(first))
        if step and first != "*" and not last:
            hi = 7  # cron's `a/s` runs from a to the end of the week
        if not (0 <= lo <= 7 and 0 <= hi <= 7) or lo > hi:
            raise ValueError(f"day of week {part!r} is out of range")
        days.update(n % 7 for n in range(lo, hi + 1, int(step or 1)))
    return ",".join(_DOW_NAMES[n] for n in sorted(days))


def crontab_trigger(expr: str, zone) -> CronTrigger:  # noqa: ANN001 - a tzinfo
    """A crontab line, read the way cron reads it.

    APScheduler numbers weekdays from Monday = 0, so `from_crontab("... 1-5")`
    fires Tuesday to Saturday -- the pre- and post-shift briefs would skip
    Monday and turn up on Saturday. The weekday field is rewritten as names
    first, which fixes every row in the jobs table, including ones already
    deployed.
    """
    fields = expr.split()
    if len(fields) == 5:
        # Cron fires when *either* day field matches if both are restricted;
        # APScheduler requires both. Rather than silently differ, refuse.
        if fields[2] != "*" and fields[4] != "*":
            raise ValueError("day-of-month and day-of-week both set; cron and "
                             "APScheduler disagree on what that means")
        fields[4] = _cron_weekdays(fields[4])
        expr = " ".join(fields)
    return CronTrigger.from_crontab(expr, timezone=zone)


class Scheduler:
    def __init__(self, ctx: JobContext) -> None:
        self._ctx = ctx
        self._zone = ZoneInfo(ctx.config.timezone)
        self._sched = AsyncIOScheduler(timezone=self._zone)
        self.registered: list[str] = []
        self.skipped: list[str] = []

    async def start(self) -> None:
        rows = await self._ctx.store.jobs()
        for row in rows:
            name = row["name"]
            if not row.get("enabled", True):
                self.skipped.append(f"{name} (disabled)")
                continue
            if name not in HANDLERS:
                # A row with no handler is a migration ahead of the code, or a
                # typo. Either way it should be loud, not silently never run.
                log.warning("job %s is in the table but has no handler", name)
                self.skipped.append(f"{name} (no handler)")
                continue
            try:
                trigger = crontab_trigger(row["cron"], self._zone)
            except ValueError as exc:
                log.error("job %s has an invalid cron %r: %s", name, row["cron"], exc)
                self.skipped.append(f"{name} (bad cron)")
                continue

            self._sched.add_job(
                self.run,
                trigger=trigger,
                args=[name],
                id=name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
            )
            self.registered.append(name)

        self._sched.start()
        log.info("scheduler started: %s", ", ".join(self.registered) or "nothing")
        if self.skipped:
            log.warning("not scheduled: %s", ", ".join(self.skipped))

    async def run(self, name: str, now: datetime | None = None) -> JobResult:
        """Run one job and record the outcome. Never raises."""
        handler = HANDLERS.get(name)
        if handler is None:
            return JobResult(name, ran=False, reason="no such job")

        scheduled = SCHEDULED.set(True)
        try:
            result = await handler(self._ctx, now)
        except Exception as exc:  # noqa: BLE001 - a job must never kill the scheduler
            log.exception("job %s failed", name)
            result = JobResult(name, ran=False, reason=f"failed: {exc}")
            status = "failed"
        else:
            status = ("partial" if result.partial else "ok") if result.ran else "deferred"
        finally:
            SCHEDULED.reset(scheduled)

        try:
            await self._ctx.store.mark_job(
                name, status=status, error=None if status == "ok" else result.reason
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a job
            log.warning("could not record job %s: %s", name, exc)
        return result

    async def catch_up(self, name: str, max_age: timedelta, now: datetime | None = None) -> JobResult | None:
        """Run `name` now if it never ran, last failed, or last ran over `max_age` ago.

        A fresh install, or a box that was off, would otherwise know nothing
        new from Canvas or the calendar until the job's next slot -- up to four
        hours in which "what's due tomorrow?" has nothing to go on.
        """
        now = now or datetime.now(self._zone)
        row = next((r for r in await self._ctx.store.jobs() if r["name"] == name), None)
        if row is None or not row.get("enabled", True) or name not in HANDLERS:
            return None
        last = row.get("last_run_at")
        if last is not None and row.get("last_status") in ("ok", "partial") and now - last < max_age:
            return None
        log.info("catching up on %s (last run: %s)", name, last or "never")
        return await self.run(name)

    def next_runs(self) -> dict[str, datetime | None]:
        return {job.id: job.next_run_time for job in self._sched.get_jobs()}

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
