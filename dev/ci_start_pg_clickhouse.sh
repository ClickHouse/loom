#!/usr/bin/env bash
# Start the pg_clickhouse image: stock Postgres with the pg_clickhouse extension,
# standing in for an org's own Postgres that reads Loom's SQL surface
# (docs/guides/query-from-postgres.md). It runs on the host network so it
# reaches the ClickHouse that dev/ci_start_clickhouse.sh published on
# 127.0.0.1, and listens on 127.0.0.1 only.
#
# LOOM_PGCH_POSTGRES picks the Postgres major (16 or 18), LOOM_PGCH_VERSION the
# extension release; LOOM_PGCH_CONTAINER and LOOM_PGCH_PORT a second one beside
# a running one.
set -euo pipefail

POSTGRES="${LOOM_PGCH_POSTGRES:-16}"
VERSION="${LOOM_PGCH_VERSION:-0.11.0}"
NAME="${LOOM_PGCH_CONTAINER:-loom-pgch}"
PORT="${LOOM_PGCH_PORT:-5433}"
IMAGE="ghcr.io/clickhouse/pg_clickhouse:$POSTGRES-$VERSION"

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "port $PORT is already in use; set LOOM_PGCH_PORT" >&2
  exit 1
fi

docker run -d --name "$NAME" --network host \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=appdev -e POSTGRES_DB=app \
  "$IMAGE" -c "port=$PORT" -c listen_addresses=127.0.0.1 >/dev/null

echo "Waiting for $IMAGE…"
for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U app -d app -h 127.0.0.1 -p "$PORT" >/dev/null 2>&1 && exit 0
  sleep 1
done
echo "pg_clickhouse never became ready" >&2
docker logs --tail 50 "$NAME" >&2
exit 1
