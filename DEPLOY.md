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
docker compose run --rm sloane sh -c 'psql "$DATABASE_URL" -f sql/001_init.sql'
docker compose run --rm sloane sh -c 'psql "$DATABASE_URL" -f sql/002_hybrid_search.sql'
docker compose run --rm sloane sh -c 'psql "$DATABASE_URL" -f sql/003_school.sql'
```

> The **single quotes matter**. Double quotes would expand `$DATABASE_URL` in
> your shell on the box, which does not have it — `.env` is read by the
> container, not exported to your session — and psql would get an empty
> connection string.

All three are idempotent — re-running them is safe and is how you upgrade later.

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

# a new migration shipped
docker compose run --rm sloane sh -c 'psql "$DATABASE_URL" -f sql/00N_whatever.sql'
```

On Telegram: `/brief` gives the morning brief on demand. `/jobs` shows what
ran and whether it worked. `/sync` pulls Canvas, the calendar and shifts now.
`/usage` shows model calls in the last day. `/state` shows her durable facts.

---

## When something is wrong

| Symptom | Cause |
|---|---|
| `health` says `database: down` | Wrong `DATABASE_URL`, or you took the **transaction** pooler (6543). Use **session** (5432). |
| `prepared statement already exists` | Same thing — transaction-mode pooler. The code disables prepared statements, so if you see this, an old image is running: rebuild. |
| Bot silent, no errors | `TELEGRAM_CHAT_ID` does not match the account messaging her. She drops unknown chats on purpose. |
| `claude_code` failing, everything else fine | The CLI login expired. Redo step 6. |
| Canvas 401 | Token revoked or expired. Regenerate; district tokens sometimes have a lifetime. |
| Recall empty, FACTS fine | The embedder never downloaded. `doctor.py --warm`. She still answers from FACTS, and full-text recall still works. |
| No morning brief | `/jobs` — `deferred` means quiet hours or the budget held it (reason shown); `failed` shows the error; `never` means the scheduler didn't start — check the logs. |
| Container restarting | `journalctl -u sloane -n 100`. Usually a malformed `.env` line. |

**Nothing here needs an inbound port.** If you ever find yourself opening one to
fix something, stop — the answer is somewhere else.

---

## What this does not do yet

Email triage (P4) is not built yet. The five daily briefs
start on their own the moment she's running — the first you'll hear is the
6:35 AM brief. `TELEGRAM_CHAT_ID` must be set or the briefs run and record but
have nobody to send to; `/jobs` will show that plainly.
