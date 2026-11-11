#!/usr/bin/env bash
# Python compiler-tier pass: scip-python over the checkout, converted +
# uploaded through the same code.scip_ingest path as the C++ tier.
#
# Unlike scip-clang there is no per-TU delta mode — scip-python re-indexes
# the whole python surface (~2min on the ClickHouse corpus) — so this pass
# runs the full index and lets the converter's --changed-files filter bound
# the UPLOAD to the window's changed .py files. Watermark: scip_commit_py
# (producer-scoped on apply; never touches the C++ scip_commit).
#
# Usage: run_python_delta.sh <checkout> <loom_url>
# Env: LOOM_API_KEY (writer), SCIP_PYTHON (bin), PYTHON (venv python),
#      SCIP_ENV_JSON (environment file; default: empty-env sidecar — the
#      pip-enumeration path can crash, see sourcegraph/scip-python
#      issues #151/#157)
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# as the workers do: no repack inside a fetch of the checkout
NO_GC=(-c gc.auto=0 -c maintenance.auto=false)
# absolute before any cd: the indexing step runs inside the checkout
PY_STUBS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/python_stubs

CHECKOUT=$(absolute_dir "${1:?checkout path}")
# DELTA_OUT=/path/delta.json run_python_delta.sh <checkout> <base|--full> <head>
# indexes and writes the JSON delta for the code_scip_py_delta worker — no
# upload, no watermark read (the worker owns both). Without DELTA_OUT the
# script uploads: run_python_delta.sh <checkout> <loom_url> [org] [ns].
if [[ -n "${DELTA_OUT:-}" ]]; then
  LOOM_BASE_URL=""
else
  LOOM_BASE_URL=${2:?loom url}
fi
# Optional: address one configured repo entry (multi-repo). Defaults to the
# scalar LOOM_CODE_ORG / LOOM_CODE_NAMESPACE.
ORG_ARG=${3:-}
NS_ARG=${4:-}
SCIP_PYTHON=${SCIP_PYTHON:-scip-python}
# The node heap scip-python runs under (MB). The default 8 GB
# was not enough for loom's 2,074 files ("Ineffective mark-compacts near heap
# limit", SIGABRT after the walk, an empty index uploaded as a 0-file delta).
SCIP_PYTHON_HEAP_MB=${SCIP_PYTHON_HEAP_MB:-16384}
if [[ -n "${DELTA_OUT:-}" ]]; then
  BASE=${2:?base sha or --full}
  [[ "$BASE" == "--full" ]] && BASE=""
  HEAD_SHA=${3:?head sha}
  ORG=""
  NS=""
