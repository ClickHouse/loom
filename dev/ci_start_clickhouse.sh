#!/usr/bin/env bash
# Start ClickHouse for CI with the SAME config the dev/OSS stack uses:
# deploy/clickhouse-config/*.xml + the lemmagen3 'en' dictionary. Without these,
# the content_excerpt_{stem,lemma} MATERIALIZED columns can't evaluate
# lemmatize()/stem() (experimental, gated) and lightweight UPDATEs fail on
# ClickHouse — so every insert into memory_embeddings dies and search returns
# nothing. GitHub Actions `services:` start BEFORE checkout and can't mount
# repo files, so CI runs the container as a post-checkout step via this script.
#
# Pinned to the same image as dev/docker-compose.dev.yml so CI is reproducible
# and matches what we validate against locally.
set -euo pipefail

ROOT="${GITHUB_WORKSPACE:-$(pwd)}"
CFG="$ROOT/deploy/clickhouse-config"

docker run -d --pull always --name loom-ch \
  -p 8123:8123 -p 9000:9000 \
  -e CLICKHOUSE_DB=loom \
  -e CLICKHOUSE_USER=loom \
  -e CLICKHOUSE_PASSWORD=loomdev \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  --ulimit nofile=262144:262144 \
  -v "$ROOT/deploy/lemmagen3:/var/lib/clickhouse/lemmagen3:ro" \
  -v "$CFG/lemmatizers.xml:/etc/clickhouse-server/config.d/lemmatizers.xml:ro" \
  -v "$CFG/nlp_user_settings.xml:/etc/clickhouse-server/users.d/nlp_user_settings.xml:ro" \
  -v "$CFG/merge_tree_settings.xml:/etc/clickhouse-server/config.d/merge_tree_settings.xml:ro" \
  -v "$CFG/keeper.xml:/etc/clickhouse-server/config.d/keeper.xml:ro" \
  clickhouse/clickhouse-server:latest
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
