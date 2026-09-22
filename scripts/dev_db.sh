#!/bin/sh
# Local Postgres 16 + pgvector for tests, on a unix socket at /tmp, port 5433.
# Idempotent: installs if missing, initialises once, starts if stopped, and
# creates the databases the tests and the eval use. Safe to re-run any time
# (sandboxes reap idle processes; this brings it back).
#
#   sh scripts/dev_db.sh
#   export DATABASE_URL="postgresql://postgres@/sloane?host=/tmp&port=5433"
set -e
PGBIN=/usr/lib/postgresql/16/bin
DATA=/var/lib/postgresql/sloane
ROOT=$(cd "$(dirname "$0")/.." && pwd)

if [ ! -x "$PGBIN/pg_ctl" ] || [ ! -f /usr/share/postgresql/16/extension/vector.control ]; then
  apt-get update -qq && apt-get install -y -qq postgresql-16 postgresql-16-pgvector >/dev/null
fi
if [ ! -f "$DATA/PG_VERSION" ]; then
  mkdir -p "$DATA" && chown postgres:postgres "$DATA"
  su postgres -c "$PGBIN/initdb -D $DATA -A trust -U postgres" >/dev/null
fi
su postgres -c "$PGBIN/pg_ctl -D $DATA status" >/dev/null 2>&1 || \
  su postgres -c "$PGBIN/pg_ctl -D $DATA -o '-p 5433 -k /tmp' -l /tmp/pg.log start" >/dev/null
sleep 1

for db in sloane sloane_eval sloane_review; do
  psql "postgresql://postgres@/postgres?host=/tmp&port=5433" -tAc \
    "select 1 from pg_database where datname='$db'" | grep -q 1 || \
    psql "postgresql://postgres@/postgres?host=/tmp&port=5433" -qc "create database $db"
  for f in "$ROOT"/sql/*.sql; do
    psql "postgresql://postgres@/$db?host=/tmp&port=5433" -v ON_ERROR_STOP=1 -q -f "$f" 2>&1 | grep -v NOTICE || true
  done
done
echo "postgres ready: postgresql://postgres@/sloane?host=/tmp&port=5433"
