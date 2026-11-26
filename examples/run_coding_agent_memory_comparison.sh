#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.venv/bin:$PATH" GITHUB_WORKSPACE="$PWD"
# A caller may be in a Git hook or have an operator deployment configured.
while IFS= read -r variable; do
  case "$variable" in
    GIT_* | LOOM_* | UVICORN_*) unset "$variable" ;;
  esac
done < <(compgen -e)
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 24 ? 1 : 0)' || {
  echo 'Use Node.js 24 or later for the reference MCP server.' >&2
  exit 1
}
test -x .venv/bin/loom || {
  echo 'Run uv sync --locked --python 3.12 --extra dev --extra code first.' >&2
  exit 1
}

scratch=$(mktemp -d "${TMPDIR:-/tmp}/loom-memory-comparison.XXXXXXXX")
container="ch-$(basename "$scratch")"
export LOOM_CH_HOST_PORT=${COMPARISON_CH_HTTP_PORT:-19351}
export LOOM_CH_NATIVE_HOST_PORT=${COMPARISON_CH_NATIVE_PORT:-19951}
api_port=${COMPARISON_API_PORT:-19751}
server_pid=''
started_db=''
cleanup() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  if [ -n "$started_db" ]; then docker rm -fv "$container" >/dev/null 2>&1 || true; fi
  rm -rf "$scratch"
}
trap cleanup EXIT
for port in "$LOOM_CH_HOST_PORT" "$LOOM_CH_NATIVE_HOST_PORT" "$api_port"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "Port $port is in use. Set the COMPARISON port variables to free ports." >&2
    exit 1
  fi
done

export LOOM_ENV_FILE='' LOOM_CH_CONTAINER="$container" LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:26.11.2.4
export LOOM_CH_HOST=127.0.0.1 LOOM_CH_PORT="$LOOM_CH_HOST_PORT" LOOM_CH_DATABASE=loom LOOM_CH_SECURE=false
export LOOM_CH_USER=loom LOOM_CH_READER_USER=loom_reader LOOM_CH_PASSWORD=loomdev LOOM_STORAGE_AUTHORITY=clickhouse
export LOOM_BASE_URL="http://127.0.0.1:$api_port" LOOM_INSTANCE_LABEL="iso:coding-agent-comparison:${scratch##*.}"
export LOOM_CODE_INGEST_ENABLED=true LOOM_GIT_HISTORY=local LOOM_CODE_REPOS_ROOT="$scratch/mirrors"
export LOOM_AUTH_REQUIRED=true LOOM_EMBEDDING_MODEL=fake-ngram-1024 LOOM_LLM_PROVIDER=fake
export LOOM_BACKGROUND_WORKERS=off LOOM_ADOPTION_TELEMETRY_ENABLED=false LOOM_MEMORY_CODE_LINKS_SETTLE_SECONDS=10
LOOM_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
export LOOM_JWT_SECRET

mkdir "$scratch/deps"
cp examples/coding-agent-memory-comparison/{package.json,package-lock.json} "$scratch/deps/"
npm ci --prefix "$scratch/deps" --ignore-scripts --no-audit --no-fund >&2
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost"
export no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost"
started_db=1
bash dev/ci_start_clickhouse.sh >&2
python -m server.core.schema.migrate >"$scratch/migrate.log" 2>&1 || {
  cat "$scratch/migrate.log" >&2
  exit 1
}
LOOM_BUILD_COMMIT=$(git rev-parse HEAD) uvicorn server.main:app --app-dir "$PWD" --host 127.0.0.1 --port "$api_port" --loop asyncio >"$scratch/server.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 45); do
  if curl -fsS --max-time 2 "$LOOM_BASE_URL/v1/health" 2>/dev/null | jq -e --arg label "$LOOM_INSTANCE_LABEL" '.instance_label == $label and .status == "ok"' >/dev/null; then break; fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "$scratch/server.log" >&2
    exit 1
  fi
  sleep 1
done
python -m examples.compare_coding_agent_memory --graph-memory \
  "$scratch/deps/node_modules/@modelcontextprotocol/server-memory/dist/index.js"
