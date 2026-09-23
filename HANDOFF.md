# Sloane — handoff

**Read this first if you are a new Claude Code session picking up Sloane.**
This file records what exists, what's verified, what's in flight and what's left.
It's kept up to date at the end of every work session. Last updated: 2026-09-22 (evening).

- Repo: `github.com/landenm999-coder/sloane`, branch `main` (commit straight to main; CI runs on push).
- Separate from **Capture** (`landenm999-coder/capture`), a voice-capture PWA that will later feed Sloane
  through an API. Keep them in separate repos. The old `claude/sloane-personal-assistant-nodd15` branch on
  capture is stale and can be deleted by Landen.
- Owner: Landen. High-school senior in Parker, CO (America/Denver). Works 3–7 PM Mon–Fri, runs a small AI-website
  business, does DECA. Wants everything automated; his only steps are the ones in "Only Landen can do".

---

## Status in one paragraph

v1 (build plan phases P0–P4) is **done, reviewed and green in CI**. Sloane is a Python service (FastAPI +
Telegram long polling) with four-tier memory in Postgres/pgvector, read-only Canvas + calendar sync, generated
work shifts, five daily briefs, voice replies, an approval/trust-ledger system for actions, and Gmail triage
with approval-gated replies. Three adversarial review passes were run on v1. The first two found real bugs, all
fixed with regression tests. The third found nothing high or medium.

**After v1** (same day), these features were added, each with tests:
- timed reminders (typed, spoken or captured)
- `/today` and `/week` with no model involved
- Canvas change alerts
- a watchdog that messages him when something breaks
- the `POST /capture` intake for the Capture app
- promises
- a Sunday weekly review
- nightly JSON backups
- `CLAUDE.md` and a SessionStart hook

A review pass over those additions was started; check git log for any "review fixes" commits after `9cffd41`.

**She has never run against the real services.** This sandbox can't reach Telegram, Groq, Canvas, Google or
Supabase. Everything external is tested against local stubs, and the real-model eval (via `claude -p`) scores
21/21. The next real milestone is Landen deploying it.

---

## What's built (all on main, all tested)

