#!/usr/bin/env bash
# Weekly full compiler-tier re-index for EVERY configured entry (the backstop:
# heals TU-cap degrades of per-merge deltas and any drift, and seeds entries
# the delta reports as "watermarks not seeded"). ~50 min per entry.
#
# Locks: scip-full.lock keeps two full passes apart. `loom code scip-full`
# holds the entry's own lock (`loom code scip-lock-path`) for the whole entry,
# waiting for a delta already on it; scip-delta.sh skips only that entry
# meanwhile, so every other entry stays fresh. Runs under nice so the deltas
# keep their share of the CPU.
#
# Each entry runs under a timeout far above its observed duration, so a hung
# re-index frees its entry lock and the next entry still runs. After each
# entry the log gets one line to grep:
#   STEP scip-full ns=<namespace> exit=<code> seconds=<n>   (124: timed out)
#
# Install: copy to <loom root>/var/cron/ (see docs/operations/code-index-operations.md).
set -euo pipefail
LOOM_ROOT=${LOOM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
CRON_DIR=$LOOM_ROOT/var/cron
cd "$LOOM_ROOT"
LOG=$CRON_DIR/scip-full.log
ENTRY_TIMEOUT=${SCIP_FULL_ENTRY_TIMEOUT:-8h}
trap 'rm -f "$CRON_DIR/.f.tmp"' EXIT

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

exec 9>"$CRON_DIR/scip-full.lock"
if ! flock -n 9; then
  echo "=== $(stamp) scip-full: previous pass still running — skipped ===" >>"$LOG"
  exit 0
fi
if ! ENTRIES=$(
  .venv/bin/python -m server.cli code entries 2>>"$LOG"
); then
  echo "STEP entries ns=all exit=1 seconds=0 reason=entry-list-failed" >>"$LOG"
  exit 1
fi
while read -r ORG NS CHECKOUT _LOCK; do
  [ -n "$NS" ] || continue
  [ -d "$CHECKOUT" ] || {
    echo "skip $NS: no checkout at $CHECKOUT" >>"$LOG"
    continue
  }
  {
    rc=0 t0=$SECONDS
    echo "=== $(stamp) scip-full $NS ==="
    timeout --kill-after=5m "$ENTRY_TIMEOUT" \
      nice -n 10 .venv/bin/python -m server.cli code scip-full --org "$ORG" --namespace "$NS" || rc=$?
    echo "STEP scip-full ns=$NS exit=$rc seconds=$((SECONDS - t0))"
  } >>"$LOG" 2>&1
  tail -c 2097152 "$LOG" >"$CRON_DIR/.f.tmp" && mv "$CRON_DIR/.f.tmp" "$LOG"
done <<<"$ENTRIES"
