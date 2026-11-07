#!/usr/bin/env bash
# Compiler-tier delta for the loom code plane: scip-clang over the changed
# translation units of one commit range, converted + uploaded as a JSON delta.
#
# Usage:
#   run_delta.sh <checkout> <base_sha> <head_sha> <loom_url>
#   run_delta.sh <checkout> --full "" <loom_url>          # full-index backfill
#
# Env: DELTA_OUT (write the JSON delta there instead of uploading; the
# worker's mode), LOOM_API_KEY (WRITER token, upload mode), SCIP_CLANG (binary
# path, default scip-clang on PATH), PYTHON (converter python), MAX_TUS
# (default 200), JOBS (default nproc/2).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CHECKOUT=$(absolute_dir "${1:?checkout path}")
BASE=${2:?base sha or --full}
HEAD_SHA=${3:-}
LOOM_BASE_URL=${4:?loom url}
SCIP_CLANG=${SCIP_CLANG:-scip-clang}
MAX_TUS=${MAX_TUS:-200}
JOBS=${JOBS:-$(($(nproc) / 2 > 0 ? $(nproc) / 2 : 1))}

cd "$CHECKOUT"
HEAD_SHA=${HEAD_SHA:-$(git rev-parse HEAD)}

# 1. compile_commands.json — cmake configure only (no build). $STALE marks a
# compile database whose reconfigure failed, until one succeeds.
STALE=build/.loom-reconfigure-failed
if [[ ! -f build/compile_commands.json ]]; then
  # ClickHouse's CMake refuses GCC ("Compiler GNU is not supported"), and a
  # fresh checkout has no cache to remember the compiler — pin clang
  # explicitly.
  CC_BIN=${CC:-$(command -v clang-21 || command -v clang-18 || command -v clang)}
  CXX_BIN=${CXX:-$(command -v clang++-21 || command -v clang++-18 || command -v clang++)}
  echo ">> cmake configure (compile_commands.json) with $CXX_BIN…"
  # No compile_commands.json but a cache present = a failed earlier configure.
  # CMake refuses to switch compilers on an existing cache ("You have changed
  # variables that require your cache to be deleted"), so start clean.
  rm -rf build/CMakeCache.txt build/CMakeFiles
  cmake -B build -S . -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_C_COMPILER="$CC_BIN" -DCMAKE_CXX_COMPILER="$CXX_BIN" \
    -DCOMPILER_CACHE=disabled -DENABLE_RUST=0 >/dev/null
  # ^ the two gates a fresh checkout hits: cmake/ccache.cmake errors without ccache/sccache, and
  #   corrosion wants a Rust toolchain the indexer host does not have.
  rm -f "$STALE"
  CONFIGURED=1
fi

# reconfigure: the compile database lists only the TUs of the tree it was
# configured on; the existing cache keeps the compiler, and a failed configure
# keeps the last database. The marker only makes the next run retry: it is
# replaced, never written through, since the checkout can hold a link there.
reconfigure() {
  if [[ -z "${CONFIGURED:-}" ]]; then
    echo ">> cmake reconfigure (compile_commands.json): $1"
    # a delta also indexes the TUs the new database lists and the old did not (added
    # while a configure failed, or pulled in by a build edit); a full index reads all
    if [[ "$BASE" != "--full" ]]; then
      compdb_files "$WORK/listed.before.txt"
    fi
    rm -f "$STALE"
    if cmake -B build -S . >"$WORK/cmake.log" 2>&1; then
      if [[ "$BASE" != "--full" ]]; then
        compdb_files "$WORK/listed.after.txt"
        LC_ALL=C comm -13 "$WORK/listed.before.txt" "$WORK/listed.after.txt" \
          >"$WORK/newly_listed.txt"
      fi
    else
      : >"$STALE"
      tail -n 20 "$WORK/cmake.log" >"$WORK/cmake.tail"
      echo ">> CMake exited non-zero; indexing with the last compile_commands.json" >&2
    fi
  fi
}

# compdb_files <out>: the repository's source files compile_commands.json lists,
# relative to the checkout, sorted.
compdb_files() {
  "$PYTHON" - build/compile_commands.json "$PWD/" >"$1" <<'PYEOF'
import json, sys
root = sys.argv[2]
with open(sys.argv[1]) as fh:
    files = {e["file"][len(root):] for e in json.load(fh) if e["file"].startswith(root)}
sys.stdout.write("".join(f + "\n" for f in sorted(files) if f.startswith(("src/", "base/", "programs/"))))
PYEOF
}

# report_stale: a run that indexed with a stale compile database says so in its
# closing lines, with CMake's last lines; the workers read them from the output.
report_stale() {
  if [[ -f "$WORK/cmake.tail" ]]; then
    echo ">> stale compile database: cmake reconfigure failed"
    sed 's/^/>> cmake: /' "$WORK/cmake.tail"
  fi
}

# 2. changed TU set: changed .cpp plus TUs including changed headers.
if [[ "$BASE" == "--full" ]]; then
  reconfigure "full index"
  echo ">> full-index mode: whole compile_commands.json"
  cp build/compile_commands.json "$WORK/compdb.json"
  git_paths ls-files 'src/*' 'base/*' 'programs/*' >"$WORK/changed.txt"
  : >"$WORK/deleted.txt"
