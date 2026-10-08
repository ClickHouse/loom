#!/usr/bin/env bash
# Redeploy preflight: the three checks an operator wants
# BEFORE `make dev-cloud` replaces the running container.
#
#   1. health-sha  — GET /v1/health, compare `build_commit` with the target
#                    SHA. Already there → exit 3 ("skip"); redeploy.sh treats
#                    that as nothing-to-deploy unless --force, so a second
#                    redeploy of the same build changes nothing.
#   2. port        — who listens on the loom port. Nothing (first deploy) or
#                    the listener that answered /v1/health in step 1 are fine,
#                    whatever the process is called. A held port whose health
#                    does not answer is a stray process (an isolated uvicorn
#                    started on :7777 by hand): the new container could not
#                    bind, so the preflight stops.
#   3. migrate     — `loom migrate --dry-run --strict`: pending migrations with
#                    their SHA-256, ledger-only rows (applied under a filename
#                    the repo no longer has) and checksum drift. Pending is
#                    normal before a deploy; ledger-only / drift stop it under
#                    --strict (default here) so the operator prunes or
#                    re-records first — see "Migrations pending or checksum drift" in docs/operations/troubleshooting.md.
#
# Usage: dev/redeploy_preflight.sh [--target <sha>] [--allow-same] [--no-strict]
# Exit:  0 go · 1 stop (a check failed) · 3 skip (running build == target)
#
# Overrides (tests, other hosts):
#   LOOM_PREFLIGHT_HEALTH_URL  default http://localhost:7777/v1/health
#   LOOM_PREFLIGHT_PORT        default 7777
#   LOOM_PREFLIGHT_MIGRATE     command run for step 3 (default: python -m server.core.schema.migrate --dry-run)
#   PYTHON                     interpreter for the migrate step (default .venv/bin/python, else python3)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTH_URL="${LOOM_PREFLIGHT_HEALTH_URL:-http://localhost:7777/v1/health}"
PORT="${LOOM_PREFLIGHT_PORT:-7777}"
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$REPO/.venv/bin/python" ]]; then PYTHON="$REPO/.venv/bin/python"; else PYTHON=python3; fi
fi

target=""
allow_same=0
strict=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      target="$2"
      shift 2
      ;;
    --allow-same)
      allow_same=1
      shift
      ;;
    --no-strict)
      strict=0
      shift
      ;;
    -h | --help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
if [[ -z "$target" ]]; then
  target="$(git -C "$REPO" rev-parse --short origin/main 2>/dev/null || git -C "$REPO" rev-parse --short HEAD)"
fi

rc=0

# ---- 1. health-sha ---------------------------------------------------------
echo "==> preflight 1/3: running build vs target $target"
health="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
running="unreachable"
if [[ -n "$health" ]]; then
  running="$(printf '%s' "$health" | sed -n 's/.*"build_commit": *"\([^"]*\)".*/\1/p')"
  running="${running:-unknown}"
fi
echo "    running: $running"
if [[ "$running" != "unreachable" && "$running" != "unknown" ]]; then
  if [[ "$running" == "$target"* || "$target" == "$running"* ]]; then
    if [[ "$allow_same" -eq 1 ]]; then
      echo "    build already at target — continuing (--allow-same)"
    else
      echo "    build already at target — nothing to deploy (exit 3; pass --allow-same to rebuild)"
      exit 3
    fi
  fi
fi

# ---- 2. port ---------------------------------------------------------------
echo "==> preflight 2/3: who listens on :$PORT"
if command -v ss >/dev/null 2>&1; then
  listeners="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print}' || true)"
elif command -v lsof >/dev/null 2>&1; then
  # macOS has lsof but not iproute2's ss. -nP also avoids DNS/service-name
  # lookups, so the preflight stays bounded and the port remains numeric.
  listeners="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
else
  echo "!!! cannot inspect :$PORT: install iproute2 (ss) or lsof" >&2
  listeners="__inspection_unavailable__"
  rc=1
fi
if [[ -z "$listeners" ]]; then
  echo "    nobody — first deploy or the server is down"
  if [[ "$running" != "unreachable" ]]; then
    echo "    (health answered though — a proxy or another host is fronting $HEALTH_URL)"
  fi
else
  echo "$listeners" | sed 's/^/    /'
  # The judgement is about health, not the process name: a listener that
  # answered /v1/health in step 1 is the build we are replacing, whether ss
  # shows it as docker-proxy, uvicorn, python or (in a test) pytest. A held
  # port that does not answer is the stray process compose could not evict.
  if [[ "$running" == "unreachable" ]]; then
    echo "!!! :$PORT is held but /v1/health does not answer — a stray process holds the port"
    rc=1
  else
    echo "    answered /v1/health as build $running — the listener is the build being replaced"
  fi
fi

# ---- 3. migrate dry-run ----------------------------------------------------
echo "==> preflight 3/3: schema ledger vs repo (dry-run, nothing applied)"
migrate_cmd="${LOOM_PREFLIGHT_MIGRATE:-$PYTHON -m server.core.schema.migrate --dry-run}"
if [[ "$strict" -eq 1 ]]; then migrate_cmd="$migrate_cmd --strict"; fi
if (cd "$REPO" && eval "$migrate_cmd") 2>&1 | sed 's/^/  /'; then
  :
else
  echo "!!! migrate dry-run failed or found ledger-only / drifted entries (see above; docs/operations/troubleshooting.md, "Migrations pending or checksum drift")"
  rc=1
fi

# ---- 3b. migrations only on the target -------------------------------------
# The dry-run above reads THIS checkout's schemas/migrations — the build being
# replaced — so a migration that exists only on the target is invisible to it
# and reads as "0 pending". List
# what the target adds, so the operator knows what the new build's boot runs.
if [[ -n "$target" ]] && git -C "$REPO" rev-parse --verify -q "${target}^{commit}" >/dev/null 2>&1; then
  have=$(ls "$REPO/schemas/migrations" 2>/dev/null | grep '\.sql$' | sort)
  want=$(git -C "$REPO" ls-tree --name-only "$target" -- schemas/migrations/ | sed 's|.*/||' | grep '\.sql$' | sort)
  added=$(comm -13 <(printf '%s\n' "$have") <(printf '%s\n' "$want") | grep . || true)
  if [[ -n "$added" ]]; then
    n=$(printf '%s\n' "$added" | grep -c .)
    echo "    target adds $n migration file(s) this checkout lacks — the new build's boot applies them:"
    printf '%s\n' "$added" | sed 's/^/      /'
  else
    echo "    target adds no migration file this checkout lacks"
  fi
fi

if [[ "$rc" -eq 0 ]]; then echo "==> preflight OK"; else echo "==> preflight STOP"; fi
exit "$rc"
