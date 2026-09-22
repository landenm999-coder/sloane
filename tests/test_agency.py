"""P4: the trust ledger and the approval flow, against a live Postgres.

DESTRUCTIVE: clears proposals and every non-hard-line trust row.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.agency import ActionType, Agency, callback_data, parse_callback, reminder_action
from sloane.memory.store import Store

FAILURES: list[str] = []
T0 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def main() -> None:
    config = isolated(database_url=os.environ["DATABASE_URL"])
    async with Store(config) as store:
        await store._exec("truncate proposals cascade")
        await store._exec("delete from trust where not hard_line")

        clock, asked, told, ran = Clock(), [], [], []

        async def ask(row):
            asked.append(row)
            return 1000 + len(asked)

        async def tell(text):
            told.append(text)

        agency = Agency(store, config, ask=ask, tell=tell, clock=clock)
        agency.register(reminder_action(tell))

        async def note(target, payload):
            ran.append((target, payload["text"]))
            return "noted"

        agency.register(ActionType(
            name="note", preview=lambda t, p: f"note to {t}: {p['text']}",
            execute=note, revise=lambda p, text: {**p, "text": text},
        ))

        async def approve_once(target="keegan", text="hi"):
            out = await agency.propose("note", target, {"text": text})
            return await agency.decide(str(out.proposal["id"]), "approve")

        # -- nine is not ten -------------------------------------------------------
        for i in range(9):
            await approve_once(text=f"n{i}")
        first = await agency.propose("note", "keegan", {"text": "tenth"})
        check("after nine approvals it still asks", first.status, "pending")
        await agency.decide(str(first.proposal["id"]), "approve")

        # -- the tenth unlocks exactly this pair --------------------------------
        asked.clear()
        auto = await agency.propose("note", "keegan", {"text": "eleventh"})
        check("after ten clean approvals it runs without asking", auto.status, "executed")
        check("and did not ask", asked, [])
        check("and told him after the fact", any("without asking" in t for t in told), True)
        check("the action really ran", ran[-1], ("keegan", "eleventh"))

        other = await agency.propose("note", "anyone", {"text": "x"})
        check("trusting keegan earns nothing for anyone", other.status, "pending")
        near = await agency.propose("note", "keegan2", {"text": "x"})
        check("nor for a lookalike target", near.status, "pending")
        diff = await agency.propose("remind", "self", {"text": "x"})
        check("nor for a different action", diff.status, "pending")

        # -- decay: sixty days unused re-gates; use restarts the clock -----------
        clock.now = T0 + timedelta(days=50)
        used = await agency.propose("note", "keegan", {"text": "day 50"})
        check("still trusted on day 50", used.status, "executed")
        clock.now = T0 + timedelta(days=100)  # 50 days after the last use
        check(
            "use restarted the window: still trusted 50 days later",
            (await agency.propose("note", "keegan", {"text": "day 100"})).status, "executed",
        )
        clock.now = T0 + timedelta(days=161)  # 61 days after the last use
        lapsed = await agency.propose("note", "keegan", {"text": "day 161"})
        check("61 days unused and it asks again", lapsed.status, "pending")
        ledger = {(r["action"], r["target"]): r for r in await store.trust_ledger()}
        check("the lapse reset the streak", ledger[("note", "keegan")]["clean_streak"], 0)
        await agency.decide(str(lapsed.proposal["id"]), "deny")

        # -- an edit resets; approving the edited version is not clean -----------
        clock.now = T0
        await store._exec("delete from trust where not hard_line")
        for i in range(5):
            await approve_once(target="coach", text=f"c{i}")
        edit = await agency.propose("note", "coach", {"text": "draft"})
        editing = await agency.decide(str(edit.proposal["id"]), "edit")
        check("edit asks for the replacement", editing.status, "editing")
        ledger = {(r["action"], r["target"]): r for r in await store.trust_ledger()}
        check("an edit resets the streak", ledger[("note", "coach")]["clean_streak"], 0)
        revised = await agency.submit_edit("better draft")
        check("the replacement goes back up for approval", revised.status, "pending")
        check("with the new text", revised.proposal["payload"]["text"], "better draft")
        done = await agency.decide(str(revised.proposal["id"]), "approve")
        check("the edited version runs", done.status, "executed")
        check("with his text", ran[-1], ("coach", "better draft"))
        ledger = {(r["action"], r["target"]): r for r in await store.trust_ledger()}
        check("approving an edited one does not count as clean", ledger[("note", "coach")]["clean_streak"], 0)
        check("no edit is waiting any more", await agency.submit_edit("stray"), None)

        cancel_me = await agency.propose("note", "coach", {"text": "orig"})
        await agency.decide(str(cancel_me.proposal["id"]), "edit")
        kept = await agency.cancel_edit()
        check("/cancel restores the original for approval", (kept.status, kept.proposal["payload"]["text"]), ("pending", "orig"))

        # -- deny is a reversal: trusted straight back to gated -----------------
        await store._exec("delete from trust where not hard_line")
        for i in range(10):
            await approve_once(target="mom", text=f"m{i}")
        check("mom is trusted", (await agency.propose("note", "mom", {"text": "t"})).status, "executed")
        await agency.revoke("note", "mom")
        after = await agency.propose("note", "mom", {"text": "after revoke"})
        check("/revoke sends it straight back to asking", after.status, "pending")
        await agency.decide(str(after.proposal["id"]), "deny")
        ledger = {(r["action"], r["target"]): r for r in await store.trust_ledger()}
        check("and reversals are counted", ledger[("note", "mom")]["reversals"], 2)

        # -- hard lines: refused before anything, unlockable never --------------
        before = len(ran)
        refused = await agency.propose("submit", "schoolwork", {"text": "essay"})
        check("a hard line is refused", refused.status, "refused")
        check("and recorded as refused, never pending", refused.proposal["status"], "refused")
        del_any = await agency.propose("delete", "inbox", {})
        check("delete anything covers every delete target", del_any.status, "refused")
        for _ in range(100):
            await store.record_trust("submit", "schoolwork", "clean", now=T0,
                                     unlock_after=10, decay_days=60)
        check("a hundred approvals cannot unlock a hard line",
              await store.is_trusted("submit", "schoolwork", T0 + timedelta(days=1)), False)
        hard = {(r["action"], r["target"]): r for r in await store.trust_ledger()}
        check("the hard-line row was never touched", hard[("submit", "schoolwork")]["clean_streak"], 0)
        check("nothing ran", len(ran), before)

        # -- one decision, once ---------------------------------------------------
        once = await agency.propose("note", "race", {"text": "once"})
        pid = str(once.proposal["id"])
        results = await asyncio.gather(*(agency.decide(pid, "approve") for _ in range(5)))
        check("five racing approvals execute exactly once",
              sorted(r.status for r in results), ["executed", "stale", "stale", "stale", "stale"])
        check("the action ran once", sum(1 for r in ran if r == ("race", "once")), 1)
        check("a later deny of a decided proposal is stale", (await agency.decide(pid, "deny")).status, "stale")

        # -- failures are recorded, never raised --------------------------------
        async def boom(target, payload):
            raise RuntimeError("smtp exploded")

        agency.register(ActionType(name="flaky", preview=lambda t, p: "flaky",
                                   execute=boom, revise=lambda p, x: p))
        flaky = await agency.propose("flaky", "x", {})
        failed = await agency.decide(str(flaky.proposal["id"]), "approve")
        check("an executor raising is reported as failed", failed.status, "failed")
        row = await store.get_proposal(str(flaky.proposal["id"]))
        check("and recorded on the proposal", (row["status"], "smtp exploded" in row["result"]), ("failed", True))

        check("an unregistered action is refused",
              (await agency.propose("wire", "money_market", {})).status, "refused")
        check("reminders only go to him",
              (await agency.decide(str((await agency.propose("remind", "keegan", {"text": "x"})).proposal["id"]), "approve")).status,
              "failed")

    # -- callback data is input, and is validated like input ----------------------
    good = "0f8e3d0e-1c2b-4a5b-9c8d-7e6f5a4b3c2d"
    check("well-formed data parses", parse_callback(callback_data(good, "a")), (good, "approve"))
    check("and fits Telegram's 64 bytes", len(callback_data(good, "d").encode()) <= 64, True)
    for forged in ["", "p:x:a", f"p:{good}:z", f"q:{good}:a", f"p:{good}:a:extra",
                   f"p:{good.upper()}:a", "p:'; drop table proposals;--:a"]:
        check(f"forged {forged!r} decides nothing", parse_callback(forged), None)


async def bot_checks() -> None:
    """Only the owner's chat can press a button that does anything."""
    from sloane.telegram import Bot

    decided = []

    class FakeAgency:
        async def decide(self, pid, decision):
            decided.append((pid, decision))

    class FakeStore:
        async def log_message(self, **k):
            return True

    good = "0f8e3d0e-1c2b-4a5b-9c8d-7e6f5a4b3c2d"

    def press(chat, sender):
        return {"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": sender},
            "message": {"chat": {"id": chat}, "message_id": 5},
            "data": callback_data(good, "a"),
        }}

    for owner, chat, sender, label in [
        (0, 42, 42, "no owner configured"),
        (42, 99, 99, "a stranger's chat"),
        (42, 42, 99, "the owner's chat but someone else's tap"),
    ]:
        bot = Bot(FakeStore(), None, isolated(telegram_bot_token="x", telegram_chat_id=owner),
                  agency=FakeAgency())
        await bot._handle_callback(press(chat, sender))
        check(f"{label}: nothing is decided", decided, [])


asyncio.run(main())
asyncio.run(bot_checks())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("agency: ledger, decay, edits, hard lines, races and owner-only buttons all pass")