else
  require_base "$BASE"
  window_files "$WORK" "$BASE" "$HEAD_SHA"
  # a window that adds a translation unit or edits the build changes what the
  # compile database must list
  git_paths diff --name-only --no-renames --diff-filter=A --end-of-options "$BASE" "$HEAD_SHA" \
    >"$WORK/added.txt"
  if grep -qE '\.(cpp|cc|cxx)$' "$WORK/added.txt" \
    || grep -qE '(^|/)CMakeLists\.txt$|\.cmake$' "$WORK/changed.txt" "$WORK/deleted.txt"; then
    reconfigure "the window adds a translation unit or edits the build"
  elif [[ -f "$STALE" ]]; then
    reconfigure "the last reconfigure failed"
  fi

  grep -E '\.(cpp|cc|cxx)$' "$WORK/changed.txt" >"$WORK/tus.txt" || true
  # headers: every TU that includes them (text search on the checkout)
  # `|| true`: a window with no header changes is normal — under
  # pipefail a matchless grep otherwise kills the whole harness with a silent
  # "exit 1" whenever a merge window touches only .cpp files.
  { grep -E '\.(h|hpp|inl|ipp)$' "$WORK/changed.txt" || true; } | while read -r hdr; do
    base_hdr=$(basename "$hdr")
    git_paths grep -l -F -e "$base_hdr" -- 'src/*.cpp' 'programs/*.cpp' 'base/*.cpp' || true
  done | sort -u >>"$WORK/tus.txt"
  if [[ -f "$WORK/newly_listed.txt" ]]; then
    cat "$WORK/newly_listed.txt" >>"$WORK/tus.txt"
  fi
  sort -u "$WORK/tus.txt" -o "$WORK/tus.txt"

  N_TUS=$(wc -l <"$WORK/tus.txt")
  if ((N_TUS > MAX_TUS)); then
    echo ">> $N_TUS TUs exceed cap $MAX_TUS — degrading to changed .cpp only" >&2
    { grep -E '\.(cpp|cc|cxx)$' "$WORK/changed.txt" || true; } | sort -u >"$WORK/tus.txt"
    echo ">> skipped header-driven TUs are healed by the next --full run" >&2
  fi
  if [[ ! -s "$WORK/tus.txt" && ! -s "$WORK/deleted.txt" ]]; then
    echo ">> no C++ TUs changed — nothing to index"
    report_stale
    exit 0
  fi
  echo ">> $(wc -l <"$WORK/tus.txt") TUs to index"
  # the delta carries the window's files and every TU re-read for them: an
  # includer of a changed header gets its edges refreshed too
  sort -u "$WORK/changed.txt" "$WORK/tus.txt" -o "$WORK/changed.txt"
  # compile_commands.json carries ABSOLUTE paths; git paths are relative —
  # match on suffix (an exact match gave an empty compdb, and scip-clang
  # crashes on a <4-byte input file).
  N_COMPDB=$(
    "$PYTHON" - "$WORK/tus.txt" build/compile_commands.json "$WORK/compdb.json" <<'PYEOF'
import json, sys
want = tuple("/" + ln.strip() for ln in open(sys.argv[1]) if ln.strip())
with open(sys.argv[2]) as fh:
    keep = [e for e in json.load(fh) if want and e["file"].endswith(want)]
with open(sys.argv[3], "w") as fh:
    json.dump(keep, fh)
print(len(keep))
PYEOF
  )
  if ((N_COMPDB == 0)) && [[ ! -s "$WORK/deleted.txt" ]]; then
    echo ">> changed TUs not present in compile_commands.json — nothing to index"
    report_stale
    exit 0
  fi
  echo ">> $N_COMPDB TUs matched in compdb"
fi

# 3. scip-clang; a window whose TUs are all gone (deletions only) indexes
# nothing, and a zero-byte file is an empty Index: the delta carries the
# deletions alone
if [[ "$BASE" != "--full" ]] && ((N_COMPDB == 0)); then
  : >"$WORK/index.scip"
else
  echo ">> scip-clang (-j $JOBS)…"
  "$SCIP_CLANG" --compdb-path "$WORK/compdb.json" \
    --index-output-path "$WORK/index.scip" -j "$JOBS"
fi

# 4. convert: DELTA_OUT set = write the local JSON the in-server worker
# applies; else upload to loom over HTTP (CI / operator mode).
ARGS=(--index "$WORK/index.scip" --commit "$HEAD_SHA")
if [[ "$BASE" == "--full" ]]; then
  ARGS+=(--full)
else
  ARGS+=(--base "$BASE")
fi
ARGS+=(--changed-files "$WORK/changed.txt" --deleted-files "$WORK/deleted.txt")
if [[ -n "${DELTA_OUT:-}" ]]; then
  echo ">> converting to ${DELTA_OUT}…"
  convert_delta "${ARGS[@]}"
else
  echo ">> converting + uploading…"
  convert_delta "${ARGS[@]}" --producer "scip_harness@$(hostname -s)"
fi
report_stale
