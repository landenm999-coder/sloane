# Deploying Sloane

> The short version is [START_HERE.md](START_HERE.md). This file is the detail behind it.

Target: an **Oracle Cloud Always Free** ARM instance. Free forever, 2 Ampere
cores and 12 GB, which is more than she needs. Total running cost stays $0/mo.

Roughly 40 minutes, most of it waiting on Oracle.

---

## 1. Get the instance

Oracle Cloud → **Compute → Instances → Create instance**

| Setting | Value |
|---|---|
| Shape | **Ampere A1 Compute** (ARM) — *not* the AMD micro shape |
| OCPUs / memory | 2 / 12 GB (the Always Free ceiling since June 2026) |
| Image | Ubuntu 24.04 |
| Boot volume | 50 GB default is plenty |
| SSH keys | upload your public key, or let it generate one — **save the private key** |

> **"Out of host capacity" is normal, not a mistake you made.** Free ARM capacity
> is genuinely scarce in most regions. Retry every few hours, or try a different
> availability domain in the same region. If it will not budge after a day or
> two, a GCP `e2-micro` on the free tier works — it is smaller, so expect the
> embedder's first load to be slow.

**Do not open any inbound ports.** Leave the security list as it is. Sloane
needs nothing inbound: Telegram is long polling, which is an outbound
connection she opens herself. That is the whole reason there is no public URL,
no TLS certificate and no tunnel in this design.

---

## The fast way: one command (steps 2–8)

Once you can SSH into the instance, this does everything below for you.
It installs Docker, clones the repo and asks for the seven `.env` values
(secrets are hidden as you type). Then it builds, applies migrations, seeds,
walks you through the one Claude login, runs doctor and starts the service:

```bash
curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash
```

Run the same command again any time to upgrade. It never overwrites your `.env`.
The numbered steps below are the same thing by hand, if you'd rather see each
part (or the script stops somewhere).

---

## 2. Install Docker

SSH in, then:

```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker          # or log out and back in
docker --version       # confirm
```

Oracle's Ubuntu images ship iptables rules that block almost everything
inbound. Leave them alone — see above.

---

## 3. Clone and configure

```bash
sudo mkdir -p /opt/sloane && sudo chown $USER:$USER /opt/sloane
git clone https://github.com/landenm999-coder/sloane.git /opt/sloane
cd /opt/sloane

cp .env.example .env
nano .env              # paste the seven values
```

The seven that need real values:

```
DATABASE_URL  GROQ_API_KEY  TELEGRAM_BOT_TOKEN  TELEGRAM_CHAT_ID
CANVAS_BASE_URL  CANVAS_TOKEN  CALENDAR_ICS_URL
```

Everything else already has a working default. `.env` is gitignored.
The installer checks each value as you paste it (`scripts/env_check.py`) and
`doctor.py` runs the same checks on a hand-edited `.env`: the `[YOUR-PASSWORD]`
placeholder left in, the transaction pooler, the IPv6-only direct connection,
a password with `@ # / ? $` in it (use letters and numbers), a username for
the chat id, a Canvas page instead of its address, the calendar's public or
web-page link instead of the secret iCal one.

```bash
chmod 600 .env         # it holds four credentials
```

---

## 4. Build

```bash
docker compose build
```

Five to ten minutes on ARM, most of it wheels. Every dependency publishes an
aarch64 wheel, so nothing compiles — if you see a compiler running, something
is wrong, stop and check the error.

---

## 5. Apply the schema

The image carries `psql`, so nothing extra to install:

```bash
docker compose run --rm sloane sh -c 'for f in sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || exit 1; done'
```

> The **single quotes matter**. Double quotes would expand `$DATABASE_URL` in
> your shell on the box, which does not have it — `.env` is read by the
> container, not exported to your session — and psql would get an empty
> connection string.

Every migration is idempotent, so this one line is also how you upgrade later: re-run it after any `git pull`.

> If `001` fails on `create extension vector`, enable it first: Supabase →
> **Database → Extensions** → search `vector` → toggle on.

---

## 6. Log the Claude CLI in — once

`MAIN_PROVIDER=claude_code` shells out to the Claude Code CLI, which bills
against the Pro subscription rather than API credits. It needs a one-time
interactive login:

```bash
docker compose run --rm sloane claude
```

Follow the login prompt, then exit. The credentials land in the `claude-auth`
volume and survive every future `build` and `up` — you will not do this again
unless you delete that volume.

