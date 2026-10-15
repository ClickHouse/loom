#!/usr/bin/env bash
# Pull main and redeploy loom blue/green behind the host proxy.
#
# Topology: nginx (`loom-proxy`, dev/docker-compose.proxy.yml) holds host
# :7777 and forwards to the colour named in var/proxy/upstream.conf —
# `loom-blue:7781` or `loom-green:7782`, two compose projects built from
# dev/docker-compose.colour.yml that share .env, the blob and code volumes,
# and the rate-limit table. A redeploy builds and boots the IDLE colour while
# the active one keeps serving, waits until its /v1/health says
# status=ok and schema.pending=[] (boot migrations done — every migration is
# additive, so the old colour keeps serving through them), rewrites the
# upstream, reloads nginx (new workers inherit the listening socket; nothing
# is refused), then stops the old colour (SIGTERM, uvicorn drains ≤ 10 s,
# lifespan drains) and tags its image loom:prev.
#
# Rollback: `redeploy.sh --rollback` — the previous colour's container is
# stopped, not removed; it is started again (its image is the previous build),
# health-checked, and the upstream swapped back. No schema step: migrations
# are additive (docs/operations/upgrades.md).
#
# Usage:  ./redeploy.sh [--force] | --rollback | --status   (from the deploy checkout)
#   --force     rebuild even if no new commits
#   --rollback  swap the proxy back to the previous colour (starts it if stopped)
#   --status    which colour serves, which build, what is idle
#
# Preserves local docker-compose edits via git's --autostash.
# LOOM_PREFLIGHT_STRICT=0 turns ledger-only rows / checksum drift into a
# report instead of a stop (see the upgrade notes in docs/operations/upgrades.md).
# LOOM_PUBLIC_HEALTH_URL, when set, is probed last: the health URL clients use.

set -euo pipefail

REPO="${LOOM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PUBLIC_HEALTH_URL=${LOOM_PUBLIC_HEALTH_URL:-}
PROXY_PORT=7777
LOCAL_HEALTH_URL=http://localhost:$PROXY_PORT/v1/health
UPSTREAM_DIR=$REPO/var/proxy
UPSTREAM=$UPSTREAM_DIR/upstream.conf
COLOUR_COMPOSE=dev/docker-compose.colour.yml
PROXY_COMPOSE=dev/docker-compose.proxy.yml
READY_TIMEOUT_S=${LOOM_REDEPLOY_READY_TIMEOUT_S:-180}

declare -A PORT_OF=([blue]=7781 [green]=7782)

cd "$REPO"

# ---- helpers ---------------------------------------------------------------

other() { [[ "$1" == "blue" ]] && echo green || echo blue; }

active_colour() {
  # The colour upstream.conf names; empty when the proxy has never been set up.
  [[ -f "$UPSTREAM" ]] || {
    echo ""
    return
  }
  if grep -q 'loom-blue:' "$UPSTREAM"; then
    echo blue
  elif grep -q 'loom-green:' "$UPSTREAM"; then
    echo green
  else echo ""; fi
}

container_state() { docker inspect -f '{{.State.Status}}' "loom-$1" 2>/dev/null || echo absent; }
# Each colour is a web container and a workers container
# (`loom-<colour>-workers`, no published port). Its health and lease are read
# through docker exec; the lease reason names the holder.
workers_health() { docker inspect -f '{{.State.Health.Status}}' "loom-$1-workers" 2>/dev/null || echo absent; }
workers_lease() {
  docker exec "loom-$1-workers" python -c "import os, json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/v1/health?include=workers', timeout=5))['workers_leader']['reason'])" 2>/dev/null || echo unreadable
}
wait_workers() {
  # The workers container answers health without the lease (the old colour
  # holds it until it stops); healthy is enough to swap.
  local colour=$1 deadline=$((SECONDS + READY_TIMEOUT_S))
  echo "==> waiting for loom-$colour-workers (healthcheck)"
  while ((SECONDS < deadline)); do
    [[ "$(workers_health "$colour")" == "healthy" ]] && {
      echo "    loom-$colour-workers healthy; lease: $(workers_lease "$colour")"
      return 0
    }
    sleep 2
  done
  echo "!!! loom-$colour-workers did not become healthy within ${READY_TIMEOUT_S}s"
  docker logs --tail 40 "loom-$colour-workers" 2>&1 | sed 's/^/    /' || true
  return 1
}

