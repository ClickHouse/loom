# shellcheck shell=bash
# Sourced by the run_*_delta.sh harnesses: the steps every compiler tier shares.
# Indexing is each tier's own; converting and delivering the delta is here.
#
# Sets HARNESS_DIR, LOOM_ROOT, PYTHON (default python3; the converter needs
# protobuf, so point it at a venv python) and WORK, a temp dir removed on exit.

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034 # read by the harnesses that source this file
LOOM_ROOT="$(cd "$HARNESS_DIR/../.." && pwd)"
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d)

_HARNESS_ON_EXIT=()
# on_exit <command>: run <command> at exit, the latest registered first, before
# WORK is removed. A failing command does not stop the others.
on_exit() {
  _HARNESS_ON_EXIT=("$1" "${_HARNESS_ON_EXIT[@]}")
}
_harness_exit() {
  local cmd
  for cmd in "${_HARNESS_ON_EXIT[@]}"; do eval "$cmd" || true; done
  rm -rf "$WORK"
}
trap _harness_exit EXIT

# absolute_dir <dir>: the harnesses `cd` into the checkout before converting,
# and a relative --source-root there names nothing.
absolute_dir() {
  (cd "$1" && pwd)
}

# The exit status of a harness whose window base is not a commit of the
# checkout (rewritten history, a pruned mirror): no delta is written, and the
# caller should rerun as a full index.
EXIT_BASE_NOT_FOUND=3

# require_base <base> [dir]: exit EXIT_BASE_NOT_FOUND unless <base> is a commit
# of the repository at <dir> (default: the current directory).
require_base() {
  if ! git -C "${2:-.}" cat-file -e "$1^{commit}" 2>/dev/null; then
    echo "scip harness: base commit $1 not found in $(cd "${2:-.}" && pwd); rerun as a full index" >&2
    exit "$EXIT_BASE_NOT_FOUND"
  fi
}

# git_paths <git args...>: `git <args>` printing one path per line exactly as
# the tree names it (core.quotePath off, so a non-ASCII name is not C-quoted).
# A name git must still quote (a tab, newline, quote or backslash in it) cannot
# be carried by a line-per-path list; it is dropped with a warning instead of
# being passed on as a path that names nothing.
git_paths() {
  git -c core.quotePath=false "$@" | while IFS= read -r p; do
    if [[ "$p" == \"* ]]; then
      echo "scip harness: skipping a path git cannot print unquoted: $p" >&2
    else
      printf '%s\n' "$p"
    fi
  done
  return "${PIPESTATUS[0]}"
}

# window_files <out_dir> <base> <head> [pathspec...]: the window's files in the
# current directory's repository, as <out_dir>/changed.txt (everything but
# deletions: what the index is read for) and <out_dir>/deleted.txt (deletions
# and a rename's old path: what the delta tombstones). Renames are split, so
# no old path hides inside a rename. Fails when git cannot diff the window.
window_files() {
  local out=$1 base=$2 head=$3
  shift 3
  : >"$out/changed.txt"
  : >"$out/deleted.txt"
  git_paths diff --name-only --no-renames --diff-filter=d --end-of-options "$base" "$head" -- "$@" \
    >"$out/changed.txt" \
    && git_paths diff --name-only --no-renames --diff-filter=D --end-of-options "$base" "$head" -- "$@" \
      >"$out/deleted.txt"
}

# convert_delta <scip_to_delta.py args...>: with DELTA_OUT set, write the
# JSON delta there (the in-server workers' mode); otherwise upload it to
# LOOM_BASE_URL with the writer token LOOM_API_KEY, addressed to ORG / NS
# when they are set.
convert_delta() {
  if [[ -n "${DELTA_OUT:-}" ]]; then
    "$PYTHON" "$HARNESS_DIR/scip_to_delta.py" "$@" --dry-run "$DELTA_OUT"
    return
  fi
  local up=(--loom-url "${LOOM_BASE_URL:?loom url required in upload mode}"
    --token "${LOOM_API_KEY:?LOOM_API_KEY (writer token) is required in upload mode}")
  [[ -n "${ORG:-}" ]] && up+=(--org "$ORG")
  [[ -n "${NS:-}" ]] && up+=(--namespace "$NS")
  "$PYTHON" "$HARNESS_DIR/scip_to_delta.py" "$@" "${up[@]}"
}