Confirm it took:

```bash
docker compose run --rm sloane claude -p "reply with ok"
```

---

## 7. Seed and check

```bash
docker compose run --rm sloane python scripts/seed_state.py state.example.md
docker compose run --rm sloane python scripts/seed_courses.py
docker compose run --rm sloane python scripts/doctor.py --warm
```

`--warm` downloads the 130 MB embedder and asserts it really returns 384
dimensions. It lands in the `models` volume, so it happens once.

Work down whatever `doctor.py` names until it is clean. It checks the database,
schema, pgvector, hard lines, tier 1, the embedder, both providers, the
fallback lane, Canvas, the calendar and Telegram — and gives the fix for each.

---

## 7b. Optional: a British voice (or a local fallback)

Voice replies use Groq by default: free, about 100 a day, American voices. Piper
runs on the box instead, with no daily cap, and it has British voices. It's in the
image already, so one line in `.env` is enough:

```bash
# her main voice, British (the full JARVIS)
SPEAK_PROVIDER=piper
PIPER_VOICE=en_GB-cori-medium
```

She fetches the voice once (about 60 MB, into the `models` volume, so it survives
rebuilds) when she starts. It's loaded into memory once, and after that a voice
reply takes a fraction of a second. Until the download finishes, and whenever Piper
fails, Groq speaks instead. Other voices: `en_GB-jenny_dioco-medium`,
`en_GB-alba-medium` (Scottish), `en_GB-alan-medium` (male), `en_US-amy-medium`.

To keep Groq as her voice and use Piper only as the fallback when Groq's allowance
runs out, set `PIPER_VOICE` and leave `SPEAK_PROVIDER=groq`. Restart after changing
either (`sudo systemctl restart sloane`).
`docker compose run --rm sloane python scripts/doctor.py --warm` shows whether the voice is
on disk yet, and has her say a test line.

## 7c. Optional: Gmail (about 10 minutes, once)

She reads unread mail every three hours from 7 AM to 7 PM, sorts it into
urgent / reply / fyi / ignore, and messages you only when something needs you.
For up to three of those she writes a reply in your voice and sends it to
Telegram with **Approve / Edit / Deny** — nothing is sent until you tap
Approve. Anything from a school address (`SCHOOL_EMAIL_DOMAINS`, default
`dcsdk12.org`) is never sent by her at all: Approve saves it to your Gmail
**Drafts** and you press send yourself.

1. **console.cloud.google.com** → create a project (any name, e.g. `sloane`).
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **APIs & Services → OAuth consent screen** (may be called *Google Auth
   Platform → Branding/Audience*): User type **External**, app name `Sloane`,
   your email as support and developer contact. Save.
4. On **Audience**, add yourself as a test user, then click **Publish app** →
   **In production**. *This matters:* in Testing mode Google kills the token
   every 7 days. You do not need to submit for verification — it's only you.
5. **Credentials → Create credentials → OAuth client ID** → Application type
   **Desktop app** → Create. Copy the client ID and secret into `.env`:

   ```
   GMAIL_CLIENT_ID=...
   GMAIL_CLIENT_SECRET=...
   ```

6. On the box, **outside Docker** (it writes `.env` for you):

   ```bash
   cd /opt/sloane && python3 scripts/gmail_auth.py
   ```

   Open the link it prints, pick your account, click *Advanced → Go to Sloane*
   past the unverified-app warning, allow both permissions. The browser then
   lands on a `localhost` page that **won't load — that's expected**. Copy that
   whole address, paste it into the script, done. The token goes into `.env`,
   never onto the screen.

7. `sudo systemctl restart sloane`, then `doctor.py` — the `gmail` line should
   say PASS. `/inbox` on Telegram runs a triage right away.

Permissions are read + compose only: she can read, draft and send, and has no
permission that could delete mail.

## 7d. Later: connect Capture (about 10 minutes, once)

Capture posts what you say to `POST /capture`. It is the one endpoint that
checks a password, and it stays off until you set one.

1. Make a token and put it in `.env`:

   ```bash
   python3 -c "import secrets; print('CAPTURE_TOKEN=' + secrets.token_urlsafe(32))" >> .env
   sudo systemctl restart sloane
   ```

