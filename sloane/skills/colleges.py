"""Colleges: his applications -- the plan, the deadline and what's left.

    /college add CU Boulder EA nov 1                 a school, its plan and deadline, the usual checklist
    /college add Colorado State University (CSU) RD feb 1    a nickname in brackets
    /college add CU Boulder EA nov 1; Mines EA nov 1         several at once (or one per line)
    /colleges                                        every application, the next deadline first
    /college boulder                                 one school, its checklist numbered
    /college boulder done essays  ·  done 2          tick an item, by its words or its number
    /college boulder skip scores                     not needed (test-optional, say)
    /college boulder add portfolio by oct 20         another item, with its own date if it has one
    /college boulder ED nov 1  ·  deadline nov 15    change the plan or the deadline
    /college boulder submitted [yesterday]           it went in
    /college boulder admitted | deferred | waitlisted | denied | committed
    /college boulder reopen  ·  /college boulder drop
    "what's left for Boulder?"  ·  "what's left on my college apps?"

A school is named by its number in /colleges, by any run of words in its name
("boulder" for CU Boulder), by its nickname, or by its initials (CSU).

Every application is a FACTS line with its deadline and what's left, so "what's
left for Boulder?" is answered from rows, never from memory. He hears about a
deadline two weeks, a week, three days and a day out (in the evening, after
work) and on the morning of the day; and once, the morning after, if a deadline
passes without the application being marked submitted.

This tracks. It never submits an application, pays a fee or writes to a school.
"""

from __future__ import annotations

import re
from datetime import date

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, cap, Nudge, Skill, SkillContext

MAX_NAME = 80
MAX_NICKNAME = 20
MAX_TASK = 120
FACTS_COLLEGES = 8
MAX_AT_ONCE = 15

# What nearly every application needs. He skips what a school doesn't want.
DEFAULT_TASKS = ("Application form", "Essays", "Recommendations", "Transcript", "Test scores", "Fee or waiver")
_APPLICATION_TASK = DEFAULT_TASKS[0]
# The words he'd use for each default item.
TASK_WORDS: dict[str, frozenset[str]] = {
    "application form": frozenset({"app", "application", "form", "common", "commonapp", "coalition"}),
    "essays": frozenset({"essay", "essays", "supplement", "supplements", "supp", "supps", "supplemental",
                         "writing", "prompt", "prompts", "statement", "psych"}),
    "recommendations": frozenset({"rec", "recs", "recommendation", "recommendations", "letter", "letters",
                                  "lor", "lors", "reference", "references", "counselor"}),
    "transcript": frozenset({"transcript", "transcripts", "srar", "ssar"}),
    "test scores": frozenset({"score", "scores", "test", "tests", "sat", "act"}),
    "fee or waiver": frozenset({"fee", "fees", "payment", "pay", "paid", "waiver"}),
}

PLAN_NAMES = {"ED": "Early Decision", "ED2": "Early Decision II", "EA": "Early Action",
              "REA": "Restrictive Early Action", "RD": "Regular Decision", "rolling": "Rolling",
              "priority": "Priority"}
PLAN_SHORT = {"ED": "ED", "ED2": "ED II", "EA": "EA", "REA": "REA", "RD": "RD", "rolling": "rolling",
              "priority": "priority"}
# Longest first: "early decision 2" before "early decision", "rea" before "ea".
_PLANS = (
    (re.compile(r"\b(?:restrictive\s+early\s+action|single[\s-]choice\s+early\s+action|rea|scea)\b", re.I), "REA"),
    (re.compile(r"\b(?:early\s+decision\s*(?:ii|2|two)|ed\s?(?:ii|2))\b(?![/.-]\d)", re.I), "ED2"),
    (re.compile(r"\b(?:early\s+decision(?:\s*(?:i|1|one)\b)?|ed(?:\s?1)?)\b", re.I), "ED"),
    (re.compile(r"\b(?:early\s+action|ea)\b", re.I), "EA"),
    (re.compile(r"\b(?:regular\s+decision|regular|rd)\b", re.I), "RD"),
    (re.compile(r"\brolling(?:\s+admissions?)?\b", re.I), "rolling"),
    (re.compile(r"\bpriority(?:\s+deadline)?\b", re.I), "priority"),
)

