#!/usr/bin/env bash
# TypeScript compiler-tier pass: scip-typescript over
# a checkout with its dependencies installed, converted by scip_to_delta.py
# into loom's JSON delta.
#
# Two modes, like run_delta.sh:
#   upload mode   run_typescript_delta.sh <checkout> <loom_url> [org] [namespace]
#                 reads the scip_commit_ts watermark from loom, indexes, and
#                 uploads through code.scip_ingest (producer scip-typescript)
#   DELTA_OUT     DELTA_OUT=/path/delta.json run_typescript_delta.sh <checkout> <base|--full> <head>
#                 the in-server worker's mode: index, convert, write the JSON
#                 delta; no upload, no watermark read. Exits 0 with no file
#                 when no TypeScript file changed in the window.
#
# scip-typescript has no per-file mode — it indexes the workspace (langfuse:
# 4,603 files in ~4 min, a 384 MB index) — so both modes run the full index and
# let the converter's --changed-files bound the DELTA to the window's files.
#
# Env: LOOM_API_KEY (writer; upload mode), SCIP_TYPESCRIPT (bin; `npm i -g
#      @sourcegraph/scip-typescript@0.4.0`, node 22 or 24), PYTHON (venv python),
#      WORKSPACES (--pnpm-workspaces | --yarn-workspaces | ''; inferred),
#      SCIP_TS_PATHS (1 = run scip_ts_paths.py first — needed on an UNBUILT
#      monorepo whose packages export from dist/; default 1), NODE_HEAP_MB,
#      SCIP_TS_PROJECTS (space-separated directories, for a tree whose root is
#      not one project: a directory with a tsconfig.json is indexed through it;
#      one without gets an untracked tsconfig.scip.json that reads its JavaScript,
#      and a package.json stub when it has none, so its symbols are named
#      relative to it instead of to the filesystem root. Both are removed on exit.)
# The checkout needs node_modules (`pnpm install --frozen-lockfile
# --ignore-scripts` is enough) and `pnpm` on PATH for a pnpm workspace.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CHECKOUT=$(absolute_dir "${1:?checkout path}")
SCIP_TYPESCRIPT=${SCIP_TYPESCRIPT:-scip-typescript}
NODE_HEAP_MB=${NODE_HEAP_MB:-8192}
SCIP_TS_PATHS=${SCIP_TS_PATHS:-1}
TS_GLOBS=('*.ts' '*.tsx' '*.mts' '*.cts' '*.js' '*.jsx' '*.mjs' '*.cjs')

PROJECTS=()
if [[ -n "${SCIP_TS_PROJECTS:-}" ]]; then
  read -r -a PROJECTS <<<"$SCIP_TS_PROJECTS"
  WORKSPACES=""   # every target below has a tsconfig; inference would write one into the tree
  SCIP_TS_PATHS=0 # the dist-export rewrite is for an unbuilt workspace, not named projects
fi

if [[ -z "${WORKSPACES+x}" ]]; then
  if [[ -f "$CHECKOUT/pnpm-workspace.yaml" ]]; then
    WORKSPACES=--pnpm-workspaces
  elif grep -q '"workspaces"' "$CHECKOUT/package.json" 2>/dev/null; then
    WORKSPACES=--yarn-workspaces
  elif [[ ! -f "$CHECKOUT/tsconfig.json" ]]; then
    WORKSPACES=--infer-tsconfig
  else WORKSPACES=""; fi
fi

if [[ -n "${DELTA_OUT:-}" ]]; then
  BASE=${2:?base sha or --full}
  HEAD_SHA=${3:?head sha}
  ORG=""
  NS=""
  LOOM_BASE_URL=""
