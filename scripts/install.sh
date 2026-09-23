#!/usr/bin/env bash
# One command from a fresh Ubuntu box to Sloane running under systemd.
#
#   curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash
#
# It does DEPLOY.md steps 2-8 for you: Docker, the clone, .env (asking for the
# values, secrets hidden as you type), the image, every migration, the seeds,
# the one-time Claude login, doctor, and the systemd service.
#
# Safe to run again at any time -- that is also how you upgrade: it pulls,
# rebuilds, re-applies migrations (all idempotent) and restarts. It never
# overwrites an existing .env, and never prints a credential.
set -euo pipefail

REPO=https://github.com/landenm999-coder/sloane.git
DIR=/opt/sloane
TTY=/dev/tty

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ask()  { local prompt=$1 var; read -r -p "$prompt" var <"$TTY"; printf '%s' "$var"; }
secret() { local prompt=$1 var; read -r -s -p "$prompt" var <"$TTY"; echo >"$TTY"; printf '%s' "$var"; }
compose() { sudo docker compose --project-directory "$DIR" "$@"; }

# -- 1. Docker ------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl ca-certificates >/dev/null
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
command -v git >/dev/null 2>&1 || sudo apt-get install -y -qq git >/dev/null

# -- 2. The code ----------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only
else
  say "Cloning into $DIR"
  sudo mkdir -p "$DIR" && sudo chown "$USER:$USER" "$DIR"
  git clone -q "$REPO" "$DIR"
fi
cd "$DIR"

# -- 3. .env ----------------------------------------------------------------------
if [ ! -f .env ]; then
  say "Setting up .env -- paste each value (secrets are hidden as you type)"
  cp .env.example .env
  chmod 600 .env
  set_env() {  # set_env KEY VALUE: replace the KEY= line, never echoing VALUE
    python3 - "$1" "$2" <<'PY'
import sys, pathlib
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text().splitlines()
out, done = [], False
for line in lines:
    if line.split("=", 1)[0].strip() == key:
        out.append(f"{key}={value}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={value}")
p.write_text("\n".join(out) + "\n")
PY
  }
  set_env DATABASE_URL        "$(secret 'DATABASE_URL (Supabase session pooler, port 5432): ')"
  set_env GROQ_API_KEY        "$(secret 'GROQ_API_KEY: ')"
  set_env TELEGRAM_BOT_TOKEN  "$(secret 'TELEGRAM_BOT_TOKEN: ')"
  set_env TELEGRAM_CHAT_ID    "$(ask 'TELEGRAM_CHAT_ID (a number): ')"
  set_env CANVAS_BASE_URL     "$(ask 'CANVAS_BASE_URL (e.g. https://dcsd.instructure.com): ')"
  set_env CANVAS_TOKEN        "$(secret 'CANVAS_TOKEN: ')"
  set_env CALENDAR_ICS_URL    "$(secret 'CALENDAR_ICS_URL (the secret iCal address): ')"
  chmod 600 .env
  echo "Saved .env (chmod 600). Edit it later with: nano $DIR/.env"
else
  say ".env already exists -- leaving it alone"
fi

# -- 4. Image and migrations --------------------------------------------------------
say "Building the image (first time: a few minutes)"
compose build -q

say "Applying migrations"
compose run --rm -T sloane sh -c \
  'for f in sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || exit 1; done'

if [ ! -f .installed ]; then
  say "Seeding tier 1 and the semester's courses"
  compose run --rm -T sloane python scripts/seed_state.py state.example.md
  compose run --rm -T sloane python scripts/seed_courses.py
fi

# -- 5. Claude CLI login (once; lives in the claude-auth volume) --------------------
if compose run --rm -T sloane claude -p "reply with ok" >/dev/null 2>&1; then
  say "Claude CLI is already logged in"
else
  say "Log the Claude CLI in (once). Follow the prompts, then type /exit"
  compose run --rm sloane claude <"$TTY" >"$TTY" 2>&1 || true
fi

# -- 6. Check everything, then run under systemd --------------------------------------
say "Running doctor (downloads the 130 MB embedder the first time)"
compose run --rm -T sloane python scripts/doctor.py --warm || \
  echo "doctor found things to fix -- see above. Sloane will still start."

say "Installing the systemd service"
sudo cp sloane.service /etc/systemd/system/sloane.service
sudo systemctl daemon-reload
sudo systemctl enable sloane >/dev/null 2>&1
sudo systemctl restart sloane
touch .installed

say "Done. Message your bot on Telegram -- she should answer."
echo "Logs:     journalctl -u sloane -f"
echo "Health:   curl -s localhost:8000/health"
echo "Upgrade:  run this same command again"
