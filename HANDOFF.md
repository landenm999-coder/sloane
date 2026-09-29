# Sloane — handoff

**Read this first if you are a new Claude Code session picking up Sloane.**
This file records what exists, what's verified, what's in flight and what's left.
It's kept up to date at the end of every work session. Last updated: 2026-09-29.

- Repo: `github.com/landenm999-coder/sloane`, branch `main` (CI runs on push).
- landenm999-coder/sloane#12 (skills, partner, colleges, DECA), landenm999-coder/sloane#13 (capture recall,
  Groq model), landenm999-coder/sloane#14 (memory, control room, think, local models) and
  landenm999-coder/sloane#15 (the workshop: she builds on herself) and landenm999-coder/sloane#16 (the
  control room rebuilt) are **merged**, and so are landenm999-coder/sloane#20 (talk to her, a life
  dashboard), landenm999-coder/sloane#23 (the recent skill), landenm999-coder/sloane#26 (the control room
  redesigned: one page, a chat that pops in and out, every widget a control) and landenm999-coder/sloane#27
  (Whoop past Cloudflare). All of it is live on the Oracle box, Whoop connected, as of 2026-09-29.
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

**The control room, from the mockup (2026-09-29).** Landen approved a design (`docs/design/dashboard-mockup.html`:
Timeline, dark only, an orb that shows what she's doing, more panels) and asked for it built on real data.
- *Look*: dark only (the light theme is gone), IBM Plex Sans and Mono served from `sloane/webui/fonts/`
  (latin subsets, OFL; CSP gained `font-src 'self'`), the mockup's tokens: blue a class, amber work and the
  workshop, red due, teal now/her, green habits. Flat panels, no shadows, no explainer lines, no serif, no
  gold S (the app icons are the orb now), no emoji.
- *Layout*: desktop (≥1024px) is a rail, the main column and the chat always open on the right (Talk in the
  rail focuses it). Today is a header (orb, mono clock, what she's doing; the date, weather, the countdown
  to work, what's waiting, what's due this week), a timeline (classes, shifts, reminders with cancel, focus
  blocks from the study plan or a running session, due work; a now line that moves each minute; clashes;
  a Tomorrow toggle; a "Coming up" list of later reminders and promises), and a panel grid (container
  queries: 3 columns wide, 2 on a 1024 laptop; the timeline narrows to 240px below 1280). The old Needs-you
  card, due list, reminders and promises cards became the header's "waiting on you" dialog (approve/deny,
  run again, review), timeline rows and "Coming up". Phone: tabs Today/Talk/Workshop/More (More holds
  Memory and Engine), the timeline scrolling in the middle, three panels to swipe.
- *The orb*: one JS function builds each (its own gradient id; bar delays via CSSOM, since CSP forbids style
  attributes). States idle/listening/thinking/speaking/building/needs. The server's part is
  `GET /api/activity` (`web.activity`, polled every 5 s visible, 60 s hidden): needs (a build ready, an
  approval, an alert, a failed job) > building (`Workshop.phase`, in memory: plan/code/test/ci, else
  inferred from the row; deploying is step 5) > thinking (`Scheduler.running`, the ticks reminders,
  heartbeat and watchdog left out) > idle with the next real job. The page adds listening (its bars
  follow the mic through an `AnalyserNode`), speaking (read aloud) and thinking while his message is in
  flight. `?orb=<state>` forces one for review.
- *Panels*: skills' `panel()` gained structured fields beside `lines` (which /tv and Telegram still use):
  weather `hours`/`sky`/`unit`, countdowns `created_on` (the bar), habits `habits` (14 days, streak), focus
  `today_minutes`/`goal_minutes` (`FOCUS_GOAL_MINUTES`, 135)/`running` (the ring counts down), colleges
  `schools` (checklist pips), plan `slots` (timeline blocks). The overview gained `week` (Monday–Sunday
  heat), `work` (next shift, hours this week and last), `engine` (calls today against the budgets, up since),
  `due_week`, `prefs`, and `today` on each learned fact. Choices are `dashboard_prefs` (sql/032, one row,
  text arrays so a backup restores cleanly), `POST /api/prefs`, set in Engine → Panels.