2. Reach the box from your phone **without opening a port**. Install
   [Tailscale](https://tailscale.com/download) on the box
   (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and
   on your phone, signed in to the same account. Then on the box:

   ```bash
   sudo tailscale serve --bg 8000
   ```

   Sloane is now at `https://<box-name>.<your-tailnet>.ts.net`, reachable only by
   your own devices. **Never use `tailscale funnel`**: that would put her on the
   public internet.

3. Capture is a web page, so its call to Sloane is cross-origin and the browser checks
   with Sloane first. Allow Capture's address (the one in your phone's address bar), then
   restart:

   ```bash
   echo 'CORS_ORIGINS=https://<your-capture-app>.vercel.app' >> .env
   sudo systemctl restart sloane
   ```

   Only `/capture` answers cross-origin; `/facts`, `/state` and the rest never do.
   `doctor.py` warns if this is missing.

4. In Capture, go to **Settings → Sloane**. Enter `https://<box>.<tailnet>.ts.net` and the
   token, then press **Connect**. The test stores nothing. If Chrome asks whether the site may
   reach devices on your local network, allow it. From then on, every capture is also
   remembered by Sloane ("what was that idea about the bakery site?"). If the phone is off the
   tailnet, captures wait on the phone and go when it's back.

   By default she only *remembers* captures, because Capture already sets your reminders and
   logs expenses. Tick **Let Sloane act on captures too** if you'd rather she also set
   "remind me …" reminders (on Telegram) and ran her list and habit rules. You'd then hear
   some things twice. The full contract is `CAPTURE_API.md`. To test from any device on
   your tailnet:

   ```bash
   curl -s -X POST https://<box>.<tailnet>.ts.net/capture \
     -H "Authorization: Bearer $CAPTURE_TOKEN" -H "Content-Type: application/json" \
     -d '{"check": true}'          # {"ok": true}: connected, nothing stored
   ```

## 7e. The control room: talk to her and run everything from a browser (2 minutes)

`/app` is a private page where you talk to her (the same conversation as
Telegram: commands, reminders and actions all work) and see and steer
everything she runs:

- **Today:** your day on a rail, what needs you, what's due, reminders, grades.
- **Memory:** what she knows about you, loose ends, her diary. Forget anything.
- **Engine:** jobs you can run now, what she may do without asking, model use.

The installer makes its password, `DASHBOARD_TOKEN`, for you. Read it on the
box with:

```bash
grep DASHBOARD_TOKEN /opt/sloane/.env
```

With `tailscale serve` from 7d running, open `https://<box>.<tailnet>.ts.net/app`
on your phone or laptop and paste it. You stay signed in for 30 days. On a
phone, use **Add to Home Screen** and it opens like an app. It is reachable only
from your own tailnet devices, and even there it needs the password. Changing
`DASHBOARD_TOKEN` (and restarting) signs every device out.

## 7f. Optional: the TV dashboard (2 minutes)

With `tailscale serve` from 7d running, open `https://<box>.<tailnet>.ts.net/tv`
on any device signed in to your tailnet: an old tablet on the wall, a laptop, a
TV with the Tailscale app. It shows the clock, the weather, today and tomorrow,
overdue and due-soon work, reminders, promises, grades and a card for each
skill, and refreshes itself every minute. It never loads anything from the
internet and needs no token (it is as private as `/facts`: your tailnet only).
Put it in full screen and leave it.

## 7g. Skill settings (optional)

The skills need nothing to start. Three settings in `.env` make them better:

```bash
WEATHER_LOCATION=39.52,-104.76   # already set for Parker; blank turns weather off
PAY_RATE=15                      # your hourly pay, for "about $240 earned this week"
PLAN_BEDTIME=22:30               # when /plan stops filling your evening
```

`SKILLS_DISABLED=money,habits` switches skills off by name. `doctor.py` lists
which skills loaded and checks the weather reaches Open-Meteo.

## 8. Run her

```bash
sudo cp sloane.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloane
systemctl status sloane
```

Within a minute she messages you on Telegram ("Sloane here, up and running"). She
says that once per version, so an upgrade gets "Updated and back up" and a plain
restart gets nothing. Then say hi.

```bash
docker compose run --rm sloane python -c "print('ok')"   # sanity
curl -s localhost:8000/health                            # loopback only
```

### The first day: try everything

Each of these should work on day one. If one doesn't, `/status` and
`docker compose run --rm sloane python scripts/doctor.py` say why.

| Say or type on Telegram | What should happen |
|---|---|
| `hey, how's it going?` | a line back in her voice, not a briefing; "typing…" at once and the reply written in place |
| `what's due tomorrow?`, then `and friday?` | the exact rows from Canvas; the follow-up understood without repeating yourself |
| a voice note: "what's on today?" | a voice note back (British, if you set `PIPER_VOICE=en_GB-cori-medium`) |
| `remind me in 2 minutes to test this` | a reminder in 2 minutes, with Snooze buttons |
| `remind me every weekday at 7am to take my meds`, then `/reminders` | a repeating one (🔁); `/unremind <n>` stops the series |
| `set a timer for 1 minute` | "Time's up" a minute later, on the second |
| forward her a friend's text | who it's from, what they want, and a reply you could send; `make it shorter` still sees it |
| `put batteries on the grocery list and remind me at 7 to charge the car` | both done in one go, each result shown under her reply |
| `who won the Broncos game?` | "Checking.", then the answer with a source |
| `/college add CU Boulder EA nov 1`, then `just finished my Boulder essays`, then `what's left for Boulder?` | the school, the checklist ticked, and what's left with the deadline |
| `/roleplay` | a DECA scenario; present by text or voice, say "I'm done", answer 2 questions, get a score |
| `/countdown DECA districts dec 3`, `/habit add reading`, `did reading` | a countdown and a streak (the heartbeat nudges you later) |
| `spent 12 on lunch`, `/budget 60` | the week's spending against the budget |
| `/plan` | tonight's free time filled with what's due soonest |
| `my manager at work is Dana`, then the next morning `/memory` | the nightly learn job kept it |
| `/today`, `/week`, `/grades`, `/status` | straight from the database, even if every model is down |
| `https://<box>.<tailnet>.ts.net/tv` on a tablet | the wall dashboard |
| a capture in the Capture app, then ask Sloane about it | she remembers it |

---

## Day to day

```bash
journalctl -u sloane -f              # logs
docker compose run --rm sloane python scripts/doctor.py

# update
cd /opt/sloane && git pull
docker compose build && sudo systemctl restart sloane

# after every pull: apply migrations (idempotent, safe to repeat)
docker compose run --rm sloane sh -c 'for f in sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || exit 1; done'
```

On Telegram: "remind me at 5 to call Keegan" (typed or as a voice note) or `/remind tomorrow 7am bring the lab` sets a reminder; "remind me every weekday at 7 to …" sets one that repeats; "set a timer for 10 minutes" is a timer; `/reminders` lists them and `/unremind <n>` cancels one (and stops a repeating one). `/promise send Keegan the outline by friday` tracks a promise until `/kept`.
`/status` says whether anything is broken. `/grades` shows current course grades. `/today` and `/week` show the schedule straight from the database (they work even if every AI provider is down). `/trust` shows what she may do without asking. `/brief` gives the morning brief on demand. `/jobs` shows what
ran and whether it worked. `/sync` pulls Canvas, the calendar and shifts now.
`/usage` shows model calls in the last day. `/state` shows her durable facts.
`/inbox` triages new email now.

Mostly, just talk to her. She follows the conversation ("and in stat?"), does what
you ask in plain words ("put batteries on the list and remind me at 7"), looks things
up when a question needs the outside world ("who won the Broncos game?"), and
remembers what you tell her (`/memory` shows what she's kept; `/forget <n>` fixes it).
`ADDRESS_AS=sir` in `.env` if you'd rather she called you that; `WEB_LOOKUP=false`
turns lookups off.

The skills (`/help` lists them all): "add milk to my grocery list", `/countdown
graduation may 22`, "is it going to rain?", `/card bio: q :: a` then `/quiz`,
`/habit add reading` then "did reading", `/client add Bella's Bakery $1200`,
`/plan` for tonight, `/focus 25 essay`, `/birthday Keegan mar 3`, "spent 12 on
lunch", `/college add CU Boulder EA nov 1` then "what's left for Boulder?", `/roleplay`
for DECA practice (she plays the judge; answer by voice note for the real thing). `/end`
stops a quiz or a role-play.

**College applications, first.** It's application season, so add every school you're
applying to now: `/college add <school> <EA|ED|RD> <deadline>` (several at once, one per
line or split by `;`), a nickname in brackets
if you use one (`/college add Colorado State University (CSU) RD feb 1`). Each gets
the usual checklist; `/college csu skip scores` for a test-optional school,
`/college csu add portfolio by oct 20` for anything extra. She reminds you two
weeks, a week, three days and a day out, and the morning of.

---

**Backups.** Every night at 12:30 she writes what only you could recreate
(state, promises, people, courses, trust, reminders, and every skill's data:
lists, countdowns, flashcards, habits, clients, focus, spending, college
applications, role-play scores) to
`/var/lib/sloane/models/backups/sloane-YYYY-MM-DD.json` inside the `models`
volume, keeping 14. Copy one off the box with
`docker cp sloane:/var/lib/sloane/models/backups ./sloane-backups`.
To bring lost rows back (it merges; nothing current is overwritten; dry run
unless `--apply`):
`docker compose run --rm sloane python scripts/restore_backup.py /var/lib/sloane/models/backups/sloane-YYYY-MM-DD.json --apply`

## When something is wrong

Most of the time you won't need this table: the watchdog messages you when a
job keeps failing, a model stops answering (for example, an expired Claude
login), or Gmail access dies. It tells you the fix, and tells you again when
it's working.

| Symptom | Cause |
|---|---|
| Supabase emails "table publicly accessible" / RLS disabled | The migrations weren't all applied (the last one, `999_lock_public.sql`, turns row-level security on everywhere). Run the installer again. |
| `health` says `database: down` | Wrong `DATABASE_URL`, or you took the **transaction** pooler (6543). Use **session** (5432). `doctor.py`'s `settings` line names which. |
| `prepared statement already exists` | Same thing — transaction-mode pooler. The code disables prepared statements, so if you see this, an old image is running: rebuild. |
| Bot silent, no errors | `TELEGRAM_CHAT_ID` does not match the account messaging her. She drops unknown chats on purpose. |
| `claude_code` failing, everything else fine | The CLI login expired. Redo step 6. |
| Canvas 401 | Token revoked or expired. Regenerate; district tokens sometimes have a lifetime. |
| No **New Access Token** button in Canvas | The district turned student tokens off. Leave `CANVAS_TOKEN` blank and set `CANVAS_FEED_URL` to Canvas → Calendar → **Calendar Feed**. She gets every due date; not grades or whether it's turned in, so past-due work shows as unknown, never overdue. Say "finished X" to clear one. |
| Recall empty, FACTS fine | The embedder never downloaded. `doctor.py --warm`. She still answers from FACTS, and full-text recall still works. |
| No morning brief | `/jobs` — `deferred` means quiet hours or the budget held it (reason shown); `failed` shows the error; `never` means the scheduler didn't start — check the logs. |
| `gmail` FAIL: access revoked or expired | The OAuth app is still in **Testing** (7-day tokens), or you removed its access. Publish it (7c step 4) and rerun `scripts/gmail_auth.py`. |
| No email drafts, triage works | Drafts only go to people who can answer (not `noreply@`), at most three a run, and not once today's scheduled budget is spent. `/jobs` shows the inbox line. |
| Container restarting | `journalctl -u sloane -n 100`. Usually a malformed `.env` line. |
| Replies take 5+ seconds before anything shows | The warm `claude` process isn't there: check `journalctl -u sloane` for "could not keep a claude process warm", usually an expired Claude login (redo step 6). She still answers, just from a cold start |
| She says she "tried to look that up and couldn't" | The lookup goes through `claude -p` with web search. Same login as above; or `WEB_LOOKUP=false` to stop her trying |
| No weather anywhere | `WEATHER_LOCATION` blank or not `lat,lon`, or Open-Meteo unreachable. `doctor.py` says which. |
| A skill command answers "hit an error" | That skill failed on its own; the rest of her is fine. The log has the traceback; `SKILLS_DISABLED=<name>` turns it off until it's fixed. |

**Nothing here needs an inbound port.** If you ever find yourself opening one to
fix something, stop — the answer is somewhere else.

---

## What starts on its own

The five daily briefs start the moment she's running — the first you'll hear
is the 6:35 AM brief. The heartbeat starts too, but it only speaks when a
skill has something new (a countdown a week out, rain before your shift). One
`claude` process sits idle, ready for your next message (about 150 MB); it is
replaced after every reply. At 12:20 AM she quietly reads back the day's
messages for loose ends to follow up on. `TELEGRAM_CHAT_ID` must be set or the briefs run and
record but have nobody to send to; `/jobs` will show that plainly. The inbox
job runs only once Gmail is set up (7c); until then `/jobs` lists it as
deferred with the reason `gmail is not configured`.