else
  # watermark read needs the server package + .env — run from the loom root,
  # NOT the checkout (a cwd of the indexed repo can import neither)
  # one read: org, namespace, watermark (org/namespace MUST reach the
  # converter — a namespace-scoped writer token 403s on a body without
  # them; the transport then checks GLOBAL roles), the fast tier's git_head
  # (the commit this pass indexes — the checkout's own HEAD only says where
  # the last pass left it), the entry's bare clone, and who may move the
  # checkout: this pass (run under `loom code entry-lock`, the same lock a
  # full C++ re-index holds), the in-process code_scip_py_delta worker, or the go/typescript
  # worker whose checkout it is. "-" stands for an empty field so `read`
  # keeps the columns aligned.
  IDENT=$(
    cd "$LOOM_ROOT" && ORG_ARG="$ORG_ARG" NS_ARG="$NS_ARG" "$PYTHON" - <<PYEOF
import asyncio, os
from server.core.ch import get_client
from server.settings import settings
async def m():
    c = get_client(); await c.connect()
    from server.code.store.index import CodeRepo
    from server.code.app.index.producer_inference import worker_owns
    from server.code.domain.index.repos import configured_repos
    from server.code.app.index.repo_registry import read_registry_or_exit
    await read_registry_or_exit("run_python_delta")
    repo = CodeRepo()
    org = os.environ.get("ORG_ARG") or settings.code_org
    ns = os.environ.get("NS_ARG") or settings.code_namespace
    wm = await repo.get_state(org, ns, "scip_commit_py")
    head = await repo.get_state(org, ns, "git_head")
    cfg = next((r for r in configured_repos() if r.org == org and r.namespace == ns), None)
    bare = os.path.abspath(cfg.git_dir or settings.code_git_dir) if cfg else ""
    producer = cfg.scip_producer if cfg else ""
    if cfg and settings.code_scip_py_delta_enabled and await worker_owns(repo, cfg, "python"):
        owner = "worker"
    elif producer in ("go", "typescript"):
        owner = producer  # its in-process worker moves this checkout
    elif cfg and not producer and not await worker_owns(repo, cfg, "python"):
        owner = "none"  # the tree calls for no Python tier
    else:
        owner = "manual"
    print(org, ns, wm or "-", head or "-", bare or "-", owner)
    await c.disconnect()
asyncio.run(m())
PYEOF
  )
  read -r ORG NS BASE HEAD_SHA BARE OWNER <<<"$IDENT"
  [[ "$BASE" == "-" ]] && BASE=""
  if [[ "$OWNER" == "worker" ]]; then
    echo ">> $NS: the code_scip_py_delta worker owns the python tier — skipping"
    exit 0
  fi
  if [[ "$OWNER" == "none" ]]; then
    echo ">> $NS: the tree has no Python build file — no python tier"
    exit 0
  fi
  if [[ "$OWNER" != "manual" ]]; then
    echo ">> $NS: the checkout belongs to the $OWNER worker — skipping the python tier"
    exit 0
  fi
  if [[ "$HEAD_SHA" == "-" ]]; then
    echo ">> $NS: git_head not seeded — nothing to index yet"
    exit 0
  fi
  if [[ "$BASE" == "$HEAD_SHA" ]]; then
    echo ">> python tier current at $HEAD_SHA — nothing to do"
    exit 0
  fi
  # Bring the checkout to git_head. The bare clone is the fetch source the
  # worker uses; when this host user cannot read it (it belongs to the
  # server's uid), the checkout's own origin serves the same commits.
  if ! git -C "$CHECKOUT" cat-file -e "$HEAD_SHA^{commit}" 2>/dev/null; then
    SOURCE=origin
    if [[ "$BARE" != "-" ]] && git -C "$BARE" rev-parse --git-dir >/dev/null 2>&1; then
      SOURCE=$BARE
    fi
    # a remote that refuses a bare-SHA want still serves it through its branches
    git -C "$CHECKOUT" "${NO_GC[@]}" fetch --quiet --recurse-submodules=no "$SOURCE" "$HEAD_SHA" \
      || git -C "$CHECKOUT" "${NO_GC[@]}" fetch --quiet --recurse-submodules=no "$SOURCE"
  fi
  git -C "$CHECKOUT" "${NO_GC[@]}" checkout --quiet --force "$HEAD_SHA"
fi

cd "$CHECKOUT"

if [[ -n "$BASE" ]]; then
  require_base "$BASE"
  window_files "$WORK" "$BASE" "$HEAD_SHA" '*.py'
  if [[ ! -s "$WORK/changed.txt" && ! -s "$WORK/deleted.txt" ]]; then
    echo ">> no python changes in $BASE..$HEAD_SHA — advancing watermark via empty delta"
    if [[ -n "${DELTA_OUT:-}" ]]; then exit 0; fi # the worker advances the watermark itself
  fi
  CHANGED_ARGS=(--changed-files "$WORK/changed.txt" --deleted-files "$WORK/deleted.txt")
else
  echo ">> no scip_commit_py watermark — full python backfill"
  CHANGED_ARGS=()
fi

ENV_JSON=${SCIP_ENV_JSON:-$WORK/env.json}
[[ -f "$ENV_JSON" ]] || echo '[]' >"$ENV_JSON"

# Scope pyright to the repo's own python: a submodule-bearing checkout
# carries ~8K vendored python files under contrib/, which quadruples the
# workspace and OOM-kills node (observed: SIGABRT at 10,348 project
# files vs 2,621 without submodules). Written only when the operator
# hasn't provided one; untracked, so the cpp harness's checkout --force
# leaves it alone.
if [[ ! -f pyrightconfig.json ]]; then
  cat >pyrightconfig.json <<'JSONEOF'
{"exclude": ["contrib", "build*", ".git", "rust", "base/glibc-compatibility"]}
JSONEOF
fi

if [[ -n "$BASE" && ! -s "$WORK/changed.txt" ]]; then
  # nothing to read from an index: a zero-byte file is an empty Index, and
  # the delta carries only the window's deletions
  : >"$WORK/index.scip"
else
  # python_stubs/: packages imported by wildcard that no environment installs
  # (pyright asserts on an unresolved `from x import *`; see its README)
  PYTHONPATH="$PY_STUBS${PYTHONPATH:+:$PYTHONPATH}" \
    NODE_OPTIONS="--max-old-space-size=${SCIP_PYTHON_HEAP_MB}" \
    "$SCIP_PYTHON" index . --project-name clickhouse-py \
    --environment "$ENV_JSON" --output "$WORK/index.scip" >&2
fi

# upload mode addresses the entry the watermark was read for (ORG / NS)
convert_delta --index "$WORK/index.scip" --commit "$HEAD_SHA" ${BASE:+--base "$BASE"} \
  "${CHANGED_ARGS[@]}" --source-root "$CHECKOUT" --producer scip-python