else
  LOOM_BASE_URL=${2:?loom url}
  ORG_ARG=${3:-}
  NS_ARG=${4:-}
  HEAD_SHA=$(git -C "$CHECKOUT" rev-parse HEAD)
  # one read: org, namespace, watermark — from the loom root, never the checkout
  IDENT=$(
    cd "$LOOM_ROOT" && ORG_ARG="$ORG_ARG" NS_ARG="$NS_ARG" "$PYTHON" - <<PYEOF
import asyncio, os
from server.core.ch import get_client
from server.settings import settings
async def m():
    c = get_client(); await c.connect()
    from server.code.store.index import CodeRepo
    org = os.environ.get("ORG_ARG") or settings.code_org
    ns = os.environ.get("NS_ARG") or settings.code_namespace
    wm = await CodeRepo().get_state(org, ns, "scip_commit_ts")
    print(org, ns, wm)
    await c.disconnect()
asyncio.run(m())
PYEOF
  )
  read -r ORG NS BASE <<<"$IDENT"
  BASE=${BASE:---full}
  if [[ "$BASE" == "$HEAD_SHA" ]]; then
    echo ">> typescript tier current at $HEAD_SHA — nothing to do"
    exit 0
  fi
fi

# index_workspace: scip-typescript over the checkout into $WORK/index.scip.
# Everything it writes into the tree (the tsconfig paths, a named project's
# tsconfig.scip.json and package.json stub) is undone on exit.
index_workspace() {
  local targets=() proj
  if [[ "$SCIP_TS_PATHS" == "1" ]]; then
    "$PYTHON" "$HARNESS_DIR/scip_ts_paths.py" "$CHECKOUT" >&2
    # shellcheck disable=SC2016 # expanded when the exit hook runs
    on_exit '"$PYTHON" "$HARNESS_DIR/scip_ts_paths.py" "$CHECKOUT" --restore >/dev/null 2>&1'
  fi
  for proj in "${PROJECTS[@]}"; do
    proj=${proj%/}
    [[ -d "$proj" ]] || {
      echo ">> project $proj is not in the tree — skipped" >&2
      continue
    }
    if [[ -f "$proj/tsconfig.json" ]]; then
      targets+=("$proj")
      continue
    fi
    on_exit "rm -f $(printf %q "$proj/tsconfig.scip.json")"
    cat >"$proj/tsconfig.scip.json" <<'JSONEOF'
{"compilerOptions": {"allowJs": true, "checkJs": false, "noEmit": true, "target": "es2022",
  "module": "nodenext", "moduleResolution": "nodenext", "skipLibCheck": true},
 "include": ["**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts", "**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs"],
 "exclude": ["node_modules"]}
JSONEOF
    if [[ ! -f "$proj/package.json" ]]; then
      on_exit "rm -f $(printf %q "$proj/package.json")"
      printf '{"name": "scip-%s", "version": "0.0.0", "private": true}\n' "${proj//\//-}" >"$proj/package.json"
    fi
    targets+=("$proj/tsconfig.scip.json")
  done
  local ws=()
  [[ -n "$WORKSPACES" ]] && ws=("$WORKSPACES")
  NODE_OPTIONS="--max-old-space-size=$NODE_HEAP_MB" \
    "$SCIP_TYPESCRIPT" index "${ws[@]}" "${targets[@]}" --output "$WORK/index.scip" >&2
}

cd "$CHECKOUT"
CONVERT_ARGS=(--index "$WORK/index.scip" --commit "$HEAD_SHA")
if [[ "$BASE" != "--full" ]]; then
  require_base "$BASE"
  window_files "$WORK" "$BASE" "$HEAD_SHA" "${TS_GLOBS[@]}"
  if [[ ! -s "$WORK/changed.txt" && ! -s "$WORK/deleted.txt" ]]; then
    echo ">> no TypeScript changes in $BASE..$HEAD_SHA"
    if [[ -n "${DELTA_OUT:-}" ]]; then exit 0; fi # the worker advances the watermark itself
    echo ">> advancing watermark via empty delta"
  fi
  CONVERT_ARGS+=(--base "$BASE" --changed-files "$WORK/changed.txt" --deleted-files "$WORK/deleted.txt")
else
  echo ">> no scip_commit_ts watermark — full TypeScript backfill"
  CONVERT_ARGS+=(--full)
fi

if [[ "$BASE" != "--full" && ! -s "$WORK/changed.txt" ]]; then
  # nothing to read from an index: a zero-byte file is an empty Index, and
  # the delta carries only the window's deletions
  : >"$WORK/index.scip"
else
  index_workspace
fi

# upload mode addresses the entry the watermark was read for (ORG / NS)
convert_delta "${CONVERT_ARGS[@]}" --source-root "$CHECKOUT" --producer scip-typescript