colour_health() { curl -fsS --max-time 5 "http://127.0.0.1:${PORT_OF[$1]}/v1/health" 2>/dev/null || true; }

colour_build() { colour_health "$1" | jq -r '.build_commit // "unknown"' 2>/dev/null || echo unknown; }

write_upstream() {
  # Only the serving colour is named. `resolve` re-reads its address, so a
  # container that restarts on a new one is still reached. Write-then-rename so
  # a reload never reads a half-written file.
  mkdir -p "$UPSTREAM_DIR"
  printf 'upstream loom {\n  zone loom 64k;\n  server loom-%s:%s resolve;\n  keepalive 32;\n}\n' "$1" "${PORT_OF[$1]}" >"$UPSTREAM.tmp"
  mv -f "$UPSTREAM.tmp" "$UPSTREAM"
}

ensure_proxy() {
  # Idempotent: `up -d` on a running proxy with an unchanged config is a no-op.
  # The proxy needs an upstream file to start; a first run points it at the
  # colour we are about to bring up (`resolve` lets nginx start before it is).
  [[ -f "$UPSTREAM" ]] || write_upstream "$1"
  docker compose -p loom-proxy -f "$PROXY_COMPOSE" up -d --no-build 2>&1 | sed 's/^/    /'
}

reload_proxy() {
  # Callers run as `swap_to … || exit 1`, where `set -e` does not apply, so a
  # failed config test must stop the reload explicitly.
  docker exec loom-proxy nginx -t 2>&1 | sed 's/^/    /' || return 1
  docker exec loom-proxy nginx -s reload
}

wait_ready() {
  # status=ok AND schema.pending=[] — the boot migrations have run and the
  # colour serves the schema this build expects.
  local colour=$1 port=${PORT_OF[$1]} deadline=$((SECONDS + READY_TIMEOUT_S)) body
  echo "==> waiting for loom-$colour on 127.0.0.1:$port (status=ok, schema.pending=[])"
  while ((SECONDS < deadline)); do
    body=$(curl -fsS --max-time 3 "http://127.0.0.1:$port/v1/health" 2>/dev/null || true)
    if [[ -n "$body" ]] && echo "$body" | jq -e '.status=="ok" and .ch_ok==true and (.schema.pending|type)=="array" and (.schema.pending|length)==0' >/dev/null 2>&1; then
      echo "    ready: $(echo "$body" | jq -c '{build_commit, status, providers_degraded, embedding_dim_mismatch, schema: {pending: .schema.pending, ledger_only: .schema.ledger_only}, ratelimit: .ratelimit.backend}')"
      echo "$body" | jq -e '.providers_degraded==false and .embedding_dim_mismatch==false' >/dev/null \
        || {
          echo "!!! loom-$colour is up but a health flag is wrong — not swapping"
          return 1
        }
      return 0
    fi
    sleep 2
  done
  echo "!!! loom-$colour did not become ready within ${READY_TIMEOUT_S}s"
  docker logs --tail 60 "loom-$colour" 2>&1 | sed 's/^/    /' || true
  return 1
}

swap_to() {
  local colour=$1
  echo "==> swapping the proxy to loom-$colour"
  [[ -f "$UPSTREAM" ]] && cp -p "$UPSTREAM" "$UPSTREAM.prev"
  write_upstream "$colour"
  if ! reload_proxy; then
    # Put back the file the running nginx loaded, so its next restart loads too.
    [[ -f "$UPSTREAM.prev" ]] && mv -f "$UPSTREAM.prev" "$UPSTREAM"
    return 1
  fi
  # The reload is asynchronous for in-flight requests but immediate for new
  # ones; confirm through the proxy that the new build answers.
  local got
  for _ in $(seq 1 20); do
    got=$(curl -fsS --max-time 3 "$LOCAL_HEALTH_URL" 2>/dev/null | jq -r '.build_commit // ""' || true)
    [[ -n "$got" && "$got" == "$(colour_build "$colour")" ]] && {
      echo "    :$PROXY_PORT answers as build $got"
      return 0
    }
    sleep 0.5
  done
  echo "!!! :$PROXY_PORT does not answer as loom-$colour's build — check docker logs loom-proxy"
  return 1
}

