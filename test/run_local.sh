#!/bin/bash
# Resets the local stack (PostgreSQL 16 + the Supabase stand-ins + the fake edge on :8787),
# builds the respondent page and the test deck for the `local` env, then runs the three
# suites: SQL (test_sessions_local.py), respondent page (test_respondent_page.py) and
# deck controller (test_deck.py). Exit code is non-zero when any suite fails.
#
#   bash test/run_local.sh            reset, build, run every suite
#   bash test/run_local.sh --no-run   reset and build only; leaves the fake edge running
#
# Needs: psql + a local PostgreSQL 16 (Debian/Ubuntu layout, started here if stopped),
# python3 with psycopg2, aiohttp, requests and playwright (+ `playwright install chromium`).
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DSN="dbname=llpoll user=postgres password=postgres host=127.0.0.1"

# Run a shell line as the postgres superuser: su as root, sudo on a developer machine or a CI runner.
pg_run() { if [ "$(id -u)" = "0" ]; then su postgres -c "$1"; else sudo -u postgres bash -c "$1"; fi; }

pkill -f fake_supabase.py 2>/dev/null || true
if ! pg_isready -q -h 127.0.0.1 2>/dev/null; then
  if [ "$(id -u)" = "0" ]; then pg_ctlcluster 16 main start; else sudo pg_ctlcluster 16 main start || sudo systemctl start postgresql; fi
  sleep 1
fi

# Fresh database: the stand-ins, then every migration in order.
pg_run "dropdb --if-exists llpoll && createdb llpoll"
MIGRATIONS=$(ls "$ROOT"/supabase/migrations/*.sql | sort | sed 's/^/-f /' | tr '\n' ' ')
pg_run "psql -v ON_ERROR_STOP=1 -q -X -d llpoll -f '$ROOT/test/local-supabase-stub.sql' $MIGRATIONS" 2>&1 | grep -v -E "NOTICE|WARNING|HINT" || true
pg_run "psql -q -X -d llpoll -c \"alter user postgres password 'postgres';\""

# The respondent page and the test deck for the local env. supabase-js for the page is an ES module;
# the fake edge serves a shim built from the UMD bundle in deck/, so nothing is fetched from a CDN.
python3 build.py --env local
python3 test/build_test_deck.py --env local
{ cat deck/supabase.umd.js; printf '\nexport const createClient = self.supabase.createClient;\nexport default self.supabase;\n'; } > test/supabase-esm.js

nohup python3 test/fake_supabase.py --db "$DSN" \
  --serve test/index.local.html:/ \
  --serve legacy/index.html:/legacy.html \
  --serve test/supabase-esm.js:/vendor/supabase-esm.js \
  --port 8787 > test/fake.log 2>&1 &
sleep 2
cat test/fake.log
curl -sf -o /dev/null http://127.0.0.1:8787/ || { echo "fake edge did not start"; exit 1; }

[ "${1:-}" = "--no-run" ] && exit 0

FAILED=0
echo "== SQL suite =="
python3 test/test_sessions_local.py "$DSN" supabase/migrations/0001_sessions.sql || FAILED=1
echo "== respondent page suite =="
python3 test/test_respondent_page.py test/shots || FAILED=1
echo "== deck controller suite =="
DECK_FILE="$ROOT/test/deck.local.html" python3 test/test_deck.py test/shots-deck || FAILED=1
pkill -f fake_supabase.py 2>/dev/null || true
exit $FAILED
