"""The Sunday review and the nightly backup.

DESTRUCTIVE: truncates school_changes and commitments.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.contract import Reply
from sloane.jobs.briefs import JobContext, backup, weekly_review
from sloane.jobs.governor import Governor
from sloane.memory.store import Store

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
SUNDAY = datetime(2026, 9, 27, 19, 0, tzinfo=DEN)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Agent:
    def __init__(self):
        self.asked = []

    async def answer(self, question, *, channel="", today=None, ingested=""):
        self.asked.append((question, channel))
        return Reply(speech="Good week.", detail="details")


async def main() -> None:
    folder = tempfile.mkdtemp()
    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                      backup_dir=folder, backup_keep=2)
    async with Store(config) as store:
        await store._exec("truncate school_changes, commitments")
        await store.record_school_change(assignment_id=None, kind="graded", title="Stat quiz",
                                         course="Stat", detail="18/20")
        await store.record_school_change(assignment_id=None, kind="new", title="Essay", course=None, detail="")
        done = await store.add_commitment("send Keegan the outline")
        await store.close_commitment(str(done["id"]))

        agent, sent = Agent(), []

        async def send(reply):
            sent.append(reply)

        ctx = JobContext(store=store, agent=agent, governor=Governor(store, config), config=config, send=send)

        # -- weekly review -----------------------------------------------------------
        result = await weekly_review(ctx, SUNDAY)
        check("the review runs and is delivered", (result.ran, result.sent, len(sent)), (True, True, 1))
        question, channel = agent.asked[0]
        check("tagged as its own job", channel, "job:weekly_review")
        check("the week's grades are in the record", "- GRADED: Stat quiz [Stat] 18/20" in question, True)
        check("kept promises too", "- PROMISE KEPT: send Keegan the outline" in question, True)
        check("new assignments are not 'the week behind'", "Essay" in question, False)
        night = await weekly_review(ctx, SUNDAY.replace(hour=1))
        check("quiet hours hold it", night.ran, False)

        # -- backup ---------------------------------------------------------------------
        for day in range(3):
            res = await backup(ctx, SUNDAY + timedelta(days=day))
        files = sorted(p.name for p in Path(folder).glob("*.json"))
        check("keeps only the newest N", files, ["sloane-2026-09-28.json", "sloane-2026-09-29.json"])
        data = json.loads((Path(folder) / files[-1]).read_text())
        check("every hand-made table is in it", sorted(data["tables"]),
              sorted(Store.BACKUP_TABLES))
        check("including the promise", [c["what"] for c in data["tables"]["commitments"]],
              ["send Keegan the outline"])
        check("the hard lines are backed up with the trust ledger",
              sum(1 for t in data["tables"]["trust"] if t["hard_line"]) >= 6, True)
        check("no temp file left behind", list(Path(folder).glob("*.tmp")), [])
        check("reported", res.reason.endswith("sloane-2026-09-29.json"), True)

        # -- restore merges back what was lost, and changes nothing else ------------
        await store._exec("delete from commitments")
        kept_now = await store.add_commitment("a promise made after the backup")
        restored = await store.restore_rows("commitments", data["tables"]["commitments"])
        check("the lost promise comes back", restored, 1)
        check("and the newer one is untouched",
              sorted(c["what"] for c in await store._fetch("select what from commitments")),
              ["a promise made after the backup", "send Keegan the outline"])
        check("running it again adds nothing", await store.restore_rows("commitments", data["tables"]["commitments"]), 0)
        hostile = [{"what": "x", "id": str(kept_now["id"]), 'bad") values (1); drop table state; --': 1}]
        check("unknown (hostile) column names are ignored", await store.restore_rows("commitments", hostile), 0)
        try:
            await store.restore_rows("usage_log", [{"id": 1}])
            FAILURES.append("restore must refuse tables that are not backed up")
        except ValueError:
            pass

        nowhere = JobContext(store=store, agent=agent, governor=Governor(store, config),
                             config=isolated(database_url=os.environ["DATABASE_URL"]))
        check("no folder configured: says so", (await backup(nowhere, SUNDAY)).ran, False)


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("weekly: the Sunday review's record, and backups written atomically and rotated")
