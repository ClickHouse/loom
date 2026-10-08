#!/usr/bin/env bash
# Weekly git housekeeping for every repository under var/code_repos (the bare
# mirrors the ingest fetches into, and the compiler tiers' checkouts).
#
# git's own `gc --auto` is turned off in each of them (gc.auto 0,
# maintenance.auto false): it starts inside whatever git command crosses its
# loose-object threshold, and a repack of a large checkout writes a temporary
# pack as big as the repository — enough, once, to fill the host disk. Here the
# repack runs at a known time, at idle CPU and IO priority, and only when the
# disk has room for it: free space above the size of the repository's git
# directory (the new pack is written beside the old one) plus
# REPO_GC_MARGIN_GB. A checkout is repacked under its entry lock (`loom code
# entries`), so it never races a scip pass on the same tree; a held lock skips
# it this week.
#
# One line per repository:
#   STEP repo-gc path=<dir> exit=<code> seconds=<n> [reason=<why>]
#
# Install: copy to <loom root>/var/cron/ (see docs/operations/code-index-operations.md).
set -euo pipefail
LOOM_ROOT=${LOOM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
CRON_DIR=$LOOM_ROOT/var/cron
cd "$LOOM_ROOT"
LOG=$CRON_DIR/repo-gc.log
MARGIN_GB=${REPO_GC_MARGIN_GB:-50}
GC_TIMEOUT=${REPO_GC_TIMEOUT:-4h}

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

exec 9>"$CRON_DIR/repo-gc.lock"
if ! flock -n 9; then
  echo "=== $(stamp) repo-gc: previous pass still running — skipped ===" >>"$LOG"
  exit 0
fi

# checkout path -> entry lock, for the trees a scip pass works in
declare -A LOCKS=()
if ENTRIES=$(.venv/bin/python -m server.cli code entries 2>>"$LOG"); then
  while read -r _ORG _NS CHECKOUT LOCK; do
    [ -n "$CHECKOUT" ] || continue
    [[ "$CHECKOUT" == /* ]] || CHECKOUT=$LOOM_ROOT/$CHECKOUT
    LOCKS[$(realpath -m "$CHECKOUT")]=$LOCK
  done <<<"$ENTRIES"
else
  echo "STEP repo-gc path=all exit=1 seconds=0 reason=entry-list-failed" >>"$LOG"
fi

# Object dirs another repository borrows from (objects/info/alternates; a relative
# entry is relative to that objects dir). A repository holding one must never
# prune: its unreachable objects may be another checkout's only copy.
declare -A BORROWED=()
for alt in var/code_repos/*/.git/objects/info/alternates var/code_repos/*/objects/info/alternates \
  var/code_repos/self/*/.git/objects/info/alternates; do
  [ -f "$alt" ] || continue
  base=$(dirname "$(dirname "$alt")")
  while read -r entry; do
    [ -n "$entry" ] || continue
    [[ "$entry" == /* ]] || entry=$base/$entry
    BORROWED[$(realpath -m "$entry")]=1
  done <"$alt"
done

gc_one() {
  local dir=$1 t0=$SECONDS rc=0
  git -C "$dir" config gc.auto 0
  git -C "$dir" config maintenance.auto false
  local need_kb free_kb
  local git_dir group
  git_dir=$(git -C "$dir" rev-parse --absolute-git-dir)
  # The repository is shared with the workers container through its group, not
  # its owner: what the repack writes keeps the git dir's group and group access
  # (core.sharedRepository), or the container can no longer read or fetch.
  group=$(stat -c %g "$git_dir")
  git -C "$dir" config core.sharedRepository group
  if [ -n "${BORROWED[$(realpath -m "$git_dir/objects")]:-}" ]; then
    git -C "$dir" config gc.pruneExpire never
  fi
  need_kb=$(($(du -sk "$git_dir" | cut -f1) + MARGIN_GB * 1024 * 1024))
  free_kb=$(df -Pk "$dir" | awk 'NR == 2 {print $4}')
  if [ "$free_kb" -lt "$need_kb" ]; then
    echo "STEP repo-gc path=$dir exit=0 seconds=0 reason=low-disk free_kb=$free_kb need_kb=$need_kb"
    return
  fi
  echo "=== $(stamp) repo-gc $dir ==="
  timeout --kill-after=5m "$GC_TIMEOUT" nice -n 19 ionice -c 3 git -C "$dir" gc --quiet || rc=$?
  # a file the repack wrote under this user's primary group goes back to the git dir's
  find "$git_dir" -user "$(id -u)" ! -group "$group" -exec chgrp -h "$group" {} + 2>/dev/null || true
  echo "STEP repo-gc path=$dir exit=$rc seconds=$((SECONDS - t0))"
}

for dir in var/code_repos/* var/code_repos/self/*; do
  [ -d "$dir" ] || continue
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || continue
  real=$(realpath -m "$LOOM_ROOT/$dir")
  lock=${LOCKS[$real]:-}
  if [ -n "$lock" ]; then
    mkdir -p "$(dirname "$lock")"
    (
      if ! flock -n 8; then
        echo "STEP repo-gc path=$dir exit=0 seconds=0 reason=entry-lock-held"
        exit 0
      fi
      gc_one "$dir"
    ) 8>"$lock" >>"$LOG" 2>&1 || true
  else
    gc_one "$dir" >>"$LOG" 2>&1 || true
  fi
done
tail -c 1048576 "$LOG" >"$CRON_DIR/.g.tmp" && mv "$CRON_DIR/.g.tmp" "$LOG"
