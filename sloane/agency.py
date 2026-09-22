"""Doing things, with permission.

Every action Sloane can take -- a reminder today, an email later -- goes through
`Agency.propose()`, in this order:

  1. **Hard lines.** `ensure_allowed()` runs first, in code -- on the action as
     named *and* on what it really is (a reply to a teacher is contacting school
     staff). A hard-line pair is refused and recorded as refused; it never
     becomes something Landen could approve by mis-tapping, and no streak can
     ever unlock it.
  2. **Registered?** An action nobody registered an executor for is refused.
     There is no generic "do whatever the model asked" path.
  3. **Trusted?** If this exact (action, target) pair is trusted and in date,
     it runs now and Landen is told after the fact.
  4. **Otherwise it waits** for Approve / Edit / Deny on Telegram.

The ledger moves on his decisions: a straight approval grows the streak and the
tenth in a row unlocks the pair for sixty days; an edit resets the streak; a
deny -- or /revoke -- sends the pair straight back to gated. Pairs are exact:
trusting "reply to Keegan" earns nothing for "reply to anyone".

A trusted pair's sixty days restart each time it is used, so trust lapses from
disuse rather than on a calendar -- a pair he relies on daily should not
silently re-gate mid-semester, and one he has not needed in two months should.

Nothing in here raises into the bot loop. Execution failures are recorded on
the proposal and reported.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sloane.agent import HardLineViolation, ensure_allowed
from sloane.config import Settings
from sloane.ingest import safe_field
from sloane.memory.store import Row, Store

log = logging.getLogger(__name__)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

DECISIONS = {"a": "approve", "e": "edit", "d": "deny"}


@dataclass(frozen=True)
class ActionType:
    """One thing Sloane may do. Registered explicitly, never inferred."""

    name: str
    # (target, payload) -> the one-line description he approves.
    preview: Callable[[str, dict], str]
    # (target, payload) -> a short result, or raise.
    execute: Callable[[str, dict], Awaitable[str]]
    # (payload, his replacement text) -> the edited payload.
    revise: Callable[[dict, str], dict]
    # What the action *really* is, for the hard-line check. A reply to a
    # teacher is "contact school_staff" whatever the action is called, and a
    # hard line that only matched labels could be walked around by renaming.
    really: Callable[[str, dict], tuple[str, str] | None] | None = None


@dataclass
class Outcome:
    status: str  # refused | pending | executed | failed | denied | editing | stale | ignored
    message: str
    proposal: Row | None = None


# Asks Landen to decide: sends the preview with Approve / Edit / Deny and
# returns the Telegram message id, or None if it could not be sent.
AskFn = Callable[[Row], Awaitable[int | None]]
# Tells Landen something happened. Best effort.
TellFn = Callable[[str], Awaitable[None]]


def callback_data(proposal_id: str, decision: str) -> str:
    """Compact and well under Telegram's 64-byte limit: 'p:<uuid>:a'."""
    return f"p:{proposal_id}:{decision}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """(proposal_id, decision) for well-formed data, else None.

    Callback data comes back from Telegram, not from us; it is validated like
    any other input. A malformed or forged value decides nothing.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "p":
        return None
    proposal_id, letter = parts[1], parts[2]
    if not _UUID.match(proposal_id) or letter not in DECISIONS:
        return None
    return proposal_id, DECISIONS[letter]