| Area | What | Where |
|---|---|---|
| P0 spine | 4 memory tiers, provider router (free→paid lever), `Reply(speech, detail)` contract, hard lines in code, Telegram, FastAPI | `sloane/agent.py`, `router.py`, `contract.py`, `memory/`, `telegram.py`, `main.py` |
| Retrieval | hybrid vector + full-text, RRF k=60, recency decay; untrusted rows excluded | `memory/store.py` `search_episodes` |
| P1 school | Canvas (GET only), secret `.ics` calendar (RRULE, EXDATE, RECURRENCE-ID, UNTIL fixes), generated 3–7 PM shifts, course matching, `/sync` | `sloane/school/` |
| P2 rhythm | morning_brief 6:35, pre_shift 14:45 Mon–Fri, post_shift 19:05 Mon–Fri, wrap 22:00, silent reflection 00:15; entity_sync every 4h; conflicts computed in code; governor (quiet hours 00:00–06:30, budget counts only scheduled `job` calls) | `sloane/jobs/` |
| P3 voice | voice note in → voice note out (Groq Orpheus TTS, Piper fallback, ffmpeg → OGG/Opus); text always survives | `providers/tts.py`, `voice.py` |
| P4 agency | propose → hard lines (on name *and* `really`) → registered only → trusted pairs auto-run (10 clean approvals, 60-day decay, deny/revoke re-gates) → else Approve/Edit/Deny buttons; one edit open at a time | `sloane/agency.py` |
| P4 Gmail | inbox job 7 AM–7 PM every 3h: one batched triage call, bodies stored untrusted + fenced, up to 3 drafts in his voice; `reply` sends only on Approve; school domain (`dcsdk12.org`) → `draft` only (he sends); auto-send only for DMARC-verified, Reply-To==From, non-list mail to a trusted exact pair | `sloane/mail/` |
| Reminders | "remind me at 5 to call Keegan", typed or spoken, or `/remind tomorrow 7am …`. The time comes from a rule-based parser (number words included) and never touches a model. Delivered by an every-minute job; held through quiet hours and marked late; claimed once; retried if the send fails. `/reminders`, `/unremind <n>` | `sloane/reminders.py`, `jobs/briefs.py` `reminders`, `sql/006_reminders.sql` |
| Canvas alerts | entity_sync compares each assignment before/after (`school/changes.py` `classify`); new-and-future, graded (+score), newly missing, due moved → `school_changes` rows → one rule-rendered message (no model), held through quiet hours, claimed once, retried on send failure; first sync is a silent baseline; `/sync` announces immediately | `sloane/school/changes.py`, `sql/007_school_changes.sql` |
| Watchdog | `watchdog` job every 30 min: failed/partial jobs, a lane provider failing every call for 3h (Claude-login hint), dead Gmail grant; told after 60 min grace, repeated every 24h, "working again" on recovery; `alerts` table; jobs can now record `partial` | `sloane/jobs/watchdog.py`, `sql/008_watchdog.sql` |
| Capture intake | `POST /capture` (bearer `CAPTURE_TOKEN` ≥32 chars, constant-time compare; off otherwise; 64 KB body cap; text never logged) → trusted `user` episode `source=capture` dated `captured_at`; "remind me …" → reminder. Reach via `tailscale serve` (DEPLOY §7d). The Capture app side is not built yet | `sloane/capture.py`, `tests/test_capture.py` |
| Promises | `/promise <what> [to Name] [by when]` → commitments row (person via `person_id`, due via the reminder parser, "by <day>" = 8 PM that day); `/promises`, `/kept <n>`. Upcoming reminders are now in FACTS too, so briefs and answers mention them | `sloane/promises.py`, `tests/test_promises.py` |
| Weekly review + backup | `weekly_review` Sunday 19:00 (agent turn with this week's graded/missing/kept record); `backup` 00:30 → JSON of `Store.BACKUP_TABLES` to BACKUP_DIR or `<EMBED_CACHE_DIR>/backups`, atomic write, 14 kept; `scripts/restore_backup.py FILE [--tables] [--apply]` merges rows back (existing rows win, column names checked against the schema) | `jobs/briefs.py`, `sql/009_weekly.sql`, `tests/test_weekly.py` |
| Views | `/today`, `/week`: schedule + due + computed conflicts from SQL, no model | `sloane/views.py`, `tests/test_views.py` |
| Security hardening | `claude -p` runs `--tools ""` `--strict-mcp-config` with a scrubbed env; bot fails closed with no chat id; httpx URL logging off (token/ICS URL); bind 127.0.0.1 unless `BIND_HOST` | various |
| Ops | Dockerfile (arm64), compose (loopback port, `claude-auth` + `models` volumes), systemd unit, `doctor.py` (checks every credential), `seed_state.py`, `seed_courses.py`, `gmail_auth.py` (stdlib PKCE consent → writes `.env`), `eval.py` (golden questions, real model) | root, `scripts/` |
| Agent setup | `CLAUDE.md` (invariants + how to work) and the `.claude/hooks/session-start.sh` web hook (creates `.venv`, starts pgvector Postgres, exports `DATABASE_URL`) | root, `.claude/` |
| CI | `test.yml` (py3.12 + pgvector, migrations applied twice), `image.yml` (linux/arm64 build + smoke), Dependabot | `.github/` |

Telegram commands: `/today /week /brief /jobs /sync /inbox /remind /reminders /unremind /promise /promises /kept /trust /revoke /cancel /usage /state /help`, plus plain "remind me …".
HTTP (loopback only, or your tailnet via `tailscale serve`): `/health /usage /state /facts /jobs POST /sync POST /jobs/{name}/run`, plus `POST /capture` (token).

## Invariants (a violation is a bug even if tests pass)

1. Every model call goes through `sloane/router.py`.
2. Every SQL statement lives in `sloane/memory/store.py` (AST-enforced by `tests/test_invariants.py`).
3. FACTS (SQL) beats RECALL (similarity). Never answer a date from recall.
4. Hard lines are code (`agent.py` `HARD_LINES`), checked before any side effect; the `trust` rows mirror them.
5. Memory writes never block a reply (`remember()`).
6. `Reply` is built only in agent/telegram/contract. Jobs send via the agent or `bot.say`.
7. Ingested content is data: `trusted=false`, fenced as INGESTED, fences defused with `ingest.unfence`.
8. School systems are read-only. She never submits work and never emails school staff.

---

## In flight

Nothing half-done. (Update this section before stopping if something is.)

## Backlog (ideas, in priority order)

1. **Capture → Sloane client** (in the capture repo): a settings screen for URL + token, and a POST after each
   transcription. Sloane's side (`POST /capture`) is done and documented in DEPLOY §7d.
2. Infinite Campus (grades), deliberately out of v1. Needs district credentials, and repeated automated logins can
   lock the account.
3. P5 phone calls, beyond v1.

## Only Landen can do (the whole list; see DEPLOY.md)

1. Create the Oracle Cloud ARM instance (DEPLOY §1).
2. On the box: clone, fill `.env` (he already has the values), `docker compose build`, apply migrations with the
   one-liner loop (DEPLOY §2–5).
3. `docker compose run --rm sloane claude` once to log the Claude CLI in (DEPLOY §6).
4. Seed + `doctor.py --warm`, then `systemctl enable --now sloane` (DEPLOY §7–8).
5. Optional Gmail (DEPLOY §7c): a Google Cloud Desktop OAuth client, **published In production** (Testing tokens
   die after 7 days), then `python3 scripts/gmail_auth.py` on the box.
6. Open decision, not set up: a nightly cloud routine that keeps improving the repo. It would spend his Claude limits.

Security rules he follows: never paste tokens or credentials into chat. Credentials live only in `.env`
(gitignored, chmod 600). The ICS URL and the Canvas token are passwords.

---

## How to work on it (for the next session)

```bash
# local dev in this kind of sandbox
python3.11 -m venv /tmp/sv2 && /tmp/sv2/bin/pip install -r requirements.txt pyflakes
sh scripts/dev_db.sh      # Postgres 16 + pgvector on /tmp:5433; creates sloane, sloane_eval, sloane_review; applies sql/*
DATABASE_URL="postgresql://postgres@/sloane?host=/tmp&port=5433" /tmp/sv2/bin/python tests/run.py   # all suites
/tmp/sv2/bin/python scripts/eval.py 'postgresql://postgres@/sloane_eval?host=/tmp&port=5433'       # real model, ~7 calls
```

- Integration tests are **destructive**: point them at a throwaway database. Give review agents their own
  database (e.g. `sloane_review`). A reviewer once wiped the shared DB's hard-line rows; re-running `001` restores them.
- The sandbox proxy blocks Hugging Face, Groq, Telegram, Canvas, Google and Supabase. PyPI, GitHub and `claude -p` work.
- Every fix gets a regression test that fails on the old code. Commit messages end with the session attribution lines.
- CI must be green on main after each push (test ≈1 min, arm64 image ≈2–8 min).
