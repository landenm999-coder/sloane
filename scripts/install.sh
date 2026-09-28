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

# field ASK KEY PROMPT: ask (with `ask` or `secret`) until the answer passes
# scripts/env_check.py, then write it to $draft. The reason for a retry goes to
# the terminal; the value is never shown.
field() {
  local how=$1 key=$2 prompt=$3 v out
  while :; do
    v=$("$how" "$prompt")
    if out=$(SLOANE_VALUE="$v" python3 "$DIR/scripts/env_check.py" "$key" 2>"$TTY"); then
      set_env "$draft" "$key" "$out"
      return
    fi
  done
}

# Everything runs inside main(), called on the last line. Under `curl | bash`
# the script *is* bash's stdin; wrapping it means bash has read all of it
# before the first command runs, so nothing below can swallow the rest.
main() {
  # She runs as a systemd service. WSL can have systemd off; say how to turn it on
  # now, not after ten minutes of building.
  if [ ! -d /run/systemd/system ]; then
    echo "systemd isn't running here, and Sloane runs as a systemd service." >&2
    printf '%s\n' "On Windows (WSL): printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf" >&2
    echo "then in PowerShell: wsl --shutdown   and open Ubuntu again and rerun this." >&2
    exit 1
  fi

  # -- 1. Docker ------------------------------------------------------------------
  if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker"
    sudo apt-get update -qq
    sudo apt-get install -y -qq git curl ca-certificates >/dev/null
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
  fi
  command -v git >/dev/null 2>&1 || sudo apt-get install -y -qq git >/dev/null
  if ! systemctl cat docker.service >/dev/null 2>&1; then
    echo "Docker here isn't a system service (Docker Desktop's WSL integration?), and her" >&2
    echo "service needs one. Turn that integration off for this distro, then run this again." >&2
    exit 1
  fi

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
    # Each answer is checked as it's given (scripts/env_check.py): a bad paste
    # gets the reason and the question again, not a broken .env.
    field secret DATABASE_URL 'DATABASE_URL (Supabase session pooler, port 5432): '
    field secret GROQ_API_KEY 'GROQ_API_KEY: '
    field secret TELEGRAM_BOT_TOKEN 'TELEGRAM_BOT_TOKEN: '
    field ask TELEGRAM_CHAT_ID 'TELEGRAM_CHAT_ID (a number): '
    field ask CANVAS_BASE_URL 'CANVAS_BASE_URL (e.g. https://dcsd.instructure.com): '
    field secret CANVAS_TOKEN 'CANVAS_TOKEN (Enter if Canvas has no New Access Token button): '
    if grep -q '^CANVAS_TOKEN=$' "$draft"; then
      # No token: the Calendar Feed still gives every due date.
      field secret CANVAS_FEED_URL 'CANVAS_FEED_URL (Canvas → Calendar → Calendar Feed link): '
    fi
    field secret CALENDAR_ICS_URL 'CALENDAR_ICS_URL (the secret iCal address): '
    local v
    # Two about her, not credentials. Enter keeps the default.
    v=$(ask 'What should she call you? (Enter for Landen, or e.g. sir): ')
    [ -n "$v" ] && set_env "$draft" ADDRESS_AS "$v"
    v=$(ask 'A British voice for her voice notes? [y/N]: ')
    case "$v" in
      [yY]*) set_env "$draft" SPEAK_PROVIDER piper
             set_env "$draft" PIPER_VOICE en_GB-cori-medium ;;
    esac
    v=$(ask 'Morning brief as a voice note, not text? [y/N]: ')
    case "$v" in
      [yY]*) set_env "$draft" VOICE_BRIEFS morning_brief ;;
    esac
    mv "$draft" .env
    echo "Saved .env (chmod 600). Edit it later with: nano $DIR/.env"
  else
    say ".env already exists -- leaving it alone"
    # Except for defaults the code has since moved past (a retired model, a
    # longer memory window): changed only where .env still holds the old one.
    python3 scripts/env_migrate.py .env
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

  say "Done. She'll message you on Telegram in a minute to say she's up."
  echo "Nothing? Open her chat in Telegram and press Start; she keeps trying for half an hour."
  echo "Then:     say hi, and work down START_HERE.md (step 5: try everything)"
  echo "Logs:     journalctl -u sloane -f"
  echo "Health:   curl -s localhost:8000/health"
  echo "Check-up: cd $DIR && sudo docker compose run --rm sloane python scripts/doctor.py"
  echo "Upgrade:  run this same command again"
}

main "$@"