- *Also*: `/` redirects to `/app` (it was a 404); doctor's provider checks ask for 512 tokens (16 came back
  "empty completion" from gpt-oss-120b); Groq sends `reasoning_effort: "low"` for gpt-oss models.
- *Verified*: suites 55/55, pyflakes clean but for the two known hits; in Chromium at 1440, 1024 and 390 (the
  S23's width) against a seeded database: no console or CSP errors, no outside requests, no horizontal
  scroll, every orb state forced and shown, the mic end to end with a fake device (bars follow the level),
  chat streaming, popover, Tomorrow, prefs saved and applied, Forget from "Learned today", 44px tap targets
  on the phone. Every text colour ≥4.5:1 on every surface (`--dim` is only for strokes). Past timeline rows
  are at 45% opacity, as the design asks, which puts their text under 4.5:1.

**She's live on the Oracle box.** An earlier session (not recorded here until now) set her up on an Oracle
Cloud ARM instance (`ubuntu@<its public IP>`, hostname `sloane`; the SSH key is `ssh-key-2026-09-27.key` in
his Downloads). By 2026-09-28 she was running there for real: Telegram polling, the jobs on schedule,
Canvas and the calendar syncing, the Piper British voice, and the workshop building `/coin` and merging it
on his Accept. Upgrading is: SSH in, rerun the installer. With no Tailscale set up on his laptop, the control
room is reached through an SSH tunnel (DEPLOY 7e).

**First night on real data (2026-09-28).** Asked to deploy #18, a session (not knowing about the Oracle box)
walked him through a second install on his Surface, in Ubuntu-24.04 under WSL. It shared the box's
Supabase, so it re-seeded the same starting facts and courses (no harm) and, until it was switched off
(`systemctl disable --now sloane sloane-upgrade.path` there), polled the same Telegram bot. The box was then
upgraded over SSH. That detour found an installer gap (Docker Desktop's `docker` on the Windows PATH made
it skip installing Docker Engine; backlog 8). His screenshots showed what the seeded demo hid: the header said "55 waiting on you" (every missed
Canvas item counted as needing him) and counted down to one of her plan's suggested stretches by its full
assignment title; most of the grid was empty on a new install; Windows drew grey scrollbars with arrows; a
deferred job's "gmail is not configured" was red. Fixed: "waiting" is approvals, what's broken, failed jobs
and builds to accept, with "N overdue" its own link and popover section (a dozen, then "and N older", with
the /done hint); the countdown is shifts, events and a running focus session, clipped to 28 characters; an
"Add to your day" panel (dashed) offers habit/countdown/college/focus commands into the message box while
those panels have nothing; thin dark scrollbars; a deferred job's reason is grey; timeline titles stop at
two lines; the timeline opens scrolled to now.

**Testing here is still against stubs.** This sandbox can't reach Telegram, Groq, Canvas, Google or Supabase:
everything external is tested against local stubs, and the real-model eval (via `claude -p`) scores 68/68. Her
real behaviour is on the Oracle box; ask Landen for screenshots or `doctor.py` output when it matters.

---

**Talk to her, a life dashboard (landenm999-coder/sloane#20 and #23, merged 2026-09-29).** What was "In
flight" until this redesign:

- **Talking to her in the control room** (branch `claude/nifty-thompson-5h6l2x`, draft
  landenm999-coder/sloane#20). Landen: "an assistant for anything I need... speak in conversations, quick,
  not a long wait". Built so far:
  - *A call*: **Talk** in the chat head, or tap the big orb. Hands-free: an `AnalyserNode` level check every
    40 ms (the room's noise followed slowly; 160 ms above the line starts his turn, 850 ms below ends it;
    under 300 ms of voice is a cough, dropped), the recording goes as a voice note (`/api/voice`), she
    answers out loud and listens again. The mic is off while she thinks and talks (she never hears
    herself); tap the orb to cut in, End or Esc hangs up, four quiet minutes hang up.
  - *Her own voice*: `POST /api/speak` (WAV from `Router.speak`, Piper or Groq, accounted as `talk` so
    Telegram's `DAILY_SPEAK_BUDGET` isn't spent), asked sentence by sentence while the reply streams, one
    at a time in order (the next is made while this one plays); the browser's voice when the server has
    none (retried after 5 minutes). The orb's bars follow her audio. The speaker button says every reply.
  - *The fast lane*: `QUICK_PROVIDER` (Groq, default). A spoken turn with nothing to do in it
    (`agent.needs_hands`: remind, add, send, plan, build, look up...) goes to the fast model with a slim
    context (`QUICK_BUDGETS`) and the `persona.QUICK` prompt: short, spoken, and `{"handoff": true}` for
    anything she'd need to act, look up or think hard about. A hand-off, an attempted action, empty speech
    or the fast model down (Groq's free-tier cap) all fall through to the main lane, Claude, as before.
    Typed messages and Telegram are unchanged.
  - Verified: `tests/test_agent.py` (the lane's routing), `tests/test_router.py` (quick lane, speak
    purposes), `tests/test_web.py` (`/api/speak`, `quick` only when spoken); a whole call in Chromium
    with a fake mic at 1440 and 390 wide (two turns, the cut-in, Esc, nothing sent after hanging up).
  Nothing new to paste on the box: `QUICK_PROVIDER` defaults to groq and uses the existing `GROQ_API_KEY`.
- **A life dashboard, not a school tracker** (same branch and PR). Landen: "stop trying to do all this school
  stuff... this dashboard should have everything I need... no empty space". Built:
  - *Today*: the timeline is his calendar, shifts, reminders and a running focus session (`web.agenda` drops
    due work and her study-plan stretches; Telegram's `/today` still lists both). The header counts what's
    waiting on him and emails to answer, nothing from school. Every panel that's on shows (`PANELS`, life
    first), packed like bricks (`app.js pack`: 4px grid rows, each panel spanning its height), and one with
    nothing yet is dashed with what it's for and one button to fill it (`EMPTY`). School is one small panel
    (due soon, overdue and this week's counts opening the list, grades, the next college deadline); grades,
    the week's heat, colleges, flashcards and DECA are their own panels, off by default.
  - *Four skills*: **workouts** (sql/033, logged by rule, a weekly goal, FACTS, a week of bars), **markets**
    (sql/034 `watchlist`, Yahoo chart API + CoinGecko fallback, S&P/Nasdaq/Bitcoin when the list is empty,
    FACTS from the cache only), **news** (Google News RSS, topics in `skill_settings`, headlines are outside
    text: `safe_field`, `Answer(tainted=True)`, never FACTS), **Whoop** (API v2, read-only; rotating refresh
    token kept in `WHOOP_TOKEN_FILE` on the volume, fingerprinted to the .env token it grew from;
    `scripts/whoop_auth.py`; its workouts into the workouts table once each). All four tested against stub
    servers (`tests/test_{workouts,markets,news,whoop}.py`); none of their real APIs is reachable from the
    sandbox, so the first real fetch is on the box. Yahoo is unofficial and may refuse the Oracle box's IP:
    then stocks say "can't reach the markets" and crypto still comes from CoinGecko.
  - *Inbox*: the overview's `inbox` (the last three days' triaged mail, never "ignore", senders and
    subjects flattened); needs Gmail (DEPLOY §7c). *Answers*: `skills.Answer` has `tainted`, carried into the
    Reply. *Panels*: one slow to answer is left out after `PANEL_SECONDS` (4 s) while its fetch finishes in
    the background, so a hung feed never hangs the page. `/workout` and `/watch` are on her actions list.
  - Verified: the suite (59 suites), and the page in Chromium with stub feeds at 1440, 1024 and 390, full
    and first-night (empty) data; a click-through of the chips, the School links and the packing.
- **Stray tool-call tags in her replies** (same branch). On 2026-09-29 the model behind `claude -p` (CLI
  2.1.284, no tools) began writing `<invoke name="noop"></invoke>` before its JSON on most turns, and
  sometimes `<answer></answer>` or a misspelled `<invoire>` (4 of 6 replies to one question on `main` too).
  Parsed as it was, a tag after prose became her detail and the command she'd written was lost (the eval's
  critical "put AA batteries on my grocery list" failed on it), and the streamed draft (and a call's
  voice) read the raw JSON after the tag. `contract.unstray` drops those tags, by name only, in `parse` and
  `partial_reply`; `partial_reply` also switches to the JSON once it starts after prose. Pinned in
  `tests/test_contract.py`. If new tag names turn up, add them to `_STRAY`. The same days showed prose,
  then the JSON, then more with a brace in it (a second object, a tag's leftover `{}`): shape 3's
  first-to-last-brace match took it all in and read as nothing, so the eval's critical "just finished my
  Boulder essays" lost its `/college` command. `contract._each_object` now reads each object where it
  starts and takes the first that's a reply.
- **"She'll do that for me"** (same branch). What she can't do yet she now offers to build ("I can't do
  that yet. Want me to build it? It'll be in the Workshop for you to OK."), or puts in with `/idea` at
  once when he asks for the ability itself, never for what she mustn't do (`actions.instructions`, only
  when `/idea` is available). Checked against the real model: "can you keep track of how much water I
  drink every day?" and "build yourself a way to track my water intake" each came back as an `/idea`
  with a usable spec (log glasses or ounces, a running daily total against a goal).
- **The coin skill** (`sloane/skills/coin.py`, workshop item `/coin`): `/coin`, "flip a coin", "heads or tails?"
  give Heads or Tails from `secrets`. No SQL, no setting, no migration. `tests/test_coin.py` pins the
  command, both sides, and phrases it must not catch. Awaiting Landen's review.
- **The recent skill** (`sloane/skills/recent.py`, workshop item "Show what I just captured"): `/recent`,
  `/recent 5`, `/recent today`, and "what did I just capture?" show his latest hand-saved items (/idea,
  /remember, /followup, /list, /promise, /remind, /spent), newest first, with when and where each went.
  Reads only: one `Store.recent_captures` query (in its own section at the end of `Store`), no table, no
  migration. `tests/test_recent.py` (integration, listed in run.py). Her build had no shell to run it, and
  its first CI run failed: the test's `like 'learned.%'` with no parameters is a bad placeholder to
  psycopg. Fixed (the pattern is a parameter), `main` merged in; the whole suite passes. Awaiting review.

- The brainstorm skill (`sloane/skills/brainstorm.py`), merged to `main` from the workshop without its
  tests run: its suite and pyflakes pass (run 2026-09-29, with the redesign).

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
| Life skills (PR #20) | workouts (sql/033: by rule, weekly goal, Whoop's too), markets (sql/034 watchlist; Yahoo chart API, CoinGecko fallback; FACTS from cache), news (Google News RSS; tainted answers, never FACTS), Whoop (API v2, rotating refresh token on the volume) | `skills/{workouts,markets,news,whoop}.py`, `scripts/whoop_auth.py`, `tests/test_{workouts,markets,news,whoop}.py` |
| DECA (PR #12) | `/roleplay [area]`: a model-written scenario (event, role, judge, situation, five PIs), then a session where the judge stays in character (typed or voice), two follow-up questions after "I'm done", and a score on the DECA form (PIs 0–14, four 21st Century Skills 0–6, overall 0–6) **totalled in code**; `roleplays` table (sql/027), FACTS line with recent scores + "work on", TV panel, an evening nudge when a DECA countdown is ≤14 days out and no practice in 3 days; `/roleplay` on the actions allowlist. Live-checked against the real CLI: a presentation that missed the brief was pushed back on in character and scored 19/100 with specific notes | `sloane/skills/deca.py`, `sql/027`, `tests/test_deca.py` |
| Slow skills (PR #12) | a command or skill still working after 0.6 s shows "typing…" (a role-play's judge, `/cards make`); instant ones never flash it. The actions instructions now also say recording what he reports isn't initiative, and that only the "do" list does anything (she once said "marking that off" with no command) | `telegram.py` `SLOW_SKILL_SECONDS`, `actions.instructions`; `test_live` |
| JSON repair (PR #12) | `contract.loads_lenient`/`closed`: the CLI sometimes drops an object's final `}` (1 in 3 scenario calls, live). Unrepaired, a reply came out as raw JSON (read aloud, actions lost). Every model-JSON parser now uses it: `contract.parse`, inbox triage, learn, `/cards make`, deca | `contract.py`; regression checks in test_contract, test_cards, test_learn, test_mail, test_deca |
| Colleges (PR #12) | `/college add CU Boulder EA nov 1`: plan, deadline, the usual six-item checklist (+ his own items with their own dates); `done`/`skip`/`add` by words or number; `submitted`/`admitted`/`deferred`/`waitlisted`/`denied`/`committed`; school named by any run of words, a nickname in brackets, or initials; FACTS line per school with exact dates; nudges 14/7/3/1 days out in the evening, the morning of, and once the morning after an unsubmitted deadline; "what's left for Boulder?" answered without a model; "just finished my Boulder essays" / "sent my Boulder app" / "got into Boulder" / "Boulder deferred me" recorded by **rules** (the model path missed the command ~1 run in 14), only when one of his schools is named by whole words (never via a small word like "of") and every other word belongs to the item; `/college` on the actions allowlist (not drop/skip/reopen) for looser wording, with status words checked by `grounded()` | `sloane/skills/colleges.py`, `sql/026`, `tests/test_colleges.py` |
| Capture contract v2 (PR #12) | `client_id` makes retries safe (`capture_refs`, sql/028: a retry after a lost response gets the first answer, `200` + `duplicate`; one still in flight gets `409`; a failed store releases its claim); `act: false` stores memory only (Capture sets its own reminders and logs its own expenses, so it sends `false` and he isn't told twice); `{"check": true}` tests the connection without storing anything | `capture.py`, `CAPTURE_API.md`, `test_capture` |
| Capture + skills (PR #12) | a capture that is exactly a skill phrase is acted on (`action` in the 201 body); `Registry.route(sessions=False)` so a capture is never a quiz answer | `capture.py`, `CAPTURE_API.md` |

| Reminders+ (PR #14) | repeating (`sql/029`: rule + series; delivering one makes the next; `/unremind` stops it), timers ("set a timer for 1 hour 30 minutes"; woken on the second; delivered even in quiet hours), calendar dates via `dates.py`, "starting tomorrow" | `reminders.py`, `jobs/briefs.py`, `tests/test_reminders.py` |
| Forwards (PR #14) | a forward is someone else's words: logged untrusted, answered as INGESTED (no actions), his note is the question, follow-ups see it for 10 minutes | `telegram.py`, `tests/test_forward.py` |
| Memory upgrade (PR #14) | lasting recall seats; STATE priority + cut note; "remember that" at once; learner: captures, updates, retires, diary (`put_diary`); CONVERSATION 24h/40; de-dup on real chat channels; `env_migrate.py` | `memory/store.py`, `tiers.py`, `learn.py`, `skills/memory.py`, `skills/__init__.py`, `agent.py`; tests in store, tiers, learn, agent, actions, env_check |
| Control room (PR #14, rebuilt after #15, and from the mockup) | `/app` (and `/`) + `/api/*`: chat (NDJSON stream through `Bot.respond` via an `Outlet`) typed or by microphone (`/api/voice`), read aloud in the browser; the orb (`/api/activity`); overview (agenda + clashes + focus blocks, what's waiting, health, due and due this week, reminders, grades, structured panels, the week, work, engine, prefs, memory, diary, jobs, trust, usage, system), controls (cancel reminder, approve/deny, revoke, forget, close loose end, run job, save widgets, and every widget's buttons through `POST /api/do`); the chat as a drawer that pops out into its own window; self-hosted Onest; installable (orb icons, manifest) | `web.py`, `views.day_items`, `webui/`, `sql/032`, `telegram.py` `Outlet`/`hears`/`transcribe`, `tests/test_web.py`, `docs/design/` |
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

- **Whoop past Cloudflare** (landenm999-coder/sloane#27, merged; live on the box). Connecting his Whoop on 2026-09-29, the
  token exchange in `scripts/whoop_auth.py` came back 403 "error code: 1010": Cloudflare, in front of Whoop,
  turns Python's own user-agents away (urllib's, and so presumably httpx's for the skill's refreshes and
  reads). Both now send `AGENT` ("Mozilla/5.0 (compatible; Sloane/1.0)"). `tests/test_whoop.py`'s stub
  refuses Python's user-agents the same way and runs `whoop_auth.main` end to end (it failed on the old
  code). DEPLOY 7j now says to open the link in an incognito window: his normal one got nginx's "400 Request
  Header Or Cookie Too Large" from Whoop's cookies. After the installer, whoop_auth.py and a restart, his
  recovery, sleep, strain and Whoop workouts show in the control room (he confirmed it working). The one
  snag: the widget stayed on "Connect it" until `sudo systemctl restart sloane` after the consent (step 4 of
  7j); the skill only switches on when all three WHOOP_ settings are there at startup.

- **The control room, redesigned** (landenm999-coder/sloane#26, merged; live on the box). Landen: "still looks
  like AI... bad inconsistent spacings, the page should not scroll on the home, one full page dashboard...
  make the chat be able to pop in and out... make sure all of the widgets are doing something and
  connected, and the buttons do something... like a real paid product everywhere". Built:
  - *Look*: still dark, the orb and the timeline (what he approved), but one typeface (Onest, OFL, served from
    `webui/fonts/`; IBM Plex and its mono are gone: no monospace numbers, no uppercase labels), one spacing
    scale (4 8 12 16 20 24 32), one card (icon and name, a word on the right, at most two buttons), one button
    system, no dashed empties. The side rail became tabs in a top bar (brand and orb, Home/Workshop/Memory/Engine,
    what she's doing, what's waiting, Chat). The orb only moves when she does (its rings pause when idle).
  - *Home is one screen*: a hello and her line on the day (`renderHeader`: what's next, emails waiting, recovery,
    reminders left; nothing from school), the agenda card (with Add a reminder), and a widget grid that fills the
    room there is (`layoutPanels`: columns and rows from the space, each widget at least 212 by 184; `paginate`
    places them in order, first fit, and widens a row's widgets into any hole; more than fit go on pages with
    dots). Each list shows the rows that fit and "+N more" (`fitLists`). Nothing on Home scrolls at 1024–1920.
    Inbox, news and habits are two columns wide.
  - *The chat pops in and out*: a drawer on the right (`setChat`: open, closed, popped; remembered), `/` or Chat
    opens it, the pop-out gives it its own window (`/app?chat=pop`, the page in `body.solo`), and closing that
    window or Put back brings it home (a `BroadcastChannel`; a reload of either side is handled). A widget's
    question goes to whichever chat is showing. Under 1200px it slides over the widgets.
  - *Every widget does something*: habits tick and untick, list items check off (by number), focus starts
    (25/50 or a custom block) and stops, workouts/spending/tickers/topics/countdowns/birthdays/clients/reminders/
    "remember that" are small forms over the widget, school work is marked handed in (a second press confirms,
    kept apart from the button so a redraw can't lose it), a ticker is dropped the same way, inbox rows open the
    thread in Gmail (`inbox.thread`, hex only), headlines open, the inbox is checked now (the job), the workshop
    takes an idea, and a speech-bubble button asks her about the widget. The buttons go through the new
    `POST /api/do` (`web.BUTTONS`, `Bot.run_command`: his own command, the same handler as typed, no model,
    nothing added to the conversation; session and X-Sloane like every change route). Money, birthdays and
    clients gained structured panel fields; weather gained feels/high/low. A job run from the page now says
    "Inbox triage ran." instead of "inbox: ok".
  - Verified: 61/61 suites (with new checks: `/api/do`'s refusals, that every command a widget sends is in
    `BUTTONS`, that Home never scrolls, the chat's controls), pyflakes clean but for the two known hits; in
    Chromium against the seeded demo at 1920, 1440, 1366, 1280, 1024 and 390, every widget button clicked and
    its result checked (the change in the data and her answer), the pop-out window and put back, the pager,
    no console errors, no page scroll.

- **The workshop reaching the box** (landenm999-coder/sloane#15 is merged): rerunning `install.sh` installs
  the upgrader (`.deploy/`, `host.json`, `sloane-upgrade.path`), applies `sql/031` and asks for
  `GITHUB_TOKEN`. Until the token is there the Workshop tab still takes ideas and plans; it just doesn't
  build. Its first real builds haven't happened yet (backlog item 1).
- **PR landenm999-coder/capture#2**: the Capture → Sloane client (Settings → Sloane, an IndexedDB outbox,
  memory-only by default). Tested in Chromium against a real Sloane server; not yet on a phone. Merging it
  deploys it (Vercel); then DEPLOY §7d on the box (`CAPTURE_TOKEN`, `CORS_ORIGINS`, `tailscale serve`).

## Backlog (ideas, in priority order)

1. Whatever her first real weeks turn up (she's live on the Oracle box; DEPLOY's "try everything" table is
   the checklist). His first night with the new control room found real-data gaps the seeded demo hid
   (fixed in #19); expect more of that kind. The workshop's first real
   builds on the box are part of that: watch the first few (CI time, how often the guard refuses, whether
   Sonnet's builds pass on the first go).
2. The workshop, next: a "revise" button (his notes → a second build on the same branch, instead of Deny and
   re-add), and showing the diff itself in the control room (today it's the file list, the checks and a
   link to the PR).
3. Infinite Campus (grades), deliberately out of v1. Needs district credentials, and repeated automated logins can
   lock the account.
4. P5 phone calls, beyond v1.
5. Streaming for the Groq and Anthropic providers (`claude_code` and `local` stream; the others answer whole).
6. The control room: approvals there when Telegram isn't set up (today the agency needs
   `TELEGRAM_CHAT_ID`). A call that lets him talk over her (barge-in): today the mic is off while she
   speaks, because echo cancellation on a laptop's speakers isn't reliable enough to keep her from hearing
   herself. Drag to reorder widgets (today the order is fixed; Choose widgets switches them on and off).
   If Yahoo refuses the box, a second stock source (Stooq's CSV, or a keyed API) behind the same `Quote`.
7. A weekday lunch slot for `think`, if Landen wants one (school hours are skipped today).
8. `scripts/install.sh` under WSL with Docker Desktop installed: `command -v docker` finds Docker Desktop's
   CLI through the Windows PATH (even with WSL integration off), so Docker Engine is never installed and the
   run stops at "Docker here isn't a system service". Treat a `docker` resolving under `/mnt/` as absent
   (install Engine), keeping the integration message for a `/usr/bin/docker` that links into Docker Desktop.

## Only Landen can do (the whole list; see DEPLOY.md)

Done: the Oracle box, the installer, the workshop's GitHub token, the control room, the British voice.

0. After each merge to `main`, upgrade the box: in PowerShell,
   `ssh -i "C:\Users\lande\Downloads\ssh-key-2026-09-27.key" ubuntu@<the box's IP>`, then
   `curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash`.
   (Accepted workshop builds upgrade the box by themselves.) Merge landenm999-coder/capture#2 too.
1. Optional tidy-up on his PC: the second install in WSL is switched off but still holds a copy of `.env`
   (his credentials). `wsl --unregister Ubuntu-24.04` deletes it, and `wsl --unregister Ubuntu` the empty
   Ubuntu made by accident. Neither holds anything she needs; her memory is in Supabase.
2. Optional Gmail (DEPLOY §7c): a Google Cloud Desktop OAuth client, **published In production** (Testing tokens
   die after 7 days), then `python3 scripts/gmail_auth.py` on the box.
3. Capture on the phone: Tailscale on the phone, `CAPTURE_TOKEN` + `CORS_ORIGINS` on the box, then
   Capture → Settings → Sloane (DEPLOY §7d).
4. Optional Whoop (DEPLOY §7j): an app at developer.whoop.com (redirect `http://localhost:8765`), its id and
   secret in `.env`, `python3 scripts/whoop_auth.py` on the box, restart. Gmail (2) is what fills the
   control room's Inbox panel.

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