# Where an application stands once it is in.
STATUS_WORDS = {
    "submitted": "submitted", "applied": "submitted", "sent": "submitted",
    "admitted": "admitted", "accepted": "admitted", "admit": "admitted",
    "deferred": "deferred", "waitlisted": "waitlisted", "waitlist": "waitlisted",
    "denied": "denied", "rejected": "denied",
    "committed": "committed", "commit": "committed", "enrolled": "committed", "deposit": "committed",
}
DECIDED = ("admitted", "deferred", "waitlisted", "denied", "committed")
DONE_WORDS = frozenset({"done", "did", "finished", "finish", "check", "tick", "complete", "completed"})
SKIP_WORDS = frozenset({"skip", "skipped", "optional"})
OPEN_WORDS = frozenset({"undo", "untick", "unskip", "open"})
DROP_WORDS = frozenset({"drop", "archive", "remove"})

_STOP = {"the", "my", "a", "an", "for", "of", "at", "and", "&", "to"}
_NICKNAME = re.compile(r"\(([^()]{1,40})\)")
_WHATS_LEFT = re.compile(
    r"^\s*(?:what(?:'s|\s+is|\s+do\s+i\s+have|\s+have\s+i\s+got)|anything)\s+(?:still\s+)?left\s+"
    r"(?:to\s+do\s+)?(?:for|on|with)\s+(?:my\s+|the\s+)?(?P<name>.+?)\s*[?.!]*\s*$",
    re.I,
)
# He did something the tracker records. Each needs one of his schools named.
_FINISHED = re.compile(
    r"^\s*(?:i\s+)?(?:just\s+|finally\s+)?(?P<verb>finished|completed|wrapped\s+up|done\s+with|submitted|"
    r"sent(?:\s+(?:in|off))?|turned\s+in|hit\s+submit\s+on)\s+(?P<rest>.+?)\s*[.!]*\s*$",
    re.I,
)
_GOT_IN = re.compile(
    r"^\s*(?:i\s+)?(?:just\s+)?(?:got\s+(?:into|in\s+to|accepted\s+(?:to|at|by)|admitted\s+to)|"
    r"was\s+(?:accepted|admitted)\s+(?:to|at|by))\s+(?P<rest>.+?)\s*[.!]*\s*$",
    re.I,
)
_DECIDED_ME = re.compile(
    r"^\s*(?P<rest>.+?)\s+(?:just\s+)?(?P<verb>accepted|admitted|deferred|waitlisted|rejected|denied)\s+me\s*[.!]*\s*$",
    re.I,
)
_GOT_DECISION = re.compile(
    r"^\s*(?:i\s+)?(?:just\s+)?(?:got|was|been)\s+(?P<verb>deferred|waitlisted|rejected|denied)\s+"
    r"(?:from|by|at)\s+(?P<rest>.+?)\s*[.!]*\s*$",
    re.I,
)
_APP_WORDS = frozenset({"app", "apps", "application", "applications", "whole", "entire"})
_ALL_APPS = re.compile(r"^(?:college|colleges|college\s+apps?|college\s+applications?|apps|applications)$", re.I)
_APP_SUFFIX = re.compile(r"\s+(?:app|apps|application|applications)$", re.I)


def _words(text: str) -> list[str]:
    """Lowercase words with possessives folded: "St. Mary's" -> st, mary."""
    return re.findall(r"[a-z0-9]+", re.sub(r"['’]s\b|['’]", "", (text or "").lower()))


def _initials(name: str) -> str:
    return "".join(w[0] for w in _words(name) if w not in _STOP)


def _runs(name: str, wanted: list[str], *, partial: bool) -> bool:
    """`wanted` appears as a run of the name's words; with `partial` the last
    may be cut short, if he typed at least three letters of it."""
    words = _words(name)
    n = len(wanted)
    for i in range(len(words) - n + 1):
        if words[i:i + n - 1] != wanted[:-1]:
            continue
        last, typed = words[i + n - 1], wanted[-1]
        if last == typed or (partial and len(typed) >= 3 and last.startswith(typed)):
            return True
    return False


def find_plan(text: str) -> tuple[str, tuple[int, int]] | None:
    """The plan named in `text` ("EA", "early decision 2", "rolling"), and where."""
    best = None
    for pattern, code in _PLANS:
        hit = pattern.search(text)
        if hit and (best is None or hit.start() < best[1][0]):
            best = (code, hit.span())
    return best


