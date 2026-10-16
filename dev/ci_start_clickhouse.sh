#!/usr/bin/env bash
# Start ClickHouse for CI with the config the bundled ClickHouse in compose.yaml
# mounts: deploy/clickhouse-config/*.xml (the loom_reader user, the NLP flags,
# an embedded Keeper) and the lemmagen3 'en' dictionary. GitHub Actions
# `services:` start BEFORE checkout and can't mount repo files, so CI runs the
# container as a post-checkout step via this script. The same image as
# compose.yaml, the latest release, so CI validates what a default install runs.
set -euo pipefail

ROOT="${GITHUB_WORKSPACE:-$(pwd)}"
CFG="$ROOT/deploy/clickhouse-config"
# A CI run passes the one release it resolved (dev/clickhouse_release.sh).
IMAGE="${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:latest}"
# A moving tag is pulled every time, so a cached image cannot hold an older build.
case "$IMAGE" in
  *:latest | *:head) PULL=always ;;
  *) PULL=missing ;;
esac

docker run -d --pull "$PULL" --name loom-ch \
  -p 8123:8123 -p 9000:9000 \
  -e CLICKHOUSE_DB=loom \
  -e CLICKHOUSE_USER=loom \
  -e CLICKHOUSE_PASSWORD=loomdev \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  --ulimit nofile=262144:262144 \
  -v "$ROOT/deploy/lemmagen3:/var/lib/clickhouse/lemmagen3:ro" \
  -v "$CFG/lemmatizers.xml:/etc/clickhouse-server/config.d/lemmatizers.xml:ro" \
  -v "$CFG/nlp_user_settings.xml:/etc/clickhouse-server/users.d/nlp_user_settings.xml:ro" \
  -v "$CFG/keeper.xml:/etc/clickhouse-server/config.d/keeper.xml:ro" \
  "$IMAGE"
# named_collections.xml is intentionally omitted: it's gitignored (built from a
# template against an operator secret) and only powers CH SQL ai* functions,
# which the test suite doesn't exercise.

echo "Waiting for ClickHouse to accept connections…"
for _ in $(seq 1 40); do
  if curl -fsS http://localhost:8123/ping >/dev/null 2>&1; then
    echo "ClickHouse is up."
    exit 0
  fi
  sleep 2
done
echo "ClickHouse did not become healthy in time; dumping container logs:" >&2
docker logs loom-ch >&2 || true
exit 1
