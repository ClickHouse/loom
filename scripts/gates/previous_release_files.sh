#!/usr/bin/env bash
# A release's installer files, built from its own tag: its install.sh, and
# loom-stack.tar.gz with its checksum from that tag's builder. The installer
# rehearsal (which picks the release) installs them first and upgrades over them.
#
#   previous_release_files.sh VERSION CLICKHOUSE_IMAGE OUT_DIR
#
# The bundle is rebuilt with CLICKHOUSE_IMAGE, so the upgrade checks the
# installer's files, settings and volumes, not a ClickHouse upgrade.
set -euo pipefail

version=${1:?version} clickhouse_image=${2:?clickhouse image} out=${3:?output directory}
source=$(mktemp -d)
trap 'rm -rf "$source"' EXIT
git archive "v$version" | tar -x -C "$source"
mkdir -p "$out"
python3 "$source/scripts/release/build_stack_bundle.py" build --version "$version" \
  --clickhouse-image "$clickhouse_image" --output-dir "$out" >&2
# A release from before the builder wrote install.sh shipped the tag's own.
[ -f "$out/install.sh" ] || cp "$source/install.sh" "$out/install.sh"
