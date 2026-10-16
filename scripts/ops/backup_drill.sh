#!/usr/bin/env bash
# Checks that dev/backup_loom.py produces a complete dump of the live database
# and, with --full-restore, that dev/restore_loom.py loads it back.
#
# 1. Print the target, check ClickHouse answers, snapshot the row count per table.
# 2. Run dev/backup_loom.py into a private temporary directory.
# 3. Check the manifest: complete, lists exactly the drill's tables, none
#    failed, each table has its file; report row-count drift against the
#    snapshot.
# 4. --full-restore: drop and recreate the scratch database <db>_drill, run
#    dev/restore_loom.py into it, and compare every table's row count with the
#    manifest. The scratch database is dropped on every exit path.
#
# Reads the live database only; --full-restore writes only to <db>_drill. The
# temporary directory (dump and logs) is removed on exit; a failing stage
# prints the tail of its log first. The password reaches the backup and
# restore tools through LOOM_CH_PASSWORD only, never an argument.
#
# Usage:
#   ./scripts/ops/backup_drill.sh [--full-restore]
#
# Env: .env at the repository root (LOOM_CH_HOST, LOOM_CH_PORT, LOOM_CH_USER,
# LOOM_CH_PASSWORD, LOOM_CH_DATABASE; LOOM_CH_SECURE=true or port 8443 for
# HTTPS); PYTHON (default .venv/bin/python).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PYTHON=${PYTHON:-.venv/bin/python}

log() { printf "\n\033[1;34m▸\033[0m %s\n" "$*"; }
ok() { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
fail() {
  printf "  \033[1;31m✗\033[0m %s\n" "$*" >&2
  exit 1
}

FULL_RESTORE=0
case "${1:-}" in
  "") ;;
  --full-restore) FULL_RESTORE=1 ;;
  *) fail "unknown argument: $1 (usage: $0 [--full-restore])" ;;
esac

[[ -f .env ]] || fail "no .env at $REPO_ROOT"
set -a
# shellcheck source=/dev/null
source .env
set +a
: "${LOOM_CH_HOST:?set LOOM_CH_HOST in .env}"
: "${LOOM_CH_PORT:?set LOOM_CH_PORT in .env}"
: "${LOOM_CH_USER:?set LOOM_CH_USER in .env}"
: "${LOOM_CH_DATABASE:?set LOOM_CH_DATABASE in .env}"
command -v jq >/dev/null || fail "jq is required"

DRILL_DB="${LOOM_CH_DATABASE}_drill"
if [[ $FULL_RESTORE -eq 1 && ("$DRILL_DB" == "$LOOM_CH_DATABASE" || "$DRILL_DB" == loom) ]]; then
  fail "the scratch database $DRILL_DB is a live database name; refusing"
fi

SCHEME=http
if [[ "$LOOM_CH_PORT" == "8443" || "${LOOM_CH_SECURE:-}" == "true" ]]; then SCHEME=https; fi
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/loom-backup-drill.XXXXXX")
DRILL_CREATED=0

