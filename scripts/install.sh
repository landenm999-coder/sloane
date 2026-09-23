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

# set_env FILE KEY VALUE: replace the KEY= line in FILE, never echoing VALUE.
# The value travels in the environment, not argv: argv is world-readable in
# `ps`, /proc/<pid>/environ is readable only by this user.
set_env() {
  SLOANE_VALUE="$3" SLOANE_FILE="$1" python3 - "$2" <<'PY'
import os, sys, pathlib
key, value = sys.argv[1], os.environ["SLOANE_VALUE"]
p = pathlib.Path(os.environ["SLOANE_FILE"])
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

# Everything runs inside main(), called on the last line. Under `curl | bash`
# the script *is* bash's stdin; wrapping it means bash has read all of it
# before the first command runs, so nothing below can swallow the rest.
main() {
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
    if ! { : <"$TTY"; } 2>/dev/null; then
      echo "No terminal to ask for the .env values on. Run this from an SSH session, or create $DIR/.env yourself (cp .env.example .env)." >&2
      exit 1
    fi
    say "Setting up .env -- paste each value (secrets are hidden as you type)"
    # Built in a temp file and moved into place only when every answer is in,
    # so a Ctrl-C halfway leaves no half-filled .env for the next run to trust.
    local draft
    draft=$(mktemp "$DIR/.env.draft.XXXXXX")
    chmod 600 "$draft"
    # shellcheck disable=SC2064  # expand $draft now: the trap must remove this file
    trap "rm -f '$draft'" EXIT
    cp .env.example "$draft"
    local v
    v=$(secret 'DATABASE_URL (Supabase session pooler, port 5432): ');        set_env "$draft" DATABASE_URL "$v"
    v=$(secret 'GROQ_API_KEY: ');                                              set_env "$draft" GROQ_API_KEY "$v"
    v=$(secret 'TELEGRAM_BOT_TOKEN: ');                                        set_env "$draft" TELEGRAM_BOT_TOKEN "$v"
    v=$(ask 'TELEGRAM_CHAT_ID (a number): ');                                  set_env "$draft" TELEGRAM_CHAT_ID "$v"
    v=$(ask 'CANVAS_BASE_URL (e.g. https://dcsd.instructure.com): ');          set_env "$draft" CANVAS_BASE_URL "$v"
    v=$(secret 'CANVAS_TOKEN: ');                                              set_env "$draft" CANVAS_TOKEN "$v"
    v=$(secret 'CALENDAR_ICS_URL (the secret iCal address): ');                set_env "$draft" CALENDAR_ICS_URL "$v"
    mv "$draft" .env
    echo "Saved .env (chmod 600). Edit it later with: nano $DIR/.env"
  else
    say ".env already exists -- leaving it alone"
  fi

  # -- 4. Image and migrations --------------------------------------------------------
  say "Building the image (first time: a few minutes)"
  compose build -q </dev/null

  say "Applying migrations"
  # shellcheck disable=SC2016  # $DATABASE_URL must expand inside the container, not here
  compose run --rm -T sloane sh -c \
    'for f in sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || exit 1; done' </dev/null

  if [ ! -f .installed ]; then
    say "Seeding tier 1 and the semester's courses"
    compose run --rm -T sloane python scripts/seed_state.py state.example.md </dev/null
    compose run --rm -T sloane python scripts/seed_courses.py </dev/null
  fi

  # -- 5. Claude CLI login (once; lives in the claude-auth volume) --------------------
  if compose run --rm -T sloane claude -p "reply with ok" </dev/null >/dev/null 2>&1; then
    say "Claude CLI is already logged in"
  else
    say "Log the Claude CLI in (once). Follow the prompts, then type /exit"
    # shellcheck disable=SC2094  # an interactive login reads and writes the terminal
    compose run --rm sloane claude <"$TTY" >"$TTY" 2>&1 || true
  fi

  # -- 6. Check everything, then run under systemd --------------------------------------
  say "Running doctor (downloads the 130 MB embedder the first time)"
  compose run --rm -T sloane python scripts/doctor.py --warm </dev/null || \
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
}

main "$@"
