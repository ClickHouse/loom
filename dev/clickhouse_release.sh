#!/usr/bin/env bash
# Print the exact image of the release clickhouse/clickhouse-server:latest names
# now, e.g. clickhouse/clickhouse-server:26.9.18.11, read from the registry
# without pulling. A CI run and a release bundle take one release this way, so
# `latest` moving mid-run splits nothing.
set -euo pipefail

version=$(docker buildx imagetools inspect clickhouse/clickhouse-server:latest \
  --format '{{ index (index .Image "linux/amd64").Config.Labels "com.clickhouse.build.version" }}')
if ! [[ $version =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "clickhouse/clickhouse-server:latest names no release version: '$version'" >&2
  exit 1
fi
echo "clickhouse/clickhouse-server:$version"
