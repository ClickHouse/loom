#!/usr/bin/env bash
# Every 30 min: C++ compiler-tier delta for every configured repo entry (the
# worker loops entries itself), then per-entry SQL<->C++ crossref and the
# Python compiler tier.
#
# Locks: scip-delta.lock keeps two delta passes apart on this host. Each entry
# has its own lock, this host's file plus the deployment's ClickHouse lease,
# shared with scip-full.sh on every host: the C++ pass skips an entry a full
# re-index holds (named in its RESULT line), and the crossref + Python steps
# below run under `loom code entry-lock`, which skips it too (exit 75, a line in
# the log) and kills a step whose lease is lost (exit 3). A lease that cannot be
# taken (ClickHouse unreachable) fails the step (exit 1). Every other entry goes on.
#
# Each step runs under a timeout far above its observed duration, so a hung
# step ends and frees the pass lock instead of skipping every later pass.
# After each step the log gets one line to grep:
#   STEP <step> ns=<namespace> exit=<code> seconds=<n> [reason=<why>]
# where <step> is scip-delta, crossref, python-scip or entry-lock, and exit
# 124 means the timeout ended it.
# An entry the delta cannot serve yet ("watermarks not seeded" in the RESULT
# line) is seeded by the weekly scip-full.sh.
#
# Install: copy to <loom root>/var/cron/ (see docs/operations/code-index-operations.md).
set -euo pipefail
LOOM_ROOT=${LOOM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
CRON_DIR=$LOOM_ROOT/var/cron
cd "$LOOM_ROOT"
LOG=$CRON_DIR/scip-delta.log
CPP_TIMEOUT=${SCIP_DELTA_CPP_TIMEOUT:-3h}
CROSSREF_TIMEOUT=${SCIP_DELTA_CROSSREF_TIMEOUT:-1h}
PY_TIMEOUT=${SCIP_DELTA_PY_TIMEOUT:-2h}
trap 'rm -f "$CRON_DIR/.d.tmp"' EXIT

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# step <id> <namespace> <timeout> <command...>: header, bounded run, STEP line.
step() {
  local name=$1 ns=$2 limit=$3 rc=0 t0=$SECONDS
  shift 3
  echo "=== $(stamp) $name $ns ==="
  timeout --kill-after=5m "$limit" "$@" || rc=$?
  echo "STEP $name ns=$ns exit=$rc seconds=$((SECONDS - t0))"
}

# entry_step <id> <org> <namespace> <timeout> <command...>: step under the entry
# lock; returns 75 when another pass holds the entry.
entry_step() {
  local name=$1 org=$2 ns=$3 limit=$4 rc=0 t0=$SECONDS
  shift 4
  echo "=== $(stamp) $name $ns ==="
  timeout --kill-after=5m "$limit" .venv/bin/python -m server.cli code entry-lock \
    --org "$org" --namespace "$ns" -- "$@" || rc=$?
  if [ "$rc" -eq 75 ]; then
    echo "=== $(stamp) skip $ns: entry lock held (full pass running) ==="
    echo "STEP entry-lock ns=$ns exit=0 seconds=0 reason=held-by-full-pass"
    return 75
  fi
  echo "STEP $name ns=$ns exit=$rc seconds=$((SECONDS - t0))"
}

exec 9>"$CRON_DIR/scip-delta.lock"
if ! flock -n 9; then
  echo "=== $(stamp) scip-delta: previous pass still running — skipped ===" >>"$LOG"
  exit 0
fi
step scip-delta all "$CPP_TIMEOUT" .venv/bin/python -m server.cli code scip-delta >>"$LOG" 2>&1
tail -c 1048576 "$LOG" >"$CRON_DIR/.d.tmp" && mv "$CRON_DIR/.d.tmp" "$LOG"
# one line per configured entry: org namespace checkout lock-file
if ! ENTRIES=$(
  .venv/bin/python -m server.cli code entries 2>>"$LOG"
); then
  echo "STEP entries ns=all exit=1 seconds=0 reason=entry-list-failed" >>"$LOG"
  exit 1
fi
WRITER_TOKEN=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['token'])" "$CRON_DIR/loom_writer_token.json" 2>>"$LOG" || true)
while read -r ORG NS CHECKOUT _LOCK; do
  [ -n "$NS" ] || continue
  [[ "$CHECKOUT" == /* ]] || CHECKOUT=$LOOM_ROOT/$CHECKOUT
  [ -d "$CHECKOUT" ] || {
    echo "skip $NS: no checkout at $CHECKOUT" >>"$LOG"
    continue
  }
  (
    rc=0
    entry_step crossref "$ORG" "$NS" "$CROSSREF_TIMEOUT" \
      .venv/bin/python -m server.cli code crossref --org "$ORG" --namespace "$NS" || rc=$?
    [ "$rc" -ne 75 ] || exit 0
    if [ -z "$WRITER_TOKEN" ]; then
      echo "STEP python-scip ns=$NS exit=1 seconds=0 reason=no-writer-token"
      exit 0
    fi
    LOOM_API_KEY=$WRITER_TOKEN \
      SCIP_PYTHON=$LOOM_ROOT/var/scip-python/node_modules/.bin/scip-python \
      PYTHON=$LOOM_ROOT/.venv/bin/python \
      entry_step python-scip "$ORG" "$NS" "$PY_TIMEOUT" \
      bash "$LOOM_ROOT/scripts/scip_harness/run_python_delta.sh" "$CHECKOUT" http://localhost:7777 "$ORG" "$NS" || true
  ) >>"$LOG" 2>&1 || true
done <<<"$ENTRIES"