record_deploy() {
  # var/deployed.json — the only way to answer "what is running right now?"
  local colour=$1 dirty=$2
  mkdir -p "$REPO/var"
  {
    printf '{\n'
    printf '  "deployed_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "colour": "%s",\n' "$colour"
    printf '  "previous_colour": "%s",\n' "$(other "$colour")"
    printf '  "branch": "%s",\n' "$(git rev-parse --abbrev-ref HEAD)"
    printf '  "sha": "%s",\n' "$(git rev-parse HEAD)"
    printf '  "subject": "%s",\n' "$(git log -1 --pretty=%s | sed 's/"/\\"/g')"
    printf '  "dirty": %s,\n' "$([[ -n "$dirty" ]] && echo true || echo false)"
    printf '  "uncommitted": ['
    if [[ -n "$dirty" ]]; then
      echo "$dirty" | awk '{printf "%s\"%s\"", (NR>1 ? ", " : ""), $2}'
    fi
    printf ']\n}\n'
  } >"$REPO/var/deployed.json"
  echo "==> recorded $REPO/var/deployed.json"
}

status() {
  local active
  active=$(active_colour)
  echo "proxy   : $(container_state proxy 2>/dev/null | sed 's/absent/absent (loom-proxy)/') → upstream ${active:-none}"
  for c in blue green; do
    printf '%-6s  : %-8s build %-9s port %s%s\n' "$c" "$(container_state "$c")" "$(colour_build "$c")" "${PORT_OF[$c]}" "$([[ "$c" == "$active" ]] && echo '  (serving)')"
    printf '%-6s  : workers %-8s lease: %s\n' "" "$(container_state "$c-workers")" "$(workers_lease "$c")"
  done
  echo ":$PROXY_PORT  : $(curl -fsS --max-time 3 "$LOCAL_HEALTH_URL" 2>/dev/null | jq -c '{status, build_commit, ch_ok}' || echo unreachable)"
}

rollback() {
  local active prev
  active=$(active_colour)
  [[ -n "$active" ]] || {
    echo "!!! no upstream file — nothing to roll back to"
    exit 1
  }
  prev=$(other "$active")
  echo "==> rollback: $active → $prev"
  case "$(container_state "$prev")" in
    running) ;;
    exited | created)
      echo "==> starting loom-$prev (its image is the previous build)"
      docker start "loom-$prev" >/dev/null
      ;;
    *)
      echo "!!! loom-$prev does not exist — the previous build is gone; redeploy a known-good SHA instead"
      exit 1
      ;;
  esac
  case "$(container_state "$prev-workers")" in
    running) ;;
    exited | created)
      echo "==> starting loom-$prev-workers"
      docker start "loom-$prev-workers" >/dev/null
      ;;
    *) echo "!!! no loom-$prev-workers container — the rolled-back colour runs without background workers" ;;
  esac
  wait_ready "$prev" || exit 1
  swap_to "$prev" || exit 1
  echo "==> loom-$active is still running; stop it when the rollback is confirmed: docker stop -t 45 loom-$active loom-$active-workers"
  echo "    (the workers lease moves to loom-$prev-workers within LOOM_SUPERVISOR_LEASE_RETRY_S of that stop)"
  record_deploy "$prev" ""
}

# ---- arguments ---------------------------------------------------------------

force=0
case "${1:-}" in
  --force) force=1 ;;
  --rollback)
    rollback
    exit 0
    ;;
  --status)
    status
    exit 0
    ;;
  "") ;;
  *)
    echo "usage: redeploy.sh [--force] | --rollback | --status"
    exit 2
    ;;
esac

# ---- pull ------------------------------------------------------------------

echo "==> fetching origin/main"
git fetch --quiet origin main