# The credentials reach curl on stdin as a config file, never in its argv
# (visible in ps) or in xtrace output.
ch_query() {
  { set +x; } 2>/dev/null
  local pw=${LOOM_CH_PASSWORD:-}
  pw=${pw//\\/\\\\}
  pw=${pw//\"/\\\"}
  printf 'user = "%s:%s"\n' "$LOOM_CH_USER" "$pw" \
    | curl -sfS -K - --data-binary "$1" "${SCHEME}://${LOOM_CH_HOST}:${LOOM_CH_PORT}/"
}

cleanup() {
  local rc=$?
  if [[ $DRILL_CREATED -eq 1 ]]; then
    if ch_query "DROP DATABASE IF EXISTS \`$DRILL_DB\` SYNC" >/dev/null; then
      echo "  scratch database $DRILL_DB dropped"
    else
      echo "  ! could not drop the scratch database $DRILL_DB; drop it by hand" >&2
    fi
  fi
  rm -rf "$SCRATCH"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The last lines of a stage's log, then fail.
fail_with_log() {
  tail -n 20 "$2" >&2 || true
  fail "$1"
}

log "Target"
echo "  ${SCHEME}://${LOOM_CH_HOST}:${LOOM_CH_PORT} database ${LOOM_CH_DATABASE} user ${LOOM_CH_USER} password $([[ -n "${LOOM_CH_PASSWORD:-}" ]] && echo set || echo unset)"
version=$(ch_query "SELECT version()") || fail "ClickHouse does not answer"
echo "  server $version"

# The tables a backup must carry (TABLES_SQL in dev/backup_loom.py): every
# MergeTree-family table but the migration runner's; the manifest check below
# fails when the dump leaves one out.
mapfile -t TABLES < <(ch_query "SELECT name FROM system.tables WHERE database = '${LOOM_CH_DATABASE}' AND engine LIKE '%MergeTree' AND NOT startsWith(name, '_') AND NOT startsWith(name, '.') ORDER BY name")
[[ ${#TABLES[@]} -gt 0 ]] || fail "no tables in ${LOOM_CH_DATABASE}"
[[ $FULL_RESTORE -eq 1 ]] && echo "  scratch database for the restore: $DRILL_DB (dropped and recreated)"

log "Snapshotting row counts"
declare -A COUNTS
for t in "${TABLES[@]}"; do
  COUNTS[$t]=$(ch_query "SELECT count() FROM ${LOOM_CH_DATABASE}.${t}")
  printf "  %-28s %s\n" "$t" "${COUNTS[$t]}"
done

log "Running backup → $SCRATCH"
rc=0
"$PYTHON" dev/backup_loom.py --out-dir "$SCRATCH" >"$SCRATCH/backup.log" 2>&1 || rc=$?
case $rc in
  0) ok "backup completed" ;;
  1) fail_with_log "the backup is partial: a table failed (exit 1)" "$SCRATCH/backup.log" ;;
  2) fail_with_log "the backup refused to run: no target host or bad arguments (exit 2)" "$SCRATCH/backup.log" ;;
  *) fail_with_log "the backup failed (exit $rc)" "$SCRATCH/backup.log" ;;
esac

MANIFEST="$SCRATCH/manifest.json"
[[ -f "$MANIFEST" ]] || fail "manifest.json missing"
failed=$(jq -r '[.tables[] | select(has("error")) | .name] | join(" ")' "$MANIFEST")
[[ -z "$failed" ]] || fail "the backup recorded errors for: $failed"
[[ "$(jq -r '.complete' "$MANIFEST")" == "true" ]] || fail "the manifest is not marked complete"
listed=$(jq -r '[.tables[].name] | sort | join(" ")' "$MANIFEST")
expected=$(printf '%s\n' "${TABLES[@]}" | sort | paste -sd' ' -)
[[ "$listed" == "$expected" ]] || fail "manifest lists [$listed], the drill checks [$expected]"
ok "manifest complete, lists the ${#TABLES[@]} tables, none failed"

mismatch=0
for t in "${TABLES[@]}"; do
  f="$SCRATCH/${t}.jsonl"
  [[ -f "$f" ]] || fail "missing $f"
  lines=$(wc -l <"$f" | tr -d ' ')
  if [[ "$lines" != "${COUNTS[$t]}" ]]; then
    printf "  \033[1;33m!\033[0m %-28s file=%s expected=%s (rows written or replaced since the snapshot)\n" "$t" "$lines" "${COUNTS[$t]}"
    mismatch=$((mismatch + 1))
  fi
done
if [[ $mismatch -eq 0 ]]; then
  ok "all ${#TABLES[@]} table files match row counts"
else
  echo "  ($mismatch tables drifted between the snapshot and the dump; review manually)"
fi

if [[ $FULL_RESTORE -eq 1 ]]; then
  log "Full restore drill → $DRILL_DB"
  DRILL_CREATED=1
  ch_query "DROP DATABASE IF EXISTS \`$DRILL_DB\` SYNC" >/dev/null
  ch_query "CREATE DATABASE \`$DRILL_DB\`" >/dev/null
  ok "scratch database $DRILL_DB is empty"
  rc=0
  "$PYTHON" dev/restore_loom.py --in-dir "$SCRATCH" --database "$DRILL_DB" \
    >"$SCRATCH/restore.log" 2>&1 || rc=$?
  case $rc in
    0) ok "restore completed into $DRILL_DB" ;;
    1) fail_with_log "the restore is partial: a create or insert failed (exit 1)" "$SCRATCH/restore.log" ;;
    2) fail_with_log "the restore refused: live database, non-empty target tables, or a file that fails the manifest (exit 2)" "$SCRATCH/restore.log" ;;
    *) fail_with_log "the restore failed (exit $rc)" "$SCRATCH/restore.log" ;;
  esac
  bad=0
  # A table that collapses rows of one key (Replacing, Aggregating, Summing,
  # Collapsing) may merge in the scratch database right after its attach, so
  # fewer rows than dumped is expected there once a merge has run (an active
  # part spanning more than one block). Any other difference fails.
  while IFS=$'\t' read -r t want; do
    have=$(ch_query "SELECT count() FROM \`$DRILL_DB\`.\`$t\`")
    if [[ "$have" == "$want" ]]; then
      printf "  %-28s %s\n" "$t" "$have"
      continue
    fi
    merged=$(ch_query "SELECT countIf(min_block_number < max_block_number) > 0 AND any(t.engine) NOT IN ('MergeTree', 'ReplicatedMergeTree', 'SharedMergeTree') FROM system.parts AS p INNER JOIN system.tables AS t ON t.database = p.database AND t.name = p.table WHERE p.database = '$DRILL_DB' AND p.table = '$t' AND p.active")
    if [[ "$merged" == "1" && "$have" -lt "$want" ]]; then
      printf "  \033[1;33m!\033[0m %-26s restored=%s manifest=%s (collapsed by a merge after the attach)\n" "$t" "$have" "$want"
    else
      printf "  \033[1;31m✗\033[0m %-26s restored=%s manifest=%s\n" "$t" "$have" "$want" >&2
      bad=$((bad + 1))
    fi
  done < <(jq -r '.tables[] | select(has("rows")) | [.name, .rows] | @tsv' "$MANIFEST")
  [[ $bad -eq 0 ]] || fail "$bad tables restored a row count other than the manifest's"
  ok "every restored table matches the manifest"
fi

echo
echo "Backup drill complete; the dump in $SCRATCH is removed on exit."
