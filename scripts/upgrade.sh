#!/usr/bin/env bash
# Put an accepted workshop change live on the box, or put the old version back.
#
# Run by systemd, never by hand: deploy/sloane-upgrade.path watches
# .deploy/request.json, which she writes when Landen presses Accept in the
# workshop (sloane/workshop.py). It pulls main, refuses a workshop commit that
# touched a protected file (the second lock; the workshop checks first),
# rebuilds, applies the migrations, restarts her, waits for /health, and rolls
# back to the version that was running if she doesn't come back. The outcome
# goes to .deploy/result.json, which she reads and tells him about.
set -euo pipefail

DIR=${SLOANE_DIR:-/opt/sloane}
DEPLOY="$DIR/.deploy"
REQUEST="$DEPLOY/request.json"
HEALTH_URL=${SLOANE_HEALTH_URL:-http://127.0.0.1:8000/health}
WAIT=${SLOANE_HEALTH_WAIT:-180}

[ -f "$REQUEST" ] || exit 0
TAKEN="$DEPLOY/request.taken"
mv -f "$REQUEST" "$TAKEN"
ITEM=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("item", ""))' "$TAKEN" 2>/dev/null || true)

# Git runs as whoever owns the checkout, so the next manual install still can.
OWNER=$(stat -c %U "$DIR")
as_owner() { if [ "$(id -un)" = "$OWNER" ]; then "$@"; else runuser -u "$OWNER" -- "$@"; fi; }
git_() { as_owner git -C "$DIR" "$@"; }
compose() { docker compose --project-directory "$DIR" "$@"; }

result() {  # STATUS DETAIL
  RESULT_STATUS="$1" RESULT_DETAIL="$2" RESULT_ITEM="$ITEM" RESULT_SHA="$(git_ rev-parse HEAD)" \
    python3 - "$DEPLOY" <<'PY'
import json, os, sys, time
folder = sys.argv[1]
body = {"item": os.environ["RESULT_ITEM"], "status": os.environ["RESULT_STATUS"],
        "detail": os.environ["RESULT_DETAIL"], "sha": os.environ["RESULT_SHA"], "at": time.time()}
partial = os.path.join(folder, ".result.json.part")
with open(partial, "w") as handle:
    json.dump(body, handle)
os.chmod(partial, 0o644)
os.replace(partial, os.path.join(folder, "result.json"))
PY
}

PREV=$(git_ rev-parse HEAD)

rollback() {  # DETAIL
  git_ reset -q --hard "$PREV"
  compose build -q </dev/null || true
  systemctl restart sloane || true
  result rolled_back "$1"
  exit 0
}

if ! git_ pull -q --ff-only; then
  result failed "couldn't pull main on the box (a local change in $DIR?)"
  exit 0
fi

# The protected list as it was before this pull: a commit can't loosen the lock it's checked against.
PROTECTED=$(git_ show "$PREV:deploy/protected.txt" 2>/dev/null || true)
while read -r sha; do
  [ -n "$sha" ] || continue
  while read -r path; do
    [ -n "$path" ] || continue
    if SLOANE_PATH="$path" SLOANE_PROTECTED="$PROTECTED" python3 -c '
import fnmatch, os, sys
patterns = [l.strip() for l in os.environ["SLOANE_PROTECTED"].splitlines() if l.strip() and not l.strip().startswith("#")]
sys.exit(0 if any(fnmatch.fnmatch(os.environ["SLOANE_PATH"], p) for p in patterns) else 1)'; then
      git_ reset -q --hard "$PREV"
      result failed "a workshop change touched a protected file ($path), so it wasn't deployed"
      exit 0
    fi
  done < <(git_ show --name-only --format= "$sha")
done < <(git_ log --format=%H --grep='^Workshop: ' "$PREV..HEAD")

compose build -q </dev/null || rollback "the new version didn't build"
# shellcheck disable=SC2016  # $DATABASE_URL expands inside the container
compose run --rm -T sloane sh -c \
  'for f in sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || exit 1; done' </dev/null \
  || rollback "a migration failed"
systemctl restart sloane

waited=0
while [ "$waited" -lt "$WAIT" ]; do
  sleep 5
  waited=$((waited + 5))
  if curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
    result live ""
    exit 0
  fi
done
rollback "she didn't come back healthy within $WAIT seconds, so the previous version is running again"