behind=$(git rev-list --count HEAD..origin/main)

# The colour compose builds from the WORKING TREE, so uncommitted edits ship.
# That is often intentional here, but it must never be silent.
dirty="$(git status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
  echo "==> WARNING: deploying a dirty tree — these uncommitted edits will ship:"
  echo "$dirty" | sed 's/^/    /'
  if [[ "$behind" -gt 0 ]]; then
    echo
    echo "!!! refusing to pull with a dirty tree ($behind commit(s) behind origin/main)."
    echo "    commit or stash first, then re-run."
    exit 1
  fi
fi

if [[ "$behind" -eq 0 && "$force" -eq 0 ]]; then
  echo "==> already at $(git log --oneline -1 origin/main) — nothing to deploy (use --force to rebuild anyway)"
  exit 0
fi

if [[ "$behind" -gt 0 ]]; then
  echo "==> $behind new commit(s):"
  git log --oneline HEAD..origin/main | sed 's/^/    /'
  risky=$(git diff --name-only HEAD..origin/main | grep -E '^(schemas/migrations/|dev/docker-compose|deploy/proxy/|Dockerfile$|\.env\.example$|pyproject\.toml$|requirements\.txt$|redeploy\.sh$)' || true)
  if [[ -n "$risky" ]]; then
    echo
    echo "!!! upstream touches infra files — review before continuing:"
    echo "$risky" | sed 's/^/    /'
    read -r -p "    proceed? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || {
      echo "aborted."
      exit 1
    }
  fi
  echo "==> rebasing onto origin/main (autostash preserves local compose edits)"
  # Rebase onto the ref fetched above, never `git pull`: every worktree shares
  # this .git, so a concurrent `git fetch` can rewrite FETCH_HEAD under a pull.
  # origin/main is what `behind` and the review used.
  git rebase --autostash origin/main
fi

# ---- AppArmor profile --------------------------------------------------------
# The colour compose names an AppArmor profile for the repository-job sandbox;
# Docker refuses to start a container whose profile the kernel has not loaded.
# A host check, so it runs before the preflight's slower ones.
dev/apparmor_check.sh "$COLOUR_COMPOSE" || exit 1

# ---- preflight ---------------------------------------------------------------
# Health-sha through the proxy, who holds :7777 (docker-proxy for loom-proxy,
# which answers health — fine), migrate dry-run --strict. Exit 3 = the running
# build already is HEAD.
if [[ -x dev/redeploy_preflight.sh ]]; then
  preflight_args=(--target "$(git rev-parse --short HEAD)")
  [[ "$force" -eq 1 ]] && preflight_args+=(--allow-same)
  [[ "${LOOM_PREFLIGHT_STRICT:-1}" == "0" ]] && preflight_args+=(--no-strict)
  set +e
  dev/redeploy_preflight.sh "${preflight_args[@]}"
  preflight_rc=$?
  set -e
  case "$preflight_rc" in
    0) ;;
    3)
      echo "==> the running build already is $(git rev-parse --short HEAD) — nothing to deploy (use --force)"
      exit 0
      ;;
    *)
      echo "!!! preflight failed (exit $preflight_rc) — fix or run dev/redeploy_preflight.sh --no-strict to see why"
      exit 1
      ;;
  esac
fi

# ---- pick the idle colour -----------------------------------------------------

active=$(active_colour)
if [[ -z "$active" ]]; then
  # First blue/green deploy. If the single-container deployment (loom-server
  # on :7777) is still up, the proxy cannot bind — the operator stops it in
  # the adoption step below before running this.
  if docker inspect loom-server >/dev/null 2>&1 && [[ "$(container_state server)" == "running" ]]; then
    echo "!!! the single-container deployment loom-server is running and holds :7777."
    echo "    Adopt blue/green: bring up loom-blue first, then stop loom-server and start the proxy."
    exit 1
  fi
  idle=blue
  echo "==> first blue/green deploy: no upstream file yet — deploying as $idle"
else
  idle=$(other "$active")
  echo "==> serving: loom-$active ($(colour_build "$active")) — deploying to loom-$idle"
