#!/usr/bin/env bash
# Go compiler tier for a Go repository, one module or many: one scip-go
# run per `go.mod` directory, each converted with `--path-prefix <dir>` so the
# delta's paths are repository-relative (the parser tier's), then uploaded as
# its own batch (or written to DELTA_OUT_DIR/<dir with / as __>.json; the root
# module, prefix "", to DELTA_OUT_DIR/.json).
#
#   upload mode    run_go_monorepo_delta.sh <checkout> <loom_url> <org> <namespace>
#   DELTA_OUT_DIR  DELTA_OUT_DIR=/path run_go_monorepo_delta.sh <checkout> --full <head>
#                  DELTA_OUT_DIR=/path run_go_monorepo_delta.sh <checkout> <base> <head>
#                  (per module, only the Go files the window
#                  changed or deleted; a module with none is skipped)
#
# Env: LOOM_API_KEY (writer; upload mode), SCIP_GO, PYTHON, GOFLAGS, MODULES (a
# newline list of module dirs to run instead of every go.mod; e.g. the ones a
# merge touched), MIN_GO_FILES (skip modules with fewer .go files; default 1),
# JOBS (parallel scip-go runs; default 2 — budget ~4 GB RAM each: the Go compiler
# runs inside scip-go), EXCLUDE_GENERATED (default 1: drop generated documents). A module whose
# `go mod download` fails is logged and skipped, never fatal to the run.
# Measured: a 425-file module → 5,461 symbols and
# 37,909 edges in 5 min on 2 cores.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CHECKOUT=$(absolute_dir "${1:?checkout path}")
SCIP_GO=${SCIP_GO:-scip-go}
JOBS=${JOBS:-2}
MIN_GO_FILES=${MIN_GO_FILES:-1}
unset DELTA_OUT # one delta per module: DELTA_OUT_DIR names where they go

if [[ -n "${DELTA_OUT_DIR:-}" ]]; then
  BASE=${2:?base sha or --full}
  HEAD_SHA=${3:?head sha}
  LOOM_BASE_URL=""
  ORG=""
  NS=""
  mkdir -p "$DELTA_OUT_DIR"
else
  LOOM_BASE_URL=${2:?loom url}
  ORG=${3:-}
  NS=${4:-}
  BASE=""
  HEAD_SHA=$(git -C "$CHECKOUT" rev-parse HEAD)
  : "${LOOM_API_KEY:?LOOM_API_KEY (writer token) is required in upload mode}"
fi
command -v "$SCIP_GO" >/dev/null || {
  echo "scip-go not found (SCIP_GO=$SCIP_GO)" >&2
  exit 2
}

# a window's base must be a commit here, or every module's window reads empty
if [[ -n "$BASE" && "$BASE" != "--full" ]]; then
  require_base "$BASE" "$CHECKOUT"
fi

if [[ -n "${MODULES:-}" ]]; then
  mapfile -t DIRS <<<"$MODULES"
else
  mapfile -t DIRS < <(cd "$CHECKOUT" && git_paths ls-files -- '**/go.mod' 'go.mod' | while IFS= read -r m; do dirname -- "$m"; done | sort -u)
fi

run_module() {
  local dir=$1 work
  work=$(mktemp -d)
  trap 'rm -rf "$work"' RETURN
  local n
  n=$(cd "$CHECKOUT/$dir" && git_paths ls-files -- '*.go' ':!:*/vendor/*' | wc -l)
  if ((n < MIN_GO_FILES)); then
    echo "skip $dir ($n go files)"
    return 0
  fi
  local prefix=$dir
  [[ "$dir" == "." ]] && prefix=""
  # The converter reads each package's clause from the tree so a
  # monorepo module's symbols are spelled the way the fast tier spells them
  local args=(--index "$work/index.scip" --commit "$HEAD_SHA" --producer scip-go
    --path-prefix "$prefix" --source-root "$CHECKOUT")
  [[ "${EXCLUDE_GENERATED:-1}" == "1" ]] && args+=(--exclude-generated)
  local window=0
  if [[ "$BASE" == "--full" || -z "$BASE" ]]; then
    args+=(--full)
  else
    window=1
    # A window — the module's changed and deleted Go files
    # between BASE and HEAD (repository-relative; the converter strips the
    # prefix), so the delta tombstones what the window removed
    (cd "$CHECKOUT" && window_files "$work" "$BASE" "$HEAD_SHA" "${prefix:+$prefix/}*.go")
    if [[ ! -s "$work/changed.txt" && ! -s "$work/deleted.txt" ]]; then
      echo "skip $dir (no Go change in window)"
      return 0
    fi
    args+=(--base "$BASE" --changed-files "$work/changed.txt" --deleted-files "$work/deleted.txt")
  fi
  if ((window)) && [[ ! -s "$work/changed.txt" ]]; then
    : >"$work/index.scip" # only deletions: an empty Index, no scip-go run
  elif ! (cd "$CHECKOUT/$dir" && go mod download >"$work/download.log" 2>&1); then
    echo "skip $dir: go mod download failed: $(tail -1 "$work/download.log")" >&2
    return 0
  elif ! (cd "$CHECKOUT/$dir" && "$SCIP_GO" --output "$work/index.scip" >"$work/scip-go.log" 2>&1); then
    echo "skip $dir: scip-go failed: $(tail -1 "$work/scip-go.log")" >&2
    return 0
  fi
  if [[ -n "${DELTA_OUT_DIR:-}" ]]; then
    DELTA_OUT="$DELTA_OUT_DIR/${prefix//\//__}.json" convert_delta "${args[@]}" | tail -1
  else
    convert_delta "${args[@]}" | tail -1
  fi
  echo "done $dir"
}
export -f run_module git_paths window_files convert_delta
export CHECKOUT SCIP_GO PYTHON HARNESS_DIR HEAD_SHA BASE LOOM_BASE_URL ORG NS MIN_GO_FILES DELTA_OUT_DIR LOOM_API_KEY EXCLUDE_GENERATED
printf '%s\n' "${DIRS[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_module "$@"' _ {}
