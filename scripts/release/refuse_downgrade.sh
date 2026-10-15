#!/usr/bin/env bash
# Fail when <image> is an older ClickHouse release than the bundle of one of
# the 30 newest published releases (prereleases included) names: an install
# upgrading to it would start an older server on data a newer one wrote.
# ALLOW_DOWNGRADE=true skips the check.
#
#   scripts/release/refuse_downgrade.sh clickhouse/clickhouse-server:A.B.C.D
set -euo pipefail

image=$1
if [ "${ALLOW_DOWNGRADE:-}" = true ]; then
  exit 0
fi
here=$(dirname "$0")
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT

status=0
bash "$here/published_bundle.sh" recent "$dir" || status=$?
case $status in
  0) python3 "$here/build_stack_bundle.py" not-older "$image" "$dir"/*/loom-stack.tar.gz ;;
  3) ;;
  *) exit "$status" ;;
esac
