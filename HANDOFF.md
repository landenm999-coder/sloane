# Sloane — handoff

**Read this first if you are a new Claude Code session picking up Sloane.**
This file records what exists, what's verified, what's in flight and what's left.
It's kept up to date at the end of every work session. Last updated: 2026-09-28.

- Repo: `github.com/landenm999-coder/sloane`, branch `main` (CI runs on push).
- landenm999-coder/sloane#12 (skills, partner, colleges, DECA), landenm999-coder/sloane#13 (capture recall,
  Groq model), landenm999-coder/sloane#14 (memory, control room, think, local models) and
  landenm999-coder/sloane#15 (the workshop: she builds on herself) are **merged**. The control room
  rebuild is draft PR landenm999-coder/sloane#16.
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
- thirteen skills: lists, countdowns, weather, flashcards and quizzes, habits, clients, a study plan, focus,
  birthdays, money, memory, college applications, DECA role-play practice
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

**College applications (same PR, after the partner upgrade).** It's his application season, so a twelfth
skill tracks each school's plan (EA/ED/RD...), deadline and checklist, with nudges 14/7/3/1 days out and one
if a deadline passes unsubmitted. "I sent my Boulder app" gets recorded through the actions path, and
`grounded()` now requires him to have *said* the status ("sent", "got in", "deferred"). Naming the school
isn't enough, because marking an application submitted silences its deadline.

**Overnight before first deploy (2026-09-26).** Readiness work, all tested:
- `START_HERE.md`, the whole setup step by step. The installer now also asks what she should call him, whether
  she's British, and whether the morning brief should be spoken.
- A hello once per version, so he knows the install worked.
- Canvas and the calendar sync at startup when stale, not at the next 4-hour slot.
- Her Markdown reaches Telegram as HTML (it was showing raw `**`), links show their real domain, and tables
  become bullets. `/help` is regrouped.
- `/college add` takes several schools at once.
- Plain-words rules now see through "Sloane," in front and emoji or emoticons after, and "let's do a
  marketing roleplay" starts one.
- A full boot test against a fresh database and a fake Telegram: hello, sync, `/help`, colleges and a real
  streamed reply all worked end to end. It's now automated (`tests/test_boot.py`).
- The installer checks each pasted value and asks again with the reason (`scripts/env_check.py`; doctor
  runs the same checks). `.env.example` had shown the transaction pooler; fixed.
- Canvas without a token: `CANVAS_FEED_URL`, for districts that turn student tokens off.
- Running her on his PC under WSL when Oracle has no capacity; the installer stops early, with the fix,
  when systemd is off.
- Telegram's "/" command menu, built from `/help`'s own lines.
- The arm64 image test now fetches the British voice, speaks and makes a voice note (75 s emulated; passes).
- `sql/999_lock_public.sql`: row-level security everywhere, so Supabase's auto API sees nothing.
- The hello retries until he presses Start in the bot's chat, and the nightly prune no longer forgets it
  (that bug would have re-sent the first-install hello a month later).

Suites: 50/50. Real-model eval: 58/58 (the one soft check, "small talk isn't a briefing", varies between runs).
It now includes a follow-up that needs the conversation, small talk, an action, a non-action, a college
FACTS question and a college action.

