#!/usr/bin/env bash
# Start a stock Postgres primary with one synchronous standby, the smallest
# self-hosted layout whose acknowledged commits survive the primary's loss.
# The primary runs `wal_level = logical` (what an archive or mirror decoder
# needs) and `synchronous_standby_names` naming the standby, so a commit
# returns only once the standby has flushed it.
#
# A second pair beside a running one (one per worktree) takes its own names
# and host port: LOOM_PG_CONTAINER (the standby is "<name>-standby"),
# LOOM_PG_HOST_PORT. LOOM_PG_IMAGE picks the image (default postgres:16).
# LOOM_PG_STANDBY=0 starts the primary alone, with no synchronous standby.
set -euo pipefail

IMAGE="${LOOM_PG_IMAGE:-postgres:16}"
NAME="${LOOM_PG_CONTAINER:-loom-pg}"
PORT="${LOOM_PG_HOST_PORT:-5432}"
STANDBY="${LOOM_PG_STANDBY:-1}"
NET="$NAME-net"

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "port $PORT is already in use; set LOOM_PG_HOST_PORT" >&2
  exit 1
fi

# A rerun after the containers were removed finds the network still there.
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
docker run -d --name "$NAME" --network "$NET" --network-alias primary \
  -p "127.0.0.1:$PORT:5432" \
  -e POSTGRES_USER=loom -e POSTGRES_PASSWORD=loomdev -e POSTGRES_DB=loom \
  "$IMAGE" \
  -c wal_level=logical \
  -c max_wal_senders=10 \
  -c max_replication_slots=10 \
  -c synchronous_commit=on >/dev/null

# The image's init runs a socket-only server first; TCP answers once it is done.
echo "Waiting for the primary…"
for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U loom -d loom -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -U loom -d loom -h 127.0.0.1 >/dev/null

if [ "$STANDBY" = 1 ]; then
  docker exec "$NAME" psql -U loom -d loom -qc \
    "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator'"
  docker exec "$NAME" bash -c \
    "echo 'host replication replicator all scram-sha-256' >> \"\$PGDATA/pg_hba.conf\""
  # Set after init: a commit made while it names a standby that is not yet
  # attached waits for it.
  docker exec "$NAME" psql -U loom -d loom -qc \
    "ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (loom_standby)'"
  docker exec "$NAME" psql -U loom -d loom -qc "SELECT pg_reload_conf()" >/dev/null
  # The standby clones the primary and streams from it under the name the
  # primary waits for.
  docker run -d --name "$NAME-standby" --network "$NET" --user postgres \
    -e PGPASSWORD=replicator \
    --entrypoint bash "$IMAGE" -c '
      set -e
      # The image says where its data lives (it moved in Postgres 18).
      export PGDATA="${PGDATA:-/var/lib/postgresql/data}"
      rm -rf "$PGDATA"/* 2>/dev/null || true
      until pg_basebackup -h primary -U replicator -D "$PGDATA" -R -X stream; do sleep 1; done
      echo "primary_conninfo = '"'"'host=primary user=replicator password=replicator application_name=loom_standby'"'"'" >> "$PGDATA/postgresql.auto.conf"
      chmod 700 "$PGDATA"
      exec postgres -D "$PGDATA"
    ' >/dev/null
  echo "Waiting for the synchronous standby…"
  for _ in $(seq 1 90); do
    state=$(docker exec "$NAME" psql -U loom -d loom -Atc \
      "SELECT sync_state FROM pg_stat_replication WHERE application_name = 'loom_standby'" 2>/dev/null || true)
    if [ "$state" = sync ]; then
      echo "Postgres is up: primary on :$PORT, standby synchronous."
      exit 0
    fi
    sleep 1
  done
  echo "The standby did not become synchronous; logs:" >&2
  docker logs "$NAME-standby" >&2 || true
  exit 1
fi
echo "Postgres is up: primary on :$PORT, no standby."
