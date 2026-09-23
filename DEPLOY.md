# Deploying Sloane

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

## 7b. Optional: a local voice fallback

Voice replies use Groq by default (free, ~100 a day). For a fallback with no
cap, install Piper and one voice on the box:

```bash
# on the host, into the models volume so it survives rebuilds
docker compose run --rm sloane sh -c '
  cd /var/lib/sloane/models &&
  curl -fsSLO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx &&
  curl -fsSLO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json'
```

then set `PIPER_VOICE=/var/lib/sloane/models/en_US-amy-medium.onnx` in `.env`
and install the `piper` binary. Without this, when Groq's daily speech allowance
runs out she simply answers in text.

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

3. In Capture's settings, enter that URL and the token. Test it from any
   device on your tailnet:

   ```bash
   curl -s -X POST https://<box>.<tailnet>.ts.net/capture \
     -H "Authorization: Bearer $CAPTURE_TOKEN" -H "Content-Type: application/json" \
     -d '{"text": "remind me in 10 minutes to test capture"}'
   ```

   Whatever you capture is stored in your own voice, so she can recall it later. A captured
   "remind me …" becomes a real reminder.

## 8. Run her

```bash
sudo cp sloane.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sloane
systemctl status sloane
```

Then message the bot on Telegram. She should answer.

```bash
docker compose run --rm sloane python -c "print('ok')"   # sanity
curl -s localhost:8000/health                            # loopback only
```

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

On Telegram: "remind me at 5 to call Keegan" (typed or as a voice note) or `/remind tomorrow 7am bring the lab` sets a reminder; `/reminders` lists them and `/unremind <n>` cancels one. `/promise send Keegan the outline by friday` tracks a promise until `/kept`.
`/grades` shows current course grades. `/today` and `/week` show the schedule straight from the database (they work even if every AI provider is down). `/trust` shows what she may do without asking. `/brief` gives the morning brief on demand. `/jobs` shows what
ran and whether it worked. `/sync` pulls Canvas, the calendar and shifts now.
`/usage` shows model calls in the last day. `/state` shows her durable facts.
`/inbox` triages new email now.

---

**Backups.** Every night at 12:30 she writes what only you could recreate
(state, promises, people, courses, trust, reminders) to
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
| `health` says `database: down` | Wrong `DATABASE_URL`, or you took the **transaction** pooler (6543). Use **session** (5432). |
| `prepared statement already exists` | Same thing — transaction-mode pooler. The code disables prepared statements, so if you see this, an old image is running: rebuild. |
| Bot silent, no errors | `TELEGRAM_CHAT_ID` does not match the account messaging her. She drops unknown chats on purpose. |
| `claude_code` failing, everything else fine | The CLI login expired. Redo step 6. |
| Canvas 401 | Token revoked or expired. Regenerate; district tokens sometimes have a lifetime. |
| Recall empty, FACTS fine | The embedder never downloaded. `doctor.py --warm`. She still answers from FACTS, and full-text recall still works. |
| No morning brief | `/jobs` — `deferred` means quiet hours or the budget held it (reason shown); `failed` shows the error; `never` means the scheduler didn't start — check the logs. |
| `gmail` FAIL: access revoked or expired | The OAuth app is still in **Testing** (7-day tokens), or you removed its access. Publish it (7c step 4) and rerun `scripts/gmail_auth.py`. |
| No email drafts, triage works | Drafts only go to people who can answer (not `noreply@`), at most three a run, and not once today's scheduled budget is spent. `/jobs` shows the inbox line. |
| Container restarting | `journalctl -u sloane -n 100`. Usually a malformed `.env` line. |

**Nothing here needs an inbound port.** If you ever find yourself opening one to
fix something, stop — the answer is somewhere else.

---

## What starts on its own

The five daily briefs start the moment she's running — the first you'll hear
is the 6:35 AM brief. `TELEGRAM_CHAT_ID` must be set or the briefs run and
record but have nobody to send to; `/jobs` will show that plainly. The inbox
job runs only once Gmail is set up (7c); until then `/jobs` lists it as
deferred with the reason `gmail is not configured`.
