#!/usr/bin/env bash
# Print the exact image of the release clickhouse/clickhouse-server:latest names
# now, e.g. clickhouse/clickhouse-server:26.9.18.11. A CI run and a release
# bundle take one release this way, so `latest` moving mid-run splits nothing.
set -euo pipefail

docker pull -q clickhouse/clickhouse-server:latest >/dev/null
version=$(docker run --rm --entrypoint clickhouse clickhouse/clickhouse-server:latest \
  local --query "SELECT version()")
echo "clickhouse/clickhouse-server:$version"
