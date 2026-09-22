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
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sloane.jobs.briefs import HANDLERS, JobContext, JobResult
from sloane.router import SCHEDULED

log = logging.getLogger(__name__)

# How late a missed run may still fire. Past this, it is dropped.
MISFIRE_GRACE_SECONDS = 30 * 60


_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DOW_NUMBER = re.compile(r"(?<![/\d])(\d+)")


def crontab_trigger(expr: str, zone) -> CronTrigger:  # noqa: ANN001 - a tzinfo
    """A crontab line, read the way cron reads it.

    APScheduler numbers weekdays from Monday = 0, so `from_crontab("... 1-5")`
    fires Tuesday to Saturday -- the pre- and post-shift briefs would skip
    Monday and turn up on Saturday. Standard cron numbers from Sunday = 0 (and
    7). Translating the numbers to names removes the ambiguity for every row in
    the jobs table, including ones already deployed. Step values (`*/2`) are
    left alone.
    """
    fields = expr.split()
    if len(fields) == 5:
        def name(match: re.Match) -> str:
            n = int(match.group(1))
            if n > 7:
                raise ValueError(f"day of week {n} is out of range")
            return _DOW_NAMES[n]

        fields[4] = _DOW_NUMBER.sub(name, fields[4])
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
            status = "ok" if result.ran else "deferred"
        finally:
            SCHEDULED.reset(scheduled)

        try:
            await self._ctx.store.mark_job(
                name, status=status, error=None if status == "ok" else result.reason
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a job
            log.warning("could not record job %s: %s", name, exc)
        return result

    def next_runs(self) -> dict[str, datetime | None]:
        return {job.id: job.next_run_time for job in self._sched.get_jobs()}

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