def _cut(text: str, span: tuple[int, int]) -> str:
    return re.sub(r"\s+", " ", text[: span[0]] + " " + text[span[1]:]).strip(" ,:;-")


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def _and(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _names(word: str, task: dict) -> bool:
    """Is `word` one he'd use for this checklist item?"""
    own = set(_words(task["task"])) | TASK_WORDS.get(task["task"].lower(), frozenset())
    return word in own or (len(word) >= 4 and any(o.startswith(word) or word.startswith(o)
                                                  for o in own if len(o) >= 4))


class Colleges(Skill):
    name = "colleges"
    help = (
        "`/college add <school> <EA|ED|RD> <deadline>` — track an application; `/colleges` all of them",
        "`/college <school>` its checklist · `done <item>` · `skip <item>` · `add <item>` · `submitted` · `admitted`",
    )
    commands = frozenset({"college", "colleges"})

    # -- reading --------------------------------------------------------------------

    async def _all(self) -> tuple[list[dict], dict[str, list[dict]]]:
        """(applications in /colleges order, each one's checklist by id)."""
        rows = await self.ctx.store.active_colleges()
        tasks: dict[str, list[dict]] = {}
        for t in await self.ctx.store.active_college_tasks():
            tasks.setdefault(str(t["college_id"]), []).append(t)
        return rows, tasks

    async def _resolve(self, text: str, *, partial: bool = True) -> tuple[dict | None, str, bool]:
        """(school, the rest of the words, ambiguous). By number, words, nickname or initials.

        The longest opening run of his words that names exactly one school wins,
        so "/college boulder done essays" is CU Boulder, then "done essays".
        """
        rows, _ = await self._all()
        text = text.strip()
        first, _, rest = text.partition(" ")
        if first.isdigit():
            n = int(first)
            return (rows[n - 1] if 1 <= n <= len(rows) else None), rest.strip(), False
        typed = text.split()
        for k in range(len(typed), 0, -1):
            wanted = _words(" ".join(typed[:k]))
            if not wanted:
                continue
            joined = "".join(wanted)
            hits = [r for r in rows
                    if _runs(r["name"], wanted, partial=partial)
                    or (r.get("nickname") and _words(r["nickname"]) == wanted)
                    or (k == 1 and len(joined) >= 2 and _initials(r["name"]) == joined)]
            if len(hits) == 1:
                return hits[0], " ".join(typed[k:]), False
            if len(hits) > 1:
                exact = [r for r in hits if _words(r["name"]) == wanted
                         or (r.get("nickname") and _words(r["nickname"]) == wanted)]
                if len(exact) == 1:
                    return exact[0], " ".join(typed[k:]), False
                return None, text, True
        return None, text, False

    @staticmethod
    def _task(tasks: list[dict], words: str) -> tuple[dict | None, bool]:
        """(the checklist item he means, ambiguous). By number or by its words."""
        words = words.strip(" .,:;")
        if words.isdigit():
            n = int(words)
            return (tasks[n - 1] if 1 <= n <= len(tasks) else None), False
        wanted = [w for w in _words(words) if w not in _STOP]
        if not wanted:
            return None, False
        scored = [(sum(_names(w, t) for w in wanted), t) for t in tasks]
        best = max((s for s, _ in scored), default=0)
        if best == 0:
            return None, False
        top = [t for s, t in scored if s == best]
        return (top[0], False) if len(top) == 1 else (None, True)

    # -- rendering ------------------------------------------------------------------

    @staticmethod
    def _handle(row: dict) -> str:
        """How to name this school in a command: its nickname, else its whole
        name. Its last word alone ("college") can name several."""
        return (row.get("nickname") or row["name"]).lower()

    @staticmethod
    def _label(row: dict) -> str:
        nick = row.get("nickname")
        return f"{row['name']} ({nick})" if nick else row["name"]

    @staticmethod
    def _left(tasks: list[dict]) -> list[dict]:
        return [t for t in tasks if t["state"] == "open"]

    @staticmethod
    def _progress(tasks: list[dict]) -> str:
        needed = [t for t in tasks if t["state"] != "skipped"]
        if not needed:
            return ""
        return f"{sum(t['state'] == 'done' for t in needed)} of {len(needed)} done"

    @staticmethod
    def _day(day: date, today: date, *, exact: bool) -> str:
        """'Sat Nov 1, 2026' for FACTS, where a date is evidence; 'Saturday' when spoken."""
        return f"{day:%a %b} {day.day}, {day.year}" if exact else dates.spoken(day, today)

    @staticmethod
    def _calendar(day: date, today: date, *, exact: bool) -> str:
        """Always the date itself ('Sun Nov 1'), for lines that add '(tomorrow)' after it."""
        year = f", {day.year}" if exact or day.year != today.year else ""
        return f"{day:%a %b} {day.day}{year}"

    def _item(self, t: dict, today: date, *, exact: bool = False) -> str:
        if t.get("due_on") and t["state"] == "open":
            return f"{t['task']} (due {self._day(t['due_on'], today, exact=exact)})"
        return t["task"]

    def _where(self, row: dict, today: date, *, exact: bool = False) -> str:
        """Where it stands: 'due Sat Nov 1 (in 38 days)', 'submitted Oct 3', 'admitted'."""
        status = row["status"]
        if status == "applying":
            if row.get("deadline") is None:
                return "no deadline set"
            when = self._calendar(row["deadline"], today, exact=exact)
            ago = dates.until(row["deadline"], today)
            if row["deadline"] < today:
                return f"deadline was {when} ({ago}), not marked submitted"
            return f"due {when} ({ago})"
        sent = row.get("submitted_on")
        if status == "submitted":
            return f"submitted {self._day(sent, today, exact=exact)}" if sent else "submitted"
        return status

    def _when(self, row: dict, today: date) -> str:
        """The plan and where it stands: 'EA, due Sat Nov 1 (in 38 days)'."""
        plan = PLAN_SHORT.get(row.get("plan") or "", "") if row["status"] == "applying" else ""
        return ", ".join(p for p in (plan, self._where(row, today)) if p)

    def _line(self, row: dict, tasks: list[dict], today: date) -> str:
        bits = [self._when(row, today)]
        if row["status"] == "applying":
            progress = self._progress(tasks)
            if progress:
                bits.append(progress)
        elif row["status"] == "submitted":
            left = self._left(tasks)
            if left:
                bits.append("still open: " + ", ".join(t["task"] for t in left))
        return " · ".join(bits)

    # -- commands ------------------------------------------------------------------------

    async def add_many(self, rest: str) -> Answer:
        """Several schools at once, one per line or split by semicolons."""
        parts = [p.strip() for p in re.split(r"[;\n]+", rest) if p.strip()]
        if len(parts) <= 1:
            return await self.add(rest)
        answers = [await self.add(part) for part in parts[:MAX_AT_ONCE]]
        added = sum(a.speech.startswith("Added ") for a in answers)
        if added == len(answers):
            speech = f"Added all {added}."
        elif added == 0:
            speech = "None of those went on; the reasons are below."
        else:
            speech = f"Added {added} of {len(answers)}; the others need a look."
        if len(parts) > MAX_AT_ONCE:
            speech += f" I took the first {MAX_AT_ONCE}."
        lines = [f"• {a.speech}" for a in answers]
        return Answer(speech, "\n".join(lines + ["", "`/colleges` shows them all."]))

    async def add(self, rest: str) -> Answer:
        today = self.ctx.today()
        text = safe_field(rest, limit=300)
        nickname = None
        nick = _NICKNAME.search(text)
        if nick:
            nickname = safe_field(nick.group(1), limit=MAX_NICKNAME) or None
            text = _cut(text, nick.span())
        plan = find_plan(text)
        if plan:
            text = _cut(text, plan[1])
        deadline = None
        found = dates.find(text, today)
        if found:
            if found.day < today:
                return Answer(f"{dates.spoken(found.day, today)} has already passed.",
                              "Give the deadline that's still ahead, e.g. `/college add CU Boulder EA nov 1`.")
            deadline = found.day
            text = dates.remove(text, found)
        text = re.sub(r"\b(?:deadline|due)\b", " ", text, flags=re.I)
        name = safe_field(text.strip(" ,:;-"), limit=MAX_NAME)
        if not _words(name):
            return Answer("Which school? Try /college add CU Boulder EA nov 1.")
        row = await self.ctx.store.add_college(name, nickname=nickname, plan=plan[0] if plan else None,
                                               deadline=deadline, tasks=DEFAULT_TASKS)
        if row is None:
            return Answer(f"{name} is already on your list.")
        what = PLAN_NAMES.get(row.get("plan") or "")
        speech = f"Added {name}"
        if what:
            speech += f", {what}"
        if deadline:
            speech += f", due {dates.spoken(deadline, today)}, {dates.until(deadline, today)}"
        speech += "." if deadline else ". When's the deadline?"
        detail = (f"**{self._label(row)}** — {self._when(row, today)}\n"
                  f"Checklist: {', '.join(DEFAULT_TASKS)}.\n"
                  "`/college <school> skip <item>` for anything this school doesn't want.")
        return Answer(speech, detail)

    async def listing(self) -> Answer:
        rows, tasks = await self._all()
        today = self.ctx.today()
        if not rows:
            return Answer("No college applications yet.", "Add one with `/college add CU Boulder EA nov 1`.")
        lines = [f"{i}. {self._label(r)} — {self._line(r, tasks.get(str(r['id']), []), today)}"
                 for i, r in enumerate(rows, 1)]
        ahead = [r for r in rows if r["status"] == "applying" and r.get("deadline") and r["deadline"] >= today]
        late = [r for r in rows if r["status"] == "applying" and r.get("deadline") and r["deadline"] < today]
        speech = f"{len(rows)} application{'s' if len(rows) != 1 else ''}"
        if ahead:
            nxt = ahead[0]
            plan = PLAN_SHORT.get(nxt.get("plan") or "")
            speech += (f"; next up is {nxt['name']}" + (f" {plan}" if plan else "")
                       + f", due {dates.spoken(nxt['deadline'], today)}")
        if late:
            speech += f"; {len(late)} past deadline and not marked submitted"
        decided = [r for r in rows if r["status"] in ("admitted", "committed")]
        if not ahead and not late and decided:
            speech += f"; {len(decided)} admitted so far"
        return Answer(speech + ".", "\n".join(lines))

    async def show(self, row: dict) -> Answer:
        today = self.ctx.today()
        _, all_tasks = await self._all()
        tasks = all_tasks.get(str(row["id"]), [])
        mark = {"done": "✅", "skipped": "➖", "open": "⬜"}
        plan = PLAN_NAMES.get(row.get("plan") or "") if row["status"] == "applying" else None
        lines = [f"**{self._label(row)}** — " + ", ".join(p for p in (plan, self._where(row, today)) if p)]
        for i, t in enumerate(tasks, 1):
            note = " (not needed)" if t["state"] == "skipped" else ""
            lines.append(f"{i}. {mark[t['state']]} {self._item(t, today)}{note}")
        left = self._left(tasks)
        if row["status"] == "applying":
            if row.get("deadline"):
                speech = f"{row['name']} is due {dates.spoken(row['deadline'], today)}"
                if row["deadline"] < today:
                    speech = f"{row['name']}'s deadline was {dates.spoken(row['deadline'], today)}"
            else:
                speech = f"{row['name']} has no deadline set"
            speech += (f"; left: {_and([t['task'].lower() for t in left])}." if left
                       else "; everything on the checklist is done.")
        else:
            speech = f"{row['name']}: {self._when(row, today)}"
            speech += f"; still open: {_and([t['task'].lower() for t in left])}." if left else "."
        return Answer(cap(speech), "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if name == "colleges" or not rest:
            return await self.listing()
        verb, tail = (re.split(r"\s+", rest, maxsplit=1) + [""])[:2]
        if verb.lower() in {"add", "new"}:
            return await self.add_many(tail)
        row, action, ambiguous = await self._resolve(rest)
        if row is None:
            if ambiguous:
                return Answer("Which one? More than one school matches.", "Use the number from /colleges.")
            return Answer("Which school? Use the number from /colleges or part of its name.")
        return await self.act(row, action)

    async def act(self, row: dict, action: str) -> Answer:
        today = self.ctx.today()
        cid = str(row["id"])
        action = action.strip()
        if not action:
            return await self.show(row)
        raw = action.split()
        words = [w.lower().strip(":,.!?") for w in raw]
        head, tail, last = words[0], " ".join(raw[1:]), words[-1]

        if head in DROP_WORDS and len(words) == 1:
            await self.ctx.store.archive_college(cid)
            return Answer(f"Took {row['name']} off your list.")
        if head == "reopen" and len(words) == 1:
            done = await self.ctx.store.update_college(cid, status="applying", submitted_on=None)
            return Answer(f"{done['name']} is back to in progress.")
        if head == "add" and tail:
            return await self._add_task(row, tail, today)
        if head in {"deadline", "due"} and tail:
            return await self._deadline(row, tail, today, plan=None)
        plan = find_plan(action)
        if plan:  # "ED nov 1", "early action": only a plan and maybe a date
            rest = _cut(action, plan[1])
            when = dates.find(rest, today)
            leftover = dates.remove(rest, when) if when else rest
            if not re.sub(r"\b(?:deadline|due|by|on|is)\b", " ", leftover, flags=re.I).strip(" ,:;-"):
                return await self._deadline(row, rest, today, plan=plan[0])
        if head == "got" and len(words) > 1:  # "got in", "got deferred"
            status = "admitted" if words[1] in {"in", "into"} else STATUS_WORDS.get(words[1])
            if status and status != "submitted":
                return await self._status(row, status, "", today)

        # A checklist item: "done essays", "sent recs", "essays done", "skip scores", "undo 2".
        _, all_tasks = await self._all()
        tasks = all_tasks.get(cid, [])
        sending = {"sent", "submitted"}
        for verbs, state in ((DONE_WORDS | sending, "done"), (SKIP_WORDS, "skipped"), (OPEN_WORDS, "open")):
            if head in verbs and tail:
                words_of_item, verb = tail, head
            elif last in verbs and len(words) > 1:
                words_of_item, verb = " ".join(raw[:-1]), last
            else:
                continue
            task, ambiguous = self._task(tasks, words_of_item)
            if verb in sending and (task is None and not ambiguous or task and task["task"] == _APPLICATION_TASK):
                # "submitted yesterday", "application sent": the application itself went in.
                return await self._status(row, "submitted", words_of_item, today)
            return await self._tick(row, tasks, task, ambiguous, state)
        if head in DONE_WORDS:
            return Answer(f"Done with what? Try /college {self._handle(row)} done essays, "
                          "or submitted if it went in.")
        status = STATUS_WORDS.get(head)
        if status is not None:
            return await self._status(row, status, tail, today)
        return Answer("I didn't follow that.",
                      "Try `done <item>`, `skip <item>`, `add <item>`, `EA nov 1`, `deadline <date>`, "
                      "`submitted`, `admitted`, `deferred`, `waitlisted`, `denied`, `committed` or `drop`.")

    async def _add_task(self, row: dict, text: str, today: date) -> Answer:
        found = dates.find(text, today)
        due = found.day if found and found.day >= today else None
        if found:
            text = dates.remove(text, found)
        task = safe_field(text.strip(" ,:;-"), limit=MAX_TASK)
        if not _words(task):
            return Answer("What's the item?")
        task = cap(task)
        added = await self.ctx.store.add_college_task(str(row["id"]), task, due)
        if added is None:
            return Answer(f"{row['name']} isn't on your list anymore.")
        when = f", due {dates.spoken(due, today)}" if due else ""
        return Answer(f"Added {task.lower()} to {row['name']}{when}.")

    async def _deadline(self, row: dict, text: str, today: date, *, plan: str | None) -> Answer:
        found = dates.find(text, today)
        fields: dict = {}
        if plan:
            fields["plan"] = plan
        if found:
            if found.day < today:
                return Answer(f"{dates.spoken(found.day, today)} has already passed.")
            fields["deadline"] = found.day
        if not fields:
            return Answer("When's the deadline? Try /college boulder deadline nov 15.")
        done = await self.ctx.store.update_college(str(row["id"]), **fields)
        if done is None:
            return Answer(f"{row['name']} isn't on your list anymore.")
        bits = []
        if plan:
            bits.append(PLAN_NAMES[plan])
        if done.get("deadline"):
            bits.append(f"due {dates.spoken(done['deadline'], today)}, {dates.until(done['deadline'], today)}")
        return Answer(f"{done['name']}: {', '.join(bits)}.")

    async def _tick(self, row: dict, tasks: list[dict], task: dict | None, ambiguous: bool, state: str) -> Answer:
        if task is None:
            names = ", ".join(f"{i}. {t['task']}" for i, t in enumerate(tasks, 1))
            ask = "Which item? More than one matches." if ambiguous else "Which item?"
            return Answer(ask, f"{row['name']}: {names}" if names else f"{row['name']} has no checklist items.")
        await self.ctx.store.set_college_task(task["id"], state)
        item = task["task"].lower()
        if state == "open":
            return Answer(f"{cap(item)} is open again for {row['name']}.")
        if state == "skipped":
            return Answer(f"{row['name']} doesn't need {item}; off the checklist.")
        left = [t for t in tasks if t["state"] == "open" and t["id"] != task["id"]]
        if row["status"] != "applying":
            return Answer(f"{cap(item)} done for {row['name']}.")
        if not left:
            return Answer(f"{cap(item)} done; that's everything for {row['name']}. "
                          "Tell me when it's submitted.")
        return Answer(f"{cap(item)} done for {row['name']}; left: {_and([t['task'].lower() for t in left])}.")

    async def _status(self, row: dict, status: str, rest: str, today: date) -> Answer:
        cid = str(row["id"])
        fields: dict = {"status": status}
        if status == "submitted":
            found = dates.find(rest, today, future=False)
            sent = found.day if found and found.day <= today else today
            fields["submitted_on"] = sent
        done = await self.ctx.store.update_college(cid, **fields)
        if done is None:
            return Answer(f"{row['name']} isn't on your list anymore.")
        name = done["name"]
        if status == "submitted":
            _, all_tasks = await self._all()
            tasks = all_tasks.get(cid, [])
            for t in tasks:
                if t["task"] == _APPLICATION_TASK and t["state"] == "open":
                    await self.ctx.store.set_college_task(t["id"], "done")
                    t["state"] = "done"
            left = self._left(tasks)
            when = dates.spoken(fields["submitted_on"], today)
            speech = f"{name} is in, submitted {when}."
            if left:
                speech += f" Still open: {_and([t['task'].lower() for t in left])}."
            return Answer(speech)
        return Answer({
            "admitted": f"Admitted to {name}. Congratulations.",
            "deferred": f"{name} deferred you. That isn't a no; it goes to the next round.",
            "waitlisted": f"Waitlisted at {name}. Still in play.",
            "denied": f"{name} said no. I'm sorry.",
            "committed": f"Committed to {name}. That's the one.",
        }[status])

    # -- plain messages ------------------------------------------------------------------

    async def _find(self, text: str) -> tuple[dict | None, list[str]]:
        """The one school named anywhere in `text` by whole words, and the other
        words. A run that starts or ends on a small word ("of", "the") never
        counts, or "the rest of my essays" would be University of Denver."""
        rows, _ = await self._all()
        words = text.split()
        for n in range(len(words), 0, -1):
            for i in range(len(words) - n + 1):
                wanted = _words(" ".join(words[i:i + n]))
                if not wanted or wanted[0] in _STOP or wanted[-1] in _STOP:
                    continue
                hits = [r for r in rows
                        if _runs(r["name"], wanted, partial=False)
                        or (r.get("nickname") and _words(r["nickname"]) == wanted)
                        or (len(wanted) == 1 and len(wanted[0]) >= 2 and _initials(r["name"]) == wanted[0])]
                if len(hits) == 1:
                    return hits[0], words[:i] + words[i + n:]
                if len(hits) > 1:
                    return None, words
        return None, words

    async def _did(self, text: str) -> Answer | None:
        """He says he did something the tracker records: rules, not a model, so
        it is recorded every time. Only when a school of his is named."""
        found = _FINISHED.match(text)
        if found:
            row, rest = await self._find(found["rest"])
            if row is None:
                return None  # "finished my essay" for English class: the agent's
            words = [w for w in rest if _words(w) and _words(w)[0] not in _STOP | _APP_WORDS]
            app = any(_words(w) and _words(w)[0] in _APP_WORDS for w in rest)
            sending = found["verb"].lower().split()[0] in {"submitted", "sent", "turned", "hit"}
            if not words:
                if sending:
                    return await self.act(row, "submitted")
                return await self.act(row, "done application") if app else None
            _, all_tasks = await self._all()
            task, _ = self._task(all_tasks.get(str(row["id"]), []), " ".join(words))
            if task is None or not all(_names(w, task) for w in _words(" ".join(words)) if w not in _STOP):
                return None  # words the item doesn't explain ("the DECA form to Boulder High"): the agent's
            if sending and task["task"] == _APPLICATION_TASK:
                return await self.act(row, "submitted")
            return await self.act(row, f"done {' '.join(words)}")
        decided = _GOT_IN.match(text) or _DECIDED_ME.match(text) or _GOT_DECISION.match(text)
        if decided:
            row, rest = await self._find(decided["rest"])
            if row is None or [w for w in rest if _words(w) and _words(w)[0] not in _STOP | _APP_WORDS]:
                return None
            verb = (decided.groupdict().get("verb") or "admitted").lower()
            return await self.act(row, STATUS_WORDS[verb])
        return None

    async def match(self, text: str) -> Answer | None:
        text = text.replace("’", "'")
        did = await self._did(text)
        if did is not None:
            return did
        found = _WHATS_LEFT.match(text)
        if found is None:
            return None
        name = _APP_SUFFIX.sub("", found["name"].strip())
        if _ALL_APPS.match(name) or _ALL_APPS.match(found["name"].strip()):
            rows, _ = await self._all()
            return await self.listing() if rows else None
        row, leftover, _ = await self._resolve(name, partial=False)
        if row is None or leftover.strip():
            return None  # not one of his schools: a homework question, the agent's
        return await self.show(row)

    # -- context, dashboard, heartbeat -------------------------------------------------------

    async def facts(self) -> list[str]:
        rows, tasks = await self._all()
        if not rows:
            return []
        today = self.ctx.today()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary = ", ".join(f"{n} {'in progress' if s == 'applying' else s}" for s, n in counts.items())
        lines = [f"- COLLEGES {len(rows)} application{'s' if len(rows) != 1 else ''}: {summary}"]
        for r in rows[:FACTS_COLLEGES]:
            own = tasks.get(str(r["id"]), [])
            plan = PLAN_NAMES.get(r.get("plan") or "")
            head = safe_field(self._label(r), limit=MAX_NAME + MAX_NICKNAME + 3)
            if plan:
                head += f" ({plan})"
            left = self._left(own)
            bits = [self._where(r, today, exact=True)]
            if left:
                bits.append("left: " + ", ".join(safe_field(self._item(t, today, exact=True), limit=MAX_TASK + 30)
                                                 for t in left))
            elif own and r["status"] == "applying":
                bits.append("checklist all done")
            lines.append(f"- COLLEGE {head}: " + "; ".join(bits))
        if len(rows) > FACTS_COLLEGES:
            lines.append(f"- COLLEGE {len(rows) - FACTS_COLLEGES} more, not listed here (/colleges has all)")
        return lines

    async def panel(self) -> dict | None:
        rows, tasks = await self._all()
        if not rows:
            return None
        today = self.ctx.today()
        lines = []
        for r in rows[:8]:
            own = tasks.get(str(r["id"]), [])
            if r["status"] == "applying" and r.get("deadline"):
                plan = PLAN_SHORT.get(r.get("plan") or "")
                left = (r["deadline"] - today).days
                when = f"{r['deadline']:%b} {r['deadline'].day}"
                gap = "late" if left < 0 else ("today" if left == 0 else _days(left))
                progress = self._progress(own)
                lines.append(" · ".join(p for p in (r.get("nickname") or r["name"], plan, f"{when} ({gap})",
                                                    progress) if p))
            else:
                lines.append(f"{r.get('nickname') or r['name']} · {self._when(r, today)}")
        return {"title": "Applications", "lines": lines}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        today = now.date()
        evening = now.hour >= 19
        rows, tasks = await self._all()
        out: list[Nudge] = []
        for r in rows:
            own = tasks.get(str(r["id"]), [])
            left_items = self._left(own)
            what = _and([t["task"].lower() for t in left_items])
            if r["status"] == "applying" and r.get("deadline"):
                days = (r["deadline"] - today).days
                key = f"college:{r['id']}:{r['deadline'].isoformat()}"
                plan = PLAN_NAMES.get(r.get("plan") or "")
                app = f"{r['name']}" + (f" ({plan})" if plan else "")
                still = f" Left: {what}." if what else " The checklist is done; it just needs submitting."
                if days in (14, 7, 3) and evening:
                    out.append(Nudge(f"{key}:{days}",
                                     f"🎓 {app} is due in {_days(days)}, {dates.spoken(r['deadline'], today)}.{still}"))
                elif days == 1 and evening:
                    out.append(Nudge(f"{key}:1", f"🎓 {app} is due tomorrow.{still}"))
                elif days == 0:
                    out.append(Nudge(f"{key}:0", f"🎓 {app} is due today.{still}"))
                elif days == -1:
                    out.append(Nudge(f"{key}:late",
                                     f"🎓 {r['name']}'s deadline was yesterday and it isn't marked submitted. "
                                     f"If it went in: /college {self._handle(r)} submitted"))
            if r["status"] not in ("applying", "submitted"):
                continue
            for t in left_items:
                if not t.get("due_on"):
                    continue
                days = (t["due_on"] - today).days
                key = f"college-task:{t['id']}:{t['due_on'].isoformat()}"
                item = f"{t['task']} for {r['name']}"
                if days in (3, 1) and evening:
                    out.append(Nudge(f"{key}:{days}", f"🎓 {item} is due {dates.spoken(t['due_on'], today)}."))
                elif days == 0:
                    out.append(Nudge(f"{key}:0", f"🎓 {item} is due today."))
        return out


def build(ctx: SkillContext) -> Skill:
    return Colleges(ctx)