**2026-09-28 (PR #14).** Picked up what the 09-27 session left unmerged, then what Landen asked for
("remember like a real person, talk about anything, think on her own, a dashboard to see and control
everything and talk to her, local models on a Pi, and edit herself"):
- *Unmerged work, rescued*: repeating reminders, timers, forwards read as someone else's words (three
  commits on the old branch, cherry-picked). Reviewed; 25 checks for what the review found: an "every …"
  phrase in the reminder's text isn't a schedule, "starting tomorrow", calendar dates in reminders (via
  `dates.py`; "the 21st at 3" had become tomorrow 3 AM), "at 8 start the essay" (the "st" bug), compound
  timers, timers not held by quiet hours.
- *Memory like a person's*: recall keeps seats (`RECALL_LASTING`) for the most relevant rows however old
  (decay alone buried everything past a month); STATE puts his seeded facts, then learned ones the
  question touches, then the newest, and says how many it cut; "remember that …" is kept at once (and
  re-pins a forgotten fact); the learner reads captures, sees what it knows, updates and retires facts,
  and writes a nightly diary line (a recall episode); CONVERSATION is a day. Found: the recall de-dup
  never matched real chat turns (stored as `text`/`voice`, not `telegram`); an installed `.env` pins old
  defaults forever (GROQ_MODEL stayed on the retired model), so `scripts/env_migrate.py` moves them on
  at upgrade.
- *The control room* (`/app`, `sloane/web.py`, `sloane/webui/`): talk to her (same `Bot.respond` as
  Telegram, streamed) and see/steer everything. Password `DASHBOARD_TOKEN` (installer makes it),
  signed HttpOnly SameSite=Strict session, `X-Sloane` header on every change, lockout after five wrong.
  Checked in Chromium at desktop and phone sizes, dark and light, with no console or CSP errors.
- *She thinks*: the `think` job (sql/030) says the one thing worth saying, or nothing; only offers to act.
- *Local models*: `providers/local.py` (any OpenAI-compatible server: Ollama on a Pi), streaming, last
  fallback in both lanes or first with `BULK_PROVIDER=local`; local Whisper optional; `LOCAL_MODELS.md`.
- *Prose replies*: when the model skips the JSON, the lead paragraph is speech and the rest detail
  (it used to flatten everything into speech and repeat it), and prose streams live.
- *Self-editing*: not in PR #14 (it needed Landen's explicit go-ahead). He gave it; it's "The workshop" below.

Suites: 53/53. Real-model eval: 68/68 (10 new checks: memory, diary recall, talk, think).

**The workshop (after PR #14, same day).** Landen: "an actual workshop where I can plan out new things I want
her to have and she will add those herself with my approval ... at nighttime with the cheaper models she
could be building on herself ... so that when I wake up there's a pipeline of new features and I can accept
or deny them." Built (`sloane/workshop.py`, `sql/031`, the control room's Workshop tab, `/idea`, `/workshop`):
- *The pipeline*: idea → planned (a short plan in her voice) → queued (Build, or Build now) → building →
  ready for him → Accept → live (with Undo), or Deny (his reason is kept and fed to her next ideas).
- *The build*: a Claude Code session (`Router.code`, `WORKSHOP_MODEL=sonnet`) in her own clone of the repo on
  a volume, never the running code. File tools scoped to the clone, Bash only for the tests, pyflakes and
  read-only git; deny rules on /proc, /etc, /home, /root, /var... except the clone itself (a deny rule
  covering the clone blocked every write: found by a live run); no credentials in its environment.
- *The guard, in code* (`workshop.guard`): nothing in `deploy/protected.txt` (hard lines, agency, actions,
  ingest, school, mail, providers, the control room, CI, the installer, the Dockerfile, requirements,
  CLAUDE.md, the safety tests); the locked definitions (`HARD_LINES`, `ensure_allowed`, `Bot._handle`,
  `_act`, `_offer`, forwards, `Reply`, the recall fence); no deleted test, no suite dropped from run.py,
  no monkeypatching sloane from a skill, no secret (her own values or known token shapes) in the diff;
  migrations only add (her own tables, a column elsewhere, a job row: no delete/update/drop/truncate on
  another table, no grants, no EXECUTE, no changing a migration already on main) and no new write to the
  trust ledger, proposals or the workshop's own table, from a migration or `store.py`. Then
  the unit suites and pyflakes, run by code. Then a branch `sloane/<slug>-<id>`, a PR, and GitHub CI
  (read through the Actions runs API).
- *Accept*: squash-merge (pinned to the tested sha), then `.deploy/request.json`. On the box a systemd path
  unit (`deploy/sloane-upgrade.path`) runs `scripts/upgrade.sh`: pull, refuse a "Workshop:" commit that
  touched a protected file (checked against the list as it was *before* the pull), build, migrate,
  restart, wait for /health, roll back if she doesn't come back, write `result.json`, which she reads and
  tells him about. Undo is a revert built and checked the same way.
- *Nights*: the `workshop` job (01:10) builds up to `NIGHTLY_BUILDS` queued items, then imagines
  `NIGHTLY_OWN_IDEAS` of her own from the last week of his messages and loose ends (never a denied idea
  again), plans and builds them. Nothing of hers goes live without his Accept. The heartbeat tells him in
  the morning what's waiting.
- *Verified*: `tests/test_workshop.py` (guard, argv, the upgrader against stub docker/systemctl/curl for
  live, protected refused, rollback on unhealthy and on build failure, and the whole pipeline against a
  stub GitHub and a bare repo); and a live run with the real CLI on Sonnet in a clone: "a /coin command"
  was planned, built in 5½ minutes into `skills/coin.py` + `tests/test_coin.py` (listed in run.py, README
  and HANDOFF updated), passed the guard, the suite (55/55) and pyflakes, and was committed and pushed. It
  stopped at CI only because that run's fake GitHub reported no CI; CI → ready is covered by the test.
  The first live run found the deny-rule bug above; the second led to titles cut at the first sentence
  and summaries without her working notes (`workshop.closing`).

Suites: 54/54.

**The control room, rebuilt (after PR #15).** Landen: "make the app dashboard as good as possible".
- *Today* opens with her line on the day, written by rule from the agenda's rows (never a model, so never
  wrong about a time), a "Next: Work in 5 h 16 min" chip, the rail redrawn in lanes (work, events, due
  and reminder pins that never collide or clip), and an agenda from `views.day_items` (the same rows
  and clashes as /today, now as data): past quieter, now highlighted, next with a countdown.
- *Needs you* is one list: approvals, alerts, unreadable parts, failed jobs (Run again), overdue work,
  workshop builds ready (Review). The top bar's light counts failed jobs (`overview.health`); it used
  to say "All systems normal" while the inbox job was failing (regression test in test_web).
- *Talk*: a microphone (`POST /api/voice`: WebM/Ogg/MP4 from the browser's recorder, 8 MB cap, the
  Telegram voice path's transcriber via `Bot.transcribe`, logged as a voice note in words, never the
  audio; shown only when `Bot.hears` and the page is https), **Read aloud** (the browser's own speech,
  British when her Piper voice is), starters from what's on today, `/` to jump to the message box.
- *Phone*: the five tabs fit (icon bar; the fifth used to be cut off), badges on the icons, 44px touch
  targets, home-screen icons (`icon-180/192/512.png`, manifest, apple-touch-icon).
- *Memory* filter and counts; *Workshop* pipeline counts and files labelled skill/test/data/docs;
  *Engine* status tiles, friendly job names, failed first.
- *Verified*: screenshots at desktop, laptop and phone, dark and light, console clean; the microphone
  end to end in Chromium (fake mic → WebM → stub Whisper → her real reply via `claude -p`); every
  colour pair computed (all ≥ 4.5:1 text, ≥ 3:1 borders and markers, both themes); keyboard order,
  accessible names, ARIA references and heading order checked in the browser.

**She has never run against the real services.** This sandbox can't reach Telegram, Groq, Canvas, Google or
Supabase. Everything external is tested against local stubs, and the real-model eval (via `claude -p`) scores
58/58. The next real milestone is Landen deploying it.

---

## What's built (all on main, all tested)

| Area | What | Where |
|---|---|---|
| P0 spine | 4 memory tiers, provider router (free→paid lever), `Reply(speech, detail)` contract, hard lines in code, Telegram, FastAPI | `sloane/agent.py`, `router.py`, `contract.py`, `memory/`, `telegram.py`, `main.py` |
| Retrieval | hybrid vector + full-text, RRF k=60, recency decay; untrusted rows excluded | `memory/store.py` `search_episodes` |
| P1 school | Canvas (GET only), secret `.ics` calendar (RRULE, EXDATE, RECURRENCE-ID, UNTIL fixes), generated 3–7 PM shifts, course matching, `/sync` | `sloane/school/` |
| P2 rhythm | morning_brief 6:35, pre_shift 14:45 Mon–Fri, post_shift 19:05 Mon–Fri, wrap 22:00, silent reflection 00:15; entity_sync every 4h; conflicts computed in code; governor (quiet hours 00:00–06:30, budget counts only scheduled `job` calls) | `sloane/jobs/` |
| P3 voice | voice note in → voice note out (Groq Orpheus TTS, Piper fallback, ffmpeg → OGG/Opus); text always survives. Piper (piper-tts, in the image since PR #12) can be the main voice: `SPEAK_PROVIDER=piper PIPER_VOICE=en_GB-cori-medium` is British; a voice by name is fetched once into EMBED_CACHE_DIR at startup (atomically; a reply never waits on it), loaded once and kept in memory (~0.2 s per reply measured) | `providers/tts.py`, `voice.py`, `tests/test_piper.py` |
| P4 agency | propose → hard lines (on name *and* `really`) → registered only → trusted pairs auto-run (10 clean approvals, 60-day decay, deny/revoke re-gates) → else Approve/Edit/Deny buttons; one edit open at a time | `sloane/agency.py` |
| P4 Gmail | inbox job 7 AM–7 PM every 3h: one batched triage call, bodies stored untrusted + fenced, up to 3 drafts in his voice; `reply` sends only on Approve; school domain (`dcsdk12.org`) → `draft` only (he sends); auto-send only for DMARC-verified, Reply-To==From, non-list mail to a trusted exact pair | `sloane/mail/` |
| Reminders | "remind me at 5 to call Keegan", typed or spoken, or `/remind tomorrow 7am …`. The time comes from a rule-based parser (number words included) and never touches a model. Delivered by an every-minute job; held through quiet hours and marked late; claimed once; retried if the send fails. `/reminders`, `/unremind <n>` | `sloane/reminders.py`, `jobs/briefs.py` `reminders`, `sql/006_reminders.sql` |
| Canvas alerts | entity_sync compares each assignment before/after (`school/changes.py` `classify`); new-and-future, graded (+score), newly missing, due moved → `school_changes` rows → one rule-rendered message (no model), held through quiet hours, claimed once, retried on send failure; first sync is a silent baseline; `/sync` announces immediately | `sloane/school/changes.py`, `sql/007_school_changes.sql` |
| Watchdog | `watchdog` job every 30 min: failed/partial jobs, a lane provider failing every call for 3h (Claude-login hint), dead Gmail grant; told after 60 min grace, repeated every 24h, "working again" on recovery; `alerts` table; jobs can now record `partial` | `sloane/jobs/watchdog.py`, `sql/008_watchdog.sql` |
| Capture intake | `POST /capture` (bearer `CAPTURE_TOKEN` ≥32 chars, constant-time compare; off otherwise; 64 KB body cap; text never logged) → trusted `user` episode `source=capture` dated `captured_at`; "remind me …" → reminder. Reach via `tailscale serve` (DEPLOY §7d). The Capture app side is landenm999-coder/capture#2 | `sloane/capture.py`, `tests/test_capture.py` |
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
| Telegram formatting (PR #12) | every message used to go as plain text, so her Markdown detail (`**CU Boulder**`, `` `/college …` ``, `/help`'s own backticks) showed its asterisks and backticks. `Bot._call` now sends text that has formatting as Telegram HTML (`sloane/tgformat.py`: bold, code, fenced blocks, http(s) links shown with their real domain beside them, headings, bullets; everything else escaped; unpaired markers left alone, so streaming edits stay well-formed) and resends the plain text if Telegram refuses to parse it. `/help` is regrouped: just talk, every day, skills, behind the scenes | `tgformat.py`, `telegram.py`; `tests/test_format.py` |
| Sync at startup (PR #12) | `Scheduler.catch_up`: at startup, `entity_sync` runs at once if it never ran, last failed, or is over 4 h old. Before, a fresh install knew nothing from Canvas or the calendar until the next 4-hourly slot, so "what's due tomorrow?" had nothing to go on. Also: Markdown tables in her detail become one bullet per row on Telegram | `jobs/scheduler.py`, `main.py`, `tgformat.py`; `test_jobs`, `test_format` |
| Hello (PR #12) | at startup, once per version of her code (a hash of `sloane/**/*.py`): "Sloane here, up and running…" after an install, "Updated and back up…" after an upgrade, nothing after a plain restart. A failed send (Telegram refuses until he has pressed Start in the bot's chat) is retried for about half an hour, then next start; it's recorded only once said, and the nightly nudge prune keeps `hello:` keys (it used to drop them after 30 days, so a reboot a month later re-sent the first-install hello). The sign an install worked. Also `START_HERE.md`, the step-by-step setup, and the installer now asks what she should call him and whether she's British | `sloane/hello.py`, `main.py`, `tests/test_hello.py`, `scripts/install.sh` |
| Boot test (PR #12) | the whole app started as the box starts it, against a fake Telegram: hello once (not again on restart), every job's cron loads, the stale sync catches up, /help and /status answered and logged, /help sent as HTML, /health ok. No model call | `tests/test_boot.py` |
| Paste checks (PR #12) | the installer checks each value as it's pasted and asks again with the reason: `[YOUR-PASSWORD]` left in, transaction pooler, direct (IPv6) connection, a password symbol that breaks the URL or compose's `$`, half a bot token, a username for the chat id, a Canvas page, the calendar's public or web link. Cleans quotes, spaces, `bot` prefixes, `webcal://`. Doctor's `settings` line runs the same checks on a hand-edited `.env`. `.env.example` now shows the session pooler (it showed 6543) | `scripts/env_check.py`, `install.sh`, `doctor.py`, `tests/test_env_check.py` |
| Canvas without a token (PR #12) | `CANVAS_FEED_URL`, used only when `CANVAS_TOKEN` is blank (some districts turn student tokens off): assignments from Canvas's Calendar Feed .ics. Due dates only; ahead is `open`, past is `unknown` (never overdue: the feed can't say it went in). Rows keyed as the API keys them (source `canvas`, the assignment and course ids), so adding a token later updates them instead of doubling. The installer asks for it when the token is left blank; doctor checks it | `sloane/school/canvas_feed.py`, `school/sync.py`, `tests/test_canvas_feed.py` |
| PC fallback (PR #12) | START_HERE shows running her on his Windows PC under WSL (Ubuntu 24.04) with the same installer, for when Oracle is out of capacity; memory is in Supabase so moving later loses nothing. The installer stops at once, with the fix, when systemd is off or Docker isn't a system service (Docker Desktop's WSL integration). Also the `icacls` fix for an "unprotected" SSH key | `START_HERE.md`, `scripts/install.sh` |
| Command menu (PR #12) | at every start she sets Telegram's "/" menu for his chat only (scope: his chat), built by `telegram.menu()` from the same lines `/help` shows (`EVERYDAY_HELP`, skills' `help`, `BEHIND_HELP`), so a new skill appears on its own. A failure is logged, never fatal | `sloane/telegram.py`, `main.py`, `tests/test_format.py`, `tests/test_boot.py` |
| Supabase API lockdown (PR #12) | `sql/999_lock_public.sql`, sorted last and re-applied by every install and upgrade: row-level security on every table in `public` (no policies) and all grants revoked from Supabase's `anon`/`authenticated` roles, so the project's auto REST API sees nothing and the security advisor has nothing to email about. She connects as the tables' owner, which RLS doesn't restrict (verified with a non-superuser owner and Supabase-style default grants). Doctor's `api lockdown` line checks it | `sql/999_lock_public.sql`, `scripts/doctor.py` |
| Planted entries (PR #12) | `ingest.planted()` (narrow: an override phrase *addressed to her*, or an unmistakable marker); the heartbeat tells him once per calendar entry in the next 14 days (`planted:event:<id>`, skills or none); the entry's FACTS line is marked so she neither obeys it nor repeats the warning. Before this she flagged the eval's planted invite in almost every answer | `ingest.py`, `jobs/briefs.py` `planted_nudges`, `memory/tiers.py`; tests in `test_tiers`, `test_heartbeat` |
| DECA (PR #12) | `/roleplay [area]`: a model-written scenario (event, role, judge, situation, five PIs), then a session where the judge stays in character (typed or voice), two follow-up questions after "I'm done", and a score on the DECA form (PIs 0–14, four 21st Century Skills 0–6, overall 0–6) **totalled in code**; `roleplays` table (sql/027), FACTS line with recent scores + "work on", TV panel, an evening nudge when a DECA countdown is ≤14 days out and no practice in 3 days; `/roleplay` on the actions allowlist. Live-checked against the real CLI: a presentation that missed the brief was pushed back on in character and scored 19/100 with specific notes | `sloane/skills/deca.py`, `sql/027`, `tests/test_deca.py` |
| Slow skills (PR #12) | a command or skill still working after 0.6 s shows "typing…" (a role-play's judge, `/cards make`); instant ones never flash it. The actions instructions now also say recording what he reports isn't initiative, and that only the "do" list does anything (she once said "marking that off" with no command) | `telegram.py` `SLOW_SKILL_SECONDS`, `actions.instructions`; `test_live` |
| JSON repair (PR #12) | `contract.loads_lenient`/`closed`: the CLI sometimes drops an object's final `}` (1 in 3 scenario calls, live). Unrepaired, a reply came out as raw JSON (read aloud, actions lost). Every model-JSON parser now uses it: `contract.parse`, inbox triage, learn, `/cards make`, deca | `contract.py`; regression checks in test_contract, test_cards, test_learn, test_mail, test_deca |
| Colleges (PR #12) | `/college add CU Boulder EA nov 1`: plan, deadline, the usual six-item checklist (+ his own items with their own dates); `done`/`skip`/`add` by words or number; `submitted`/`admitted`/`deferred`/`waitlisted`/`denied`/`committed`; school named by any run of words, a nickname in brackets, or initials; FACTS line per school with exact dates; nudges 14/7/3/1 days out in the evening, the morning of, and once the morning after an unsubmitted deadline; "what's left for Boulder?" answered without a model; "just finished my Boulder essays" / "sent my Boulder app" / "got into Boulder" / "Boulder deferred me" recorded by **rules** (the model path missed the command ~1 run in 14), only when one of his schools is named by whole words (never via a small word like "of") and every other word belongs to the item; `/college` on the actions allowlist (not drop/skip/reopen) for looser wording, with status words checked by `grounded()` | `sloane/skills/colleges.py`, `sql/026`, `tests/test_colleges.py` |
| Capture contract v2 (PR #12) | `client_id` makes retries safe (`capture_refs`, sql/028: a retry after a lost response gets the first answer, `200` + `duplicate`; one still in flight gets `409`; a failed store releases its claim); `act: false` stores memory only (Capture sets its own reminders and logs its own expenses, so it sends `false` and he isn't told twice); `{"check": true}` tests the connection without storing anything | `capture.py`, `CAPTURE_API.md`, `test_capture` |
| Capture + skills (PR #12) | a capture that is exactly a skill phrase is acted on (`action` in the 201 body); `Registry.route(sessions=False)` so a capture is never a quiz answer | `capture.py`, `CAPTURE_API.md` |

| Reminders+ (PR #14) | repeating (`sql/029`: rule + series; delivering one makes the next; `/unremind` stops it), timers ("set a timer for 1 hour 30 minutes"; woken on the second; delivered even in quiet hours), calendar dates via `dates.py`, "starting tomorrow" | `reminders.py`, `jobs/briefs.py`, `tests/test_reminders.py` |
| Forwards (PR #14) | a forward is someone else's words: logged untrusted, answered as INGESTED (no actions), his note is the question, follow-ups see it for 10 minutes | `telegram.py`, `tests/test_forward.py` |
| Memory upgrade (PR #14) | lasting recall seats; STATE priority + cut note; "remember that" at once; learner: captures, updates, retires, diary (`put_diary`); CONVERSATION 24h/40; de-dup on real chat channels; `env_migrate.py` | `memory/store.py`, `tiers.py`, `learn.py`, `skills/memory.py`, `skills/__init__.py`, `agent.py`; tests in store, tiers, learn, agent, actions, env_check |
| Control room (PR #14, rebuilt after #15) | `/app` + `/api/*`: chat (NDJSON stream through `Bot.respond` via an `Outlet`) typed or by microphone (`/api/voice`), read aloud in the browser; overview (her line, agenda + clashes, rail, one needs-you list, health, due, reminders, grades, panels, memory, diary, jobs, trust, usage, system), controls (cancel reminder, approve/deny, revoke, forget, close loose end, run job); installable (icons, manifest) | `web.py`, `views.day_items`, `webui/`, `telegram.py` `Outlet`/`hears`/`transcribe`, `tests/test_web.py` |
| Think (PR #14) | `think` 10:25/12:25/16:25/20:25, not in class or on a shift, NOTHING = silent and not remembered | `jobs/briefs.py`, `sql/030`, `tests/test_jobs.py` |
| Workshop | ideas → plan → build in a clone (Claude Code, Sonnet, scoped tools) → guard in code (`deploy/protected.txt`, locked definitions, secrets, tests kept) → tests + pyflakes → PR + CI → his Accept → merge → host upgrader (`scripts/upgrade.sh` via a systemd path unit: protected re-check, build, migrate, health, rollback) → live, Undo; nightly builds + her own ideas; `/idea`, `/workshop`, the control room's Workshop tab | `sloane/workshop.py`, `skills/workshop.py`, `sql/031`, `scripts/upgrade.sh`, `deploy/`, `web.py`, `webui/`, `tests/test_workshop.py` |
| Local models (PR #14) | `LOCAL_BASE_URL`/`LOCAL_MODEL` (+ `LOCAL_STT_*`), `Provider.configured` so an unset fallback isn't a failure, doctor + paste checks, compose `host.docker.internal` | `providers/local.py`, `router.py`, `LOCAL_MODELS.md`, `tests/test_local.py` |

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

- **PR landenm999-coder/sloane#16**: the control room rebuilt (see "The control room, rebuilt" above) and
  this file's post-#15 corrections. Rerunning the installer after it merges is all the box needs (no
  migration, no new setting; the microphone uses the `GROQ_API_KEY` she already has).
- **The workshop reaching the box** (landenm999-coder/sloane#15 is merged): rerunning `install.sh` installs
  the upgrader (`.deploy/`, `host.json`, `sloane-upgrade.path`), applies `sql/031` and asks for
  `GITHUB_TOKEN`. Until the token is there the Workshop tab still takes ideas and plans; it just doesn't
  build. Its first real builds haven't happened yet (backlog item 1).
- **PR landenm999-coder/capture#2**: the Capture → Sloane client (Settings → Sloane, an IndexedDB outbox,
  memory-only by default). Tested in Chromium against a real Sloane server; not yet on a phone. Merging it
  deploys it (Vercel); then DEPLOY §7d on the box (`CAPTURE_TOKEN`, `CORS_ORIGINS`, `tailscale serve`).

## Backlog (ideas, in priority order)

1. Whatever the first real week turns up. She has never run against real Telegram, Canvas, the calendar or
   Groq; expect small fixes (DEPLOY's "try everything" table is the checklist). The workshop's first real
   builds on the box are part of that: watch the first few (CI time, how often the guard refuses, whether
   Sonnet's builds pass on the first go).
2. The workshop, next: a "revise" button (his notes → a second build on the same branch, instead of Deny and
   re-add), and showing the diff itself in the control room (today it's the file list, the checks and a
   link to the PR).
3. Infinite Campus (grades), deliberately out of v1. Needs district credentials, and repeated automated logins can
   lock the account.
4. P5 phone calls, beyond v1.
5. Streaming for the Groq and Anthropic providers (`claude_code` and `local` stream; the others answer whole).
6. The control room: her replies in her own voice (Groq/Piper audio, not the browser's), and approvals
   there when Telegram isn't set up (today the agency needs `TELEGRAM_CHAT_ID`).
7. A weekday lunch slot for `think`, if Landen wants one (school hours are skipped today).

## Only Landen can do (the whole list; see DEPLOY.md)

0. Merge landenm999-coder/sloane#16 (the control room) and landenm999-coder/capture#2 (the installer deploys `main`).
0b. Make the workshop's GitHub token (DEPLOY §7h: fine-grained, this repo only; Contents and Pull requests
   read and write, Actions read), then rerun the installer on the box and paste it when asked.
0c. Optional: the control room (START_HERE step 7). Local models are optional too: he has no Raspberry Pi
   yet, and nothing needs one (`LOCAL_MODELS.md` is there if he ever gets one).
1. Create the Oracle Cloud ARM instance (DEPLOY §1).
2. SSH in and run the one-command installer (DEPLOY "The fast way"):
   `curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash`.
   It asks for the seven `.env` values and walks him through the one Claude login. It's also the upgrade
   command. (The manual steps are DEPLOY §2–8.)
3. Optional Gmail (DEPLOY §7c): a Google Cloud Desktop OAuth client, **published In production** (Testing tokens
   die after 7 days), then `python3 scripts/gmail_auth.py` on the box.
4. Optional British voice: `SPEAK_PROVIDER=piper` and `PIPER_VOICE=en_GB-cori-medium` in `.env` (DEPLOY §7b).
5. Capture on the phone: Tailscale on the phone, `CAPTURE_TOKEN` + `CORS_ORIGINS` on the box, then
   Capture → Settings → Sloane (DEPLOY §7d).

Security rules he follows: never paste tokens or credentials into chat. Credentials live only in `.env`
(gitignored, chmod 600). The ICS URL, the Canvas token and the GitHub token are passwords.

---

## How to work on it (for the next session)

```bash
# local dev in this kind of sandbox
python3.11 -m venv /tmp/sv2 && /tmp/sv2/bin/pip install -r requirements.txt pyflakes
sh scripts/dev_db.sh      # Postgres 16 + pgvector on /tmp:5433; creates sloane, sloane_eval, sloane_review; applies sql/*
DATABASE_URL="postgresql://postgres@/sloane?host=/tmp&port=5433" /tmp/sv2/bin/python tests/run.py   # all suites
/tmp/sv2/bin/python scripts/eval.py 'postgresql://postgres@/sloane_eval?host=/tmp&port=5433'       # real model, ~28 calls
```

- Integration tests are **destructive**: point them at a throwaway database. Give review agents their own
  database (e.g. `sloane_review`). A reviewer once wiped the shared DB's hard-line rows; re-running `001` restores them.
- The sandbox proxy blocks Hugging Face, Groq, Telegram, Canvas, Google and Supabase. PyPI, GitHub and `claude -p` work.
- Every fix gets a regression test that fails on the old code. Commit messages end with the session attribution lines.
- CI must be green on main after each push (test ≈1 min, arm64 image ≈2–8 min).
