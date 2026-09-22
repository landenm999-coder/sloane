# Sloane — handoff

**Read this first if you are a new Claude Code session picking up Sloane.**
This file records what exists, what's verified, what's in flight and what's left.
It's kept up to date at the end of every work session. Last updated: 2026-09-22.

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
with approval-gated replies. Three adversarial review passes were run. The first two found real bugs, all fixed
with regression tests. The third found nothing high or medium. **She has never run against the real services.**
This sandbox can't reach Telegram, Groq, Canvas, Google or Supabase. Everything external is tested against
local stubs, and the real-model eval (via `claude -p`) scores 21/21. The next real milestone is Landen
deploying it.

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
| Security hardening | `claude -p` runs `--tools ""` `--strict-mcp-config` with a scrubbed env; bot fails closed with no chat id; httpx URL logging off (token/ICS URL); bind 127.0.0.1 unless `BIND_HOST` | various |
| Ops | Dockerfile (arm64), compose (loopback port, `claude-auth` + `models` volumes), systemd unit, `doctor.py` (checks every credential), `seed_state.py`, `seed_courses.py`, `gmail_auth.py` (stdlib PKCE consent → writes `.env`), `eval.py` (golden questions, real model) | root, `scripts/` |
| CI | `test.yml` (py3.12 + pgvector, migrations applied twice), `image.yml` (linux/arm64 build + smoke), Dependabot | `.github/` |

Telegram commands: `/brief /jobs /sync /inbox /remind /reminders /unremind /trust /revoke /cancel /usage /state /help`, plus plain "remind me …".
HTTP (loopback only): `/health /usage /state /facts /jobs POST /sync POST /jobs/{name}/run`.

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

1. **Canvas change alerts.** During entity_sync, detect new assignments, newly graded or newly missing work. Queue
   them as un-notified and deliver them batched outside quiet hours.
2. **`/today` and `/week`.** Deterministic FACTS tables straight from SQL, no model, so they work when every
   provider is down.
3. **Watchdog job.** Tell Landen once per distinct problem (failing job, degraded provider, stale sync, dead Gmail
   grant, Claude CLI login expired), and again when it recovers.
4. **Capture intake API.** `POST /capture` with a bearer token (`CAPTURE_TOKEN`). Notes and transcripts are stored
   as Landen-authored episodes, with optional commitment extraction. Needs a documented safe way to expose it
   (Tailscale or a Cloudflare tunnel), because the API is loopback-only today.
5. **Repo `CLAUDE.md` + SessionStart hook,** so future web sessions can set up the venv and a pgvector Postgres
   and run tests unattended.
6. Weekly JSON export of state/commitments/trust to the models volume (a cheap backup; Supabase free tier has no PITR).
7. Infinite Campus (grades), deliberately out of v1. Needs district credentials, and repeated automated logins can
   lock the account.
8. P5 phone calls, beyond v1.

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
