#!/usr/bin/env bash
# The previous release's installer files, built from its own tag: its
# install.sh, and loom-stack.tar.gz with its checksum from that tag's builder.
# The installer rehearsal installs them first and upgrades over them.
#
#   previous_release_files.sh VERSION CLICKHOUSE_IMAGE OUT_DIR
#
# The previous release is the highest final v* tag below VERSION, or VERSION
# itself when it is a final tagged on another commit: on main, between
# releases, VERSION is the release that is out. It prints the version it built;
# with none, nothing is written and it prints nothing. The bundle is rebuilt
# from that tag with CLICKHOUSE_IMAGE, so the upgrade checks the installer's
# files, settings and volumes, not a ClickHouse upgrade. Needs the repository's
# tags (fetch-depth: 0, or `git fetch --tags`), and fails without any.
set -euo pipefail

version=${1:?version} clickhouse_image=${2:?clickhouse image} out=${3:?output directory}
# No tags at all is a checkout without them, not a repository without releases.
[ -n "$(git tag --list 'v*')" ] || {
  echo "no v* tags: fetch them (fetch-depth: 0)" >&2
  exit 1
}
# A build after the release (main, between releases): over that release itself.
if [[ $version != *-* ]] && [ -n "$(git tag --list "v$version")" ] \
  && [ -z "$(git tag --points-at HEAD --list "v$version")" ]; then
  previous=$version
else
  # The highest final X.Y.Z below VERSION's release (a prerelease's own final excluded).
  previous=$(git tag --list 'v*' | sed -n 's/^v\([0-9]*\.[0-9]*\.[0-9]*\)$/\1/p' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | awk -F. -v v="${version%%-*}" 'BEGIN { split(v, w, ".") }
        $1 < w[1] || ($1 == w[1] && $2 < w[2]) || ($1 == w[1] && $2 == w[2] && $3 < w[3]) { p = $0 }
        END { print p }')
fi
[ -n "$previous" ] || exit 0

source=$(mktemp -d)
trap 'rm -rf "$source"' EXIT
git archive "v$previous" | tar -x -C "$source"
mkdir -p "$out"
python3 "$source/scripts/release/build_stack_bundle.py" build --version "$previous" \
  --clickhouse-image "$clickhouse_image" --output-dir "$out" >&2
# A release from before the builder wrote install.sh shipped the tag's own.
[ -f "$out/install.sh" ] || cp "$source/install.sh" "$out/install.sh"
echo "$previous"
