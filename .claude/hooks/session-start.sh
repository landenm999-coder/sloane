#!/bin/bash
# Claude Code on the web: make the test suite runnable before the session starts.
#   - a venv at .venv with requirements.txt + pyflakes (the linter we use)
#   - Postgres 16 + pgvector on /tmp:5433 with every migration applied
#     (scripts/dev_db.sh; creates sloane, sloane_eval, sloane_review)
# Idempotent. Local sessions are left alone.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt pyflakes

sh scripts/dev_db.sh

{
  echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\""
  # The integration tests are destructive; this points them at the local
  # throwaway database, never at anything real.
  echo 'export DATABASE_URL="postgresql://postgres@/sloane?host=/tmp&port=5433"'
} >> "$CLAUDE_ENV_FILE"
