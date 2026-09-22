"""What she is allowed to do right now.

Two separate questions, deliberately not conflated:

* **May she speak?** Quiet hours are midnight to 6:30 AM. This gates *outbound
  messages*, not job execution -- the 12:15 AM reflection has to run inside
  quiet hours, it just must not text him while it does. Conflating the two
  would mean either a silent night or no nightly rebuild.

* **Can she afford it?** The free tiers have daily ceilings, and the failure
  mode to avoid is spending them on scheduled work and having nothing left when
  he actually asks something. So the budget applies to *scheduled* work only.
  A message Landen sends is always answered; rationing his own questions would
  be the wrong thing to protect.

When it does hold something back it says so. A brief that silently did not
arrive is indistinguishable from a brief that had nothing to say, and he would
stop trusting either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sloane.config import Settings, settings as default_settings
from sloane.memory.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Governor:
    def __init__(self, store: Store, config: Settings | None = None) -> None:
        self._store = store
        self._config = config or default_settings()

    # -- time -----------------------------------------------------------------

    def _local(self, now: datetime | None = None) -> datetime:
        zone = ZoneInfo(self._config.timezone)
        return (now or datetime.now(zone)).astimezone(zone)

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """Midnight to 6:30 AM local, inclusive of the start, exclusive of the end."""
        local = self._local(now)
        start = time(self._config.quiet_start_hour, 0)
        end = time(self._config.quiet_end_hour, self._config.quiet_end_minute)
        current = local.time()
        if start <= end:
            return start <= current < end
        # A window that wraps midnight (e.g. 22:00-06:30).
        return current >= start or current < end

    def may_send(self, now: datetime | None = None) -> Decision:
        if self.in_quiet_hours(now):
            end = f"{self._config.quiet_end_hour}:{self._config.quiet_end_minute:02d}"
            return Decision(False, f"quiet hours until {end}")
        return Decision(True)

    # -- spend ----------------------------------------------------------------

    async def may_spend(self, purpose: str = "reply") -> Decision:
        """Whether a *scheduled* call should go ahead.

        A read failure here allows the call. Refusing to work because the
        accounting is unreadable would turn a bookkeeping problem into a missed
        morning brief, which is the wrong trade.
        """
        try:
            used = await self._store.usage_today()
        except Exception as exc:  # noqa: BLE001
            log.warning("usage unreadable, allowing the call: %s", exc)
            return Decision(True)

        if purpose == "bulk":
            spent, cap, label = used.get("bulk", 0), self._config.daily_bulk_budget, "bulk"
        else:
            spent = sum(v for k, v in used.items() if k != "bulk")
            cap, label = self._config.daily_job_budget, "scheduled"

        if spent >= cap:
            return Decision(
                False,
                f"today's {label} budget is spent ({spent}/{cap}); "
                "deferring so there is headroom left for anything you ask directly",
            )
        return Decision(True)

    async def may_run(
        self, *, sends_message: bool, purpose: str = "reply", now: datetime | None = None
    ) -> Decision:
        """The combined gate a scheduled job asks before doing anything."""
        if sends_message:
            speaking = self.may_send(now)
            if not speaking:
                return speaking
        return await self.may_spend(purpose)