class Agency:
    def __init__(
        self,
        store: Store,
        config: Settings,
        *,
        ask: AskFn,
        tell: TellFn,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._ask = ask
        self._tell = tell
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._actions: dict[str, ActionType] = {}

    def register(self, action: ActionType) -> None:
        self._actions[action.name] = action

    # -- the ledger -------------------------------------------------------------

    async def _ledger(self, action: str, target: str, outcome: str) -> None:
        try:
            await self._store.record_trust(
                action, target, outcome, now=self._now(),
                unlock_after=self._config.trust_unlock_streak,
                decay_days=self._config.trust_decay_days,
            )
        except Exception:  # noqa: BLE001 - bookkeeping never blocks the action
            log.exception("trust ledger not updated for %s/%s", action, target)

    async def _trusted(self, action: str, target: str) -> bool:
        now = self._now()
        # Lapsed pairs are re-gated lazily, here, so a stale unlock can never
        # authorise anything even if nothing else has swept it.
        for row in await self._store.lapsed_trust(now):
            await self._ledger(row["action"], row["target"], "expired")
        return await self._store.is_trusted(action, target, now)

    # -- proposing ----------------------------------------------------------------

    async def propose(self, action: str, target: str, payload: dict) -> Outcome:
        action = action.strip().lower()
        target = target.strip().lower()

        kind = self._actions.get(action)
        try:
            ensure_allowed(action, target)
            if kind is not None and kind.really is not None:
                underlying = kind.really(target, payload)
                if underlying is not None:
                    ensure_allowed(*underlying)
        except HardLineViolation as exc:
            row = await self._store.create_proposal(
                action=action, target=target, preview=str(exc), payload={},
                status="refused", result="hard line",
            )
            log.warning("refused a hard-line action: %s", exc)
            return Outcome("refused", f"I won't do that: {exc}.", row)

        if kind is None:
            row = await self._store.create_proposal(
                action=action, target=target, preview=f"unregistered action {action}",
                payload={}, status="refused", result="no such action",
            )
            return Outcome("refused", f"I don't have a way to '{action}'.", row)

        preview = safe_field(kind.preview(target, payload), limit=1000)

        if await self._trusted(action, target):
            row = await self._store.create_proposal(
                action=action, target=target, preview=preview, payload=payload,
                status="approved", auto=True,
            )
            outcome = await self._execute(row, kind)
            if outcome.status == "executed":
                await self._ledger(action, target, "used")
                await self._tell(f"Done without asking (you trust this): {preview}")
            return outcome

        row = await self._store.create_proposal(
            action=action, target=target, preview=preview, payload=payload,
        )
        message_id = await self._ask(row)
        if message_id is not None:
            await self._store.set_proposal_message(str(row["id"]), message_id)
        return Outcome("pending", f"Waiting for your OK: {preview}", row)

    # -- deciding ---------------------------------------------------------------------

    async def decide(self, proposal_id: str, decision: str) -> Outcome:
        """Apply one Approve / Edit / Deny. Owner checks happen in the caller."""
        if decision == "approve":
            row = await self._store.transition_proposal(
                proposal_id, from_status="pending", to_status="approved"
            )
            if row is None:
                return Outcome("stale", "That one was already decided.")
            kind = self._actions.get(row["action"])
            if kind is None:
                await self._store.finish_proposal(proposal_id, ok=False, result="no such action")
                return Outcome("failed", "I no longer know how to do that.", row)
            # An approval of something he had to edit is not a clean one: the
            # P4 gate is ten sent *without* edits.
            if not row["edited"]:
                await self._ledger(row["action"], row["target"], "clean")
            return await self._execute(row, kind)

        if decision == "deny":
            row = await self._store.transition_proposal(
                proposal_id, from_status="pending", to_status="denied"
            )
            if row is None:
                return Outcome("stale", "That one was already decided.")
            await self._ledger(row["action"], row["target"], "reversal")
            return Outcome("denied", "Dropped it.", row)

        if decision == "edit":
            row = await self._store.transition_proposal(
                proposal_id, from_status="pending", to_status="editing"
            )
            if row is None:
                return Outcome("stale", "That one was already decided.")
            await self._ledger(row["action"], row["target"], "edited")
            return Outcome(
                "editing",
                "Send me the replacement as your next message, or /cancel to keep the original.",
                row,
            )

        return Outcome("ignored", "Unknown decision.")

    async def submit_edit(self, text: str) -> Outcome | None:
        """His next message while a proposal is being edited. None if none is."""
        row = await self._store.editing_proposal()
        if row is None:
            return None
        kind = self._actions.get(row["action"])
        if kind is None:
            return Outcome("failed", "I no longer know how to do that.", row)
        payload = kind.revise(dict(row["payload"]), text)
        preview = safe_field(kind.preview(row["target"], payload), limit=1000)
        revised = await self._store.revise_proposal(str(row["id"]), preview=preview, payload=payload)
        if revised is None:
            return Outcome("stale", "That one was already decided.")
        message_id = await self._ask(revised)
        if message_id is not None:
            await self._store.set_proposal_message(str(revised["id"]), message_id)
        return Outcome("pending", f"Updated. Waiting for your OK: {preview}", revised)

    async def cancel_edit(self) -> Outcome | None:
        row = await self._store.editing_proposal()
        if row is None:
            return None
        restored = await self._store.transition_proposal(
            str(row["id"]), from_status="editing", to_status="pending"
        )
        if restored is None:
            return Outcome("stale", "That one was already decided.")
        message_id = await self._ask(restored)
        if message_id is not None:
            await self._store.set_proposal_message(str(restored["id"]), message_id)
        return Outcome("pending", "Kept the original. Waiting for your OK.", restored)

    async def revoke(self, action: str, target: str) -> Outcome:
        await self._ledger(action.strip().lower(), target.strip().lower(), "reversal")
        return Outcome("denied", f"{action} → {target} is back to asking first.")

    # -- executing --------------------------------------------------------------------

    async def _execute(self, row: Row, kind: ActionType) -> Outcome:
        proposal_id = str(row["id"])
        try:
            result = await kind.execute(row["target"], dict(row["payload"]))
        except Exception as exc:  # noqa: BLE001 - a failed action is reported, not raised
            log.exception("action %s/%s failed", row["action"], row["target"])
            await self._store.finish_proposal(proposal_id, ok=False, result=str(exc))
            return Outcome("failed", f"That didn't work: {exc}", row)
        await self._store.finish_proposal(proposal_id, ok=True, result=result or "done")
        return Outcome("executed", result or "Done.", row)


def reminder_action(send: Callable[[str], Awaitable[None]]) -> ActionType:
    """The one built-in action: a reminder to Landen, and only to Landen.

    It exists so the whole flow -- propose, approve, trust, decay -- runs end to
    end before anything with consequences (email) is wired to it. It touches
    nothing outside his own Telegram chat.
    """

    async def execute(target: str, payload: dict) -> str:
        if target != "self":
            raise ValueError("reminders only go to you")
        await send(f"Reminder: {payload.get('text', '')}")
        return "reminder sent"

    return ActionType(
        name="remind",
        preview=lambda target, payload: f"Send you a reminder: {payload.get('text', '')}",
        execute=execute,
        revise=lambda payload, text: {**payload, "text": text},
    )
