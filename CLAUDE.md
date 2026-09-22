# Sloane — working notes for Claude

Sloane is Landen's always-on personal assistant: FastAPI + Telegram long polling, Postgres/pgvector memory,
read-only Canvas/calendar sync, scheduled briefs, voice, approval-gated actions, Gmail triage, reminders.
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
   is its own approval.

## Working

- The environment comes from `.claude/hooks/session-start.sh` on the web (`.venv`, plus Postgres at
  `/tmp:5433` from `scripts/dev_db.sh`). Locally, run those two yourself.
- Tests: `python tests/run.py` (unit suites always; integration ones when `DATABASE_URL` is set).
  Integration tests are **destructive**, so point them only at the local `sloane` db. Give a review agent
  `sloane_review` so it doesn't clobber yours.
- Lint: `python -m pyflakes sloane scripts tests`. The two known hits are intentional:
  `doctor.py` fastembed import and `test_router.py` `import sloane.main`.
- Real-model check: `python scripts/eval.py 'postgresql://postgres@/sloane_eval?host=/tmp&port=5433'`
  (about 7 `claude -p` calls; must stay 21/21).
- Every bug fix gets a regression test that fails on the old code. Confirm that by stashing the fix and rerunning.
- New migrations are `sql/00N_*.sql` and must be idempotent (CI applies them twice). If a migration adds a
  table, add it to `EXPECTED_TABLES` in `scripts/doctor.py`. If it adds a job, add the handler to
  `jobs/briefs.py` `HANDLERS` and the name to `tests/test_jobs.py`.
- Keep README, DEPLOY, `.env.example` and HANDOFF in step with the code in the same commit.
- Commit to `main` and push. CI (`test.yml`, `image.yml` for linux/arm64) must be green afterwards.
- This sandbox can't reach Telegram, Groq, Canvas, Google or Supabase. Test those against local stub servers
  (see `tests/test_school.py` and `tests/test_mail.py`). PyPI, GitHub and `claude -p` work.
- Credentials never go in chat, logs or replies. They live in `.env` on the box (chmod 600). The calendar
  ICS URL and the Canvas token are passwords.
