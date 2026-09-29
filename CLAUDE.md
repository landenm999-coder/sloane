# Sloane — working notes for Claude

Sloane is Landen's always-on personal assistant: FastAPI + Telegram long polling, Postgres/pgvector memory,
read-only Canvas/calendar sync, scheduled briefs, voice, approval-gated actions, Gmail triage, reminders.
She is meant to feel like a partner, not a help desk (he asked for "JARVIS"): the persona in `persona.py`, the
running CONVERSATION tier, a warm streamed `claude -p` (`providers/claude_code.py`), actions on request, web
lookups, and nightly learning (`memory/learn.py`). Keep her fast and in character when you change anything.
**Start with `HANDOFF.md`.** It has what's built, what's in flight, the backlog, and what only Landen can do.
Update its "In flight" and "Backlog" sections before you stop.

## Invariants (a violation is a bug even if tests pass)

1. Every model call goes through `sloane/router.py`. No vendor SDK outside `sloane/providers/`.
2. Every SQL statement lives in `sloane/memory/store.py`. `tests/test_invariants.py` enforces 1, 2 and 4.
3. FACTS (SQL) beats RECALL (similarity). Dates and deadlines come from SQL, never from recall.
4. Hard lines are code (`HARD_LINES` in `agent.py`), checked before any side effect, on an action's name
   *and* on what it really is (`ActionType.really`). No streak or config can unlock one.
5. Memory writes never block a reply: wrap them in `remember()`.
6. `Reply(speech, detail)` is built only in `agent.py`, `telegram.py` and `contract.py`. Jobs speak through the
   agent or `JobContext.say`.
7. Ingested text (email, calendar, Canvas) is data. Store it `trusted=false`, fence it as INGESTED, and run
   multi-line text through `ingest.unfence` (single-line text through `ingest.safe_field`).
8. School systems are read-only. She never submits work and never emails school staff (drafts only).
9. Anything Sloane *initiates* goes through `Agency.propose()`. A thing Landen explicitly asks for (e.g. `/remind`)
   is its own approval. That includes the commands her reply carries when he asks in words (`sloane/actions.py`):
   his own messages only, a rule per command, nothing destructive, `grounded()` (its words come from his message
   or the offer he said yes to), every result shown. A reply built from outside text is `tainted`: stored untrusted,
   and a placeholder in CONVERSATION.

## Skills: how a new feature plugs in

New capabilities are skills: one module `sloane/skills/<name>.py` exposing `build(ctx) -> Skill | None`
(None when unconfigured). They are discovered at startup, so adding one never edits the bot or the agent.
The contract (commands, plain-message rules, sessions, FACTS lines, TV panel, heartbeat nudges, and the
rules a skill keeps) is the docstring of `sloane/skills/__init__.py`. A skill's SQL goes in its own
`sql/0NN_<name>.sql` and its own section at the end of the `Store` class, headed `# -- <name> (sql/0NN) --`.
Its tests are `tests/test_<name>.py`, listed in `tests/run.py`. `SKILLS_DISABLED` switches one off.
A skill's tables never reference a core table (`test_invariants` checks), its user data goes in
`Store.BACKUP_TABLES` (parents before children) and doctor's `EXPECTED_TABLES`, and its FACTS lines render
after the school rows. Nineteen skills exist; read one (`lists.py` is the smallest) before writing the next.
Dates from his words go through `sloane/dates.py`, times through `sloane/reminders.py`. Never a model.

## Working

- The environment comes from `.claude/hooks/session-start.sh` on the web (`.venv`, plus Postgres at
  `/tmp:5433` from `scripts/dev_db.sh`). Locally, run those two yourself.
- On Landen's Windows PC, run everything in Docker instead:
  `powershell -File C:\Users\lande\sloane-dev\test.ps1 -Repo <repo or worktree> -Database <db>` applies every
  migration twice, then runs shellcheck, pyflakes and the suite on Python 3.12 against pgvector. `-Cmd "..."` runs
  one command instead. Give each worktree its own `-Database`.
- Tests: `python tests/run.py` (unit suites always; integration ones when `DATABASE_URL` is set).
  Integration tests are **destructive**, so point them only at the local `sloane` db. Give a review agent
  `sloane_review` so it doesn't clobber yours.
- Lint: `python -m pyflakes sloane scripts tests`. The two known hits are intentional:
  `doctor.py` fastembed import and `test_router.py` `import sloane.main`.
- Real-model check: `python scripts/eval.py 'postgresql://postgres@/sloane_eval?host=/tmp&port=5433'`
  (about 28 `claude -p` calls; must stay 68/68).
- She is one person on every transport: Telegram and the control room (`sloane/web.py`, `/app`) both answer
  through `Bot.respond` and log to the same `messages`. A new way to talk to her is an `Outlet`, never a
  copy of the reply pipeline. Every `/api` change route needs the session and the `X-Sloane` header
  (`tests/test_web.py` checks each one); `tests/test_capture.py` pins the list of POST routes.
- A new default for a setting that `.env.example` spells out doesn't reach an installed box (its `.env`
  pins the old one): add the old→new pair to `scripts/env_migrate.py`.
- Every bug fix gets a regression test that fails on the old code. Confirm that by stashing the fix and rerunning.
- New migrations are `sql/00N_*.sql` and must be idempotent (CI applies them twice). `sql/999_lock_public.sql`
  sorts last on purpose (row-level security on every table, Supabase's API roles revoked); leave it last. If a migration adds a
  table, add it to `EXPECTED_TABLES` in `scripts/doctor.py`. If it adds a job, add the handler to
  `jobs/briefs.py` `HANDLERS` and the name to `tests/test_jobs.py`.
- She edits herself through the workshop (`sloane/workshop.py`): a build in her own clone, a guard in code,
  CI, then Landen's Accept, then `scripts/upgrade.sh` on the box. What she may never change is
  `deploy/protected.txt` plus `LOCKED` and `KEPT_LINES` in `workshop.py`. A new safety file, safety test or
  hard-line definition goes on that list in the same commit. Her own builds read this file too, so keep it true.
- Keep README, DEPLOY, `.env.example` and HANDOFF in step with the code in the same commit.
- Commit to `main` and push. CI (`test.yml`, `image.yml` for linux/arm64) must be green afterwards.
- This sandbox can't reach Telegram, Groq, Canvas, Google or Supabase. Test those against local stub servers
  (see `tests/test_school.py` and `tests/test_mail.py`). PyPI, GitHub and `claude -p` work.
- Credentials never go in chat, logs or replies. They live in `.env` on the box (chmod 600). The calendar
  ICS URL and the Canvas token are passwords.