fi
idle_port=${PORT_OF[$idle]}

# The idle colour's port must be free or held by the idle colour itself.
holder=$(ss -ltnp 2>/dev/null | awk -v p=":$idle_port" '$4 ~ p"$" {print}' || true)
if [[ -n "$holder" ]] && [[ "$(container_state "$idle")" != "running" ]]; then
  echo "!!! :$idle_port is held but loom-$idle is not running — a stray process holds the idle colour's port:"
  echo "$holder" | sed 's/^/    /'
  exit 1
fi

# ---- build + boot the idle colour ------------------------------------------

echo "==> building and starting loom-$idle (CH untouched, data preserved; the old colour keeps serving)"
LOOM_COLOUR=$idle LOOM_COLOUR_PORT=$idle_port \
  LOOM_BUILD_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  docker compose -p "loom-$idle" --env-file .env -f "$COLOUR_COMPOSE" up -d --build --force-recreate

wait_ready "$idle" || {
  echo "!!! loom-$idle is not healthy; the proxy still points at loom-${active:-nothing}. Inspect: docker logs loom-$idle"
  exit 1
}
wait_workers "$idle" || {
  echo "!!! loom-$idle-workers is not healthy; not swapping. Inspect: docker logs loom-$idle-workers"
  exit 1
}
# The new colour must run the repository-job sandbox before it takes traffic.
dev/sandbox_probe.sh "$idle" || {
  echo "    The proxy still points at loom-${active:-nothing}."
  exit 1
}

# ---- swap ------------------------------------------------------------------

ensure_proxy "$idle"
swap_to "$idle" || exit 1

# ---- retire the old colour ------------------------------------------------------

if [[ -n "$active" ]]; then
  old_image=$(docker inspect -f '{{.Image}}' "loom-$active" 2>/dev/null || true)
  if [[ -n "$old_image" ]]; then
    docker tag "$old_image" loom:prev
    echo "==> tagged the previous build's image loom:prev ($(colour_build "$active"))"
  fi
  # SIGTERM: uvicorn stops accepting, drains in-flight requests ≤ 10 s, the
  # lifespan drains run; stop_grace_period 45s leaves room.
  # The container is kept (stopped) so --rollback can start it in seconds.
  echo "==> stopping loom-$active (graceful; kept for --rollback)"
  docker stop -t 45 "loom-$active" >/dev/null
  # The old colour's workers container holds the supervisor
  # lease until it stops; the new colour's takes it within one retry.
  if [[ "$(container_state "$active-workers")" == "running" ]]; then
    echo "==> stopping loom-$active-workers (the lease moves to loom-$idle-workers)"
    docker stop -t 45 "loom-$active-workers" >/dev/null
    for _ in $(seq 1 10); do
      lease=$(workers_lease "$idle")
      [[ "$lease" == holds* ]] && break
      sleep 2
    done
    echo "    loom-$idle-workers lease: ${lease:-unreadable}"
  fi
fi

# ---- summary ----------------------------------------------------------------

summary=$(curl -fsS --max-time 5 "$LOCAL_HEALTH_URL")
echo "==> health via :$PROXY_PORT: $(echo "$summary" | jq -c '{status, build_commit, ch_ok, providers_degraded, fake_embedding, fake_llm, embedding_dim_mismatch, ratelimit: .ratelimit.backend}')"
echo "$summary" | jq -e '.status=="ok" and .ch_ok==true and .providers_degraded==false and .embedding_dim_mismatch==false' >/dev/null \
  || {
    echo "!!! health flags wrong — investigate (redeploy.sh --rollback swaps back)"
    exit 1
  }
if [[ -n "$PUBLIC_HEALTH_URL" ]]; then
  echo "==> public URL: $PUBLIC_HEALTH_URL → $(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$PUBLIC_HEALTH_URL")"
fi
echo "==> now at $(git log --oneline -1) on loom-$idle"
echo "==> post-check in ~30 min: cd $REPO && .venv/bin/python -m server.cli deploy-delta   # reads var/deployed.json"
record_deploy "$idle" "$dirty"
