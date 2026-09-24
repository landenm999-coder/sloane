# Sloane — handoff

**Read this first if you are a new Claude Code session picking up Sloane.**
This file records what exists, what's verified, what's in flight and what's left.
It's kept up to date at the end of every work session. Last updated: 2026-09-24.

- Repo: `github.com/landenm999-coder/sloane`, branch `main` (commit straight to main; CI runs on push).
- **The skills build-out (2026-09-24) is on branch `claude/cloud-credits-build-nh2vtj`, draft PR
  landenm999-coder/sloane#12, not yet on main.** Everything under "Skills build-out" below lives there
  until that PR is merged.
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

**After v1**, these were added, each with tests:
- timed reminders (typed, spoken or captured) with snooze buttons
- `/today`, `/week`, `/grades` and `/status`, all with no model involved
- Canvas change alerts, including course-grade moves
- a watchdog that messages him when something breaks
- the `POST /capture` intake for the Capture app
- promises
- a Sunday weekly review
- optional voice briefs
- nightly JSON backups, with a merge-restore script
- `scripts/install.sh`, the one-command deploy and upgrade (shellchecked in CI)
- `/done` for work handed in on paper
- long replies split across messages instead of truncated
- `CAPTURE_API.md`, the contract for the Capture client
- `CLAUDE.md` and a SessionStart hook

The post-v1 work was reviewed twice: by hand (`c7c88a7`), then by an adversarial review agent (`217d9ae`, no high findings; the medium and every low were fixed with tests, including the installer running correctly under `curl | bash`). The real-model eval grew to 30 checks and passes 30/30.
Test suites: 21/21.

**Skills build-out (PR #12).** The skills registry got the things it was designed for:
- a `heartbeat` job: every quarter hour, 7 AM–10 PM, no model. It says each skill nudge once.
- eleven skills: lists, countdowns, weather, flashcards and quizzes, habits, clients, a study plan, focus,
  birthdays, money, memory
- the `/tv` dashboard
- `sloane/dates.py`
- Capture running skill rules

Two adversarial review rounds found and fixed 20 real bugs, each with a regression test.

**The partner upgrade (same PR).** Landen asked for her to feel like JARVIS: a conversational partner, fast and
informed, not a slow assistant. What changed:
- `claude -p` now gets her persona as *the* system prompt. It used to be appended to Claude Code's
  coding-assistant prompt, so she introduced herself as a coding helper.
- A new persona with a character.
- A `CONVERSATION` tier: the recent messages ride in every prompt.
- A warm, streamed CLI process with "typing" and edit-in-place replies. First words arrive in about 2 s.
- Actions on request (`sloane/actions.py`).
- Web lookups (CLI with WebSearch/WebFetch only, results as INGESTED).
- Nightly learning of follow-ups and facts from his own words (`memory/learn.py`, the `learn` job,
  `/memory`).

A third review pass, on the partner upgrade, found ten more issues, all fixed with tests:
- Replies built from email or web text are now `tainted`: stored untrusted, and shown only as a placeholder in
  CONVERSATION.
- Every command has its own rule, and `/done` is off the allowlist.
- "Only what he asked for" is checked in code (`actions.grounded`).
- The warm process no longer races or leaks, and it's recycled on a timer.
- An old CLI is detected even when it breaks the pipe.
- Recall is trimmed only for what CONVERSATION shows.
- A live reply whose final edit fails is sent whole.

Suites: 42/42. Real-model eval: 52/52 (the one soft check, "small talk isn't a briefing", varies between runs).
It now includes a follow-up that needs the conversation, small talk, an action and a non-action.

**She has never run against the real services.** This sandbox can't reach Telegram, Groq, Canvas, Google or
Supabase. Everything external is tested against local stubs, and the real-model eval (via `claude -p`) scores
30/30. The next real milestone is Landen deploying it.

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
| Voice briefs | `VOICE_BRIEFS` (comma job names) → those briefs go via `JobContext.speak` (= `bot.reply(as_voice=True)`, text fallback) | `jobs/briefs.py` `_brief`, `main.py` |
| Snooze + status | delivered reminders carry Snooze 10m / 1h / Tomorrow 7am / Done buttons (`r:<uuid>:<code>` callbacks, same owner-only checks as approvals; a snooze is a new reminder row, source=snooze); `/status` health summary with no model | `reminders.py` snooze helpers, `telegram.py` `remind`/`_snooze`/`_status` |
| Done locally | `/done <words>` (unique match over open/missing titles+course) → `assignments.done_locally` (sql/011), excluded from due/overdue/working set, never written by sync; suppresses a later Canvas "missing" alert | `telegram.py` `_done`, `store.outstanding_assignments` |
| Grades | Canvas `courses?include[]=total_scores` → `courses.current_score/current_grade` (sql/010); in FACTS CLASS lines and `/grades`; ≥2-point moves become a `grade` change alert; hidden totals stay unknown | `school/canvas.py` `_current_grade`, `school/sync.py` |
| Views | `/today`, `/week`: schedule + due + computed conflicts from SQL, no model | `sloane/views.py`, `tests/test_views.py` |
| Security hardening | `claude -p` runs `--tools ""` `--strict-mcp-config` with a scrubbed env; bot fails closed with no chat id; httpx URL logging off (token/ICS URL); bind 127.0.0.1 unless `BIND_HOST` | various |
| Ops | Dockerfile (arm64), compose (loopback port, `claude-auth` + `models` volumes), systemd unit, `doctor.py` (checks every credential), `seed_state.py`, `seed_courses.py`, `gmail_auth.py` (stdlib PKCE consent → writes `.env`), `eval.py` (golden questions, real model) | root, `scripts/` |
| Agent setup | `CLAUDE.md` (invariants + how to work) and the `.claude/hooks/session-start.sh` web hook (creates `.venv`, starts pgvector Postgres, exports `DATABASE_URL`) | root, `.claude/` |
| CI | `test.yml` (py3.12 + pgvector, migrations applied twice), `image.yml` (linux/arm64 build + smoke), Dependabot | `.github/` |
| Heartbeat (PR #12) | `heartbeat` job `5,20,35,50 7-22 * * *`: `skills.nudges()` → new keys claimed in `nudges_said` in one statement → one message; failed send unclaims; quiet hours hold; keys unoffered 30 days pruned. No model | `jobs/briefs.py` `heartbeat`, `sql/014`, `tests/test_heartbeat.py` |
| Dates (PR #12) | `dates.find(text, today, future=)` → `Found(day, span)`: ISO, 5/22, may 22, 22 may, the 30th, in N days/weeks/months, today/tomorrow/yesterday, (next) weekday; "sat/sun/wed" need a qualifier; impossible dates refused; `remove()` strips the date and its connecting words | `sloane/dates.py`, `tests/test_dates.py` |
| Skills (PR #12) | lists (015), countdowns (016), weather (Open-Meteo, `WEATHER_LOCATION`), cards + `/quiz` sessions (017, Leitner, rule marking, `/cards make` is the one model call), habits (018), clients (019), plan (020 `estimate_minutes`, `PLAN_*`), focus (021, end = a reminder row), birthdays (022, on `people`), money (023 + `skill_settings`, `PAY_RATE`) | `sloane/skills/*.py`, one `tests/test_<skill>.py` each |
| TV (PR #12) | `GET /tv`: self-contained HTML (CSP `default-src 'none'`), everything escaped, refresh 60 s, clock ticks; SQL + `skills.panels()`; a failed read is a banner. `GET /panels` JSON | `sloane/dashboard.py`, `tests/test_dashboard.py` |
| Partner (PR #12) | persona rewritten (character, register, no help-desk phrases, `ADDRESS_AS`); `--system-prompt` replaces Claude Code's; CONVERSATION tier (`recent_messages`, 24 msgs/12 h, 1,500 tok, voice transcripts logged); warm stream-json `claude -p` (`prewarm`, one turn per process, refill, `close_all`), `partial_reply` + `_Live` edit-in-place streaming, typing indicator, memory writes after the reply (`Agent.settle`); `actions.py` allowlist + `Bot._act`; `look` → `Router.research` (CLI, WebSearch/WebFetch only) → INGESTED second turn; `learn` job (sql/024) + `memory` skill | `persona.py`, `providers/claude_code.py`, `contract.py`, `telegram.py`, `agent.py`, `actions.py`, `memory/learn.py`, `skills/memory.py`; tests `test_conversation`, `test_claude_stream` (+ `fake_cli.py`), `test_live`, `test_actions`, `test_lookup`, `test_learn` |
| Capture + skills (PR #12) | a capture that is exactly a skill phrase is acted on (`action` in the 201 body); `Registry.route(sessions=False)` so a capture is never a quiz answer | `capture.py`, `CAPTURE_API.md` |

Telegram commands (all listed by `/help`): `/today /week /grades /done /status /brief /jobs /sync /inbox /remind /reminders /unremind /promise /promises /kept /trust /revoke /cancel /usage /state /help`, plus plain "remind me …".
Skill commands (PR #12): `/list /countdown /weather /card /cards /quiz /habit /habits /did /client /clients /plan /estimate /focus /birthday /birthdays /spent /budget /memory /forget /followup /followups /end`, plus plain phrases (each skill's docstring lists them).
HTTP (loopback only, or your tailnet via `tailscale serve`): `/health /usage /state /facts /jobs /tv /panels POST /sync POST /jobs/{name}/run`, plus `POST /capture` (token).

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

- **PR landenm999-coder/sloane#12 (draft)**: the skills build-out plus the partner upgrade. It's complete and
  green (42/42 suites, eval 52/52), waiting on review and merge to main. After merging, the box needs
  `git pull`, a rebuild and the migrations (`sql/014`–`025`, all idempotent). `install.sh` does all of that.

## Backlog (ideas, in priority order)

1. **Capture → Sloane client** (in the capture repo): settings for URL + token, a POST after each transcription,
   and an offline retry queue. Sloane's side (`POST /capture`) is done. The exact contract is in
   **`CAPTURE_API.md`**, with the setup in DEPLOY §7d.
2. Infinite Campus (grades), deliberately out of v1. Needs district credentials, and repeated automated logins can
   lock the account.
3. P5 phone calls, beyond v1.
4. More skills, if he wants them: a college-applications tracker (deadlines with checklists, heads-up
   nudges; it's application season), a stock watchlist (quotes only, since trading is a hard line), and a
   DECA roleplay practice session (the model plays the judge). Each is one module plus a migration.
5. Streaming for the Groq and Anthropic providers (only `claude_code` streams today; the others answer whole).
   Also possible: a British voice for full JARVIS (a Piper `en_GB` voice via `PIPER_VOICE`, or a paid TTS).
6. The eval shows her mentioning the planted calendar injection in almost every answer. That's correct but
   noisy. Consider flagging an ingested injection once (a watchdog-style alert) rather than on every turn.

## Only Landen can do (the whole list; see DEPLOY.md)

1. Create the Oracle Cloud ARM instance (DEPLOY §1).
2. SSH in and run the one-command installer (DEPLOY "The fast way"):
   `curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash`.
   It asks for the seven `.env` values and walks him through the one Claude login. It's also the upgrade
   command. (The manual steps are DEPLOY §2–8.)
3. Optional Gmail (DEPLOY §7c): a Google Cloud Desktop OAuth client, **published In production** (Testing tokens
   die after 7 days), then `python3 scripts/gmail_auth.py` on the box.
4. Open decision, not set up: a nightly cloud routine that keeps improving the repo. It would spend his Claude limits.

Security rules he follows: never paste tokens or credentials into chat. Credentials live only in `.env`
(gitignored, chmod 600). The ICS URL and the Canvas token are passwords.

---

## How to work on it (for the next session)

```bash
# local dev in this kind of sandbox
python3.11 -m venv /tmp/sv2 && /tmp/sv2/bin/pip install -r requirements.txt pyflakes
sh scripts/dev_db.sh      # Postgres 16 + pgvector on /tmp:5433; creates sloane, sloane_eval, sloane_review; applies sql/*
DATABASE_URL="postgresql://postgres@/sloane?host=/tmp&port=5433" /tmp/sv2/bin/python tests/run.py   # all suites
/tmp/sv2/bin/python scripts/eval.py 'postgresql://postgres@/sloane_eval?host=/tmp&port=5433'       # real model, ~11 calls
```

- Integration tests are **destructive**: point them at a throwaway database. Give review agents their own
  database (e.g. `sloane_review`). A reviewer once wiped the shared DB's hard-line rows; re-running `001` restores them.
- The sandbox proxy blocks Hugging Face, Groq, Telegram, Canvas, Google and Supabase. PyPI, GitHub and `claude -p` work.
- Every fix gets a regression test that fails on the old code. Commit messages end with the session attribution lines.
- CI must be green on main after each push (test ≈1 min, arm64 image ≈2–8 min).
