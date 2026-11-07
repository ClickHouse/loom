#!/usr/bin/env bash
# Print the ClickHouse release the bundle of <version> names:
#   - the one its published bundle already names, so a re-run never moves an
#     installed version's ClickHouse (CLICKHOUSE_IMAGE is then ignored, with a
#     warning);
#   - else CLICKHOUSE_IMAGE, when the dispatch names one;
#   - else, for a final, the one its newest candidate on this commit with a
#     published bundle was soaked with;
#   - else the release clickhouse/clickhouse-server:latest names now.
# Every pin but this version's own published bundle's is checked for a downgrade
# (refuse_downgrade.sh). Run with the repository's tags fetched.
#
#   scripts/release/release_clickhouse.sh <version>
set -euo pipefail

version=$1
here=$(dirname "$0")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if [ -n "${CLICKHOUSE_IMAGE:-}" ]; then
  python3 - "$here" "$CLICKHOUSE_IMAGE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from build_stack_bundle import EXACT_CLICKHOUSE
if not EXACT_CLICKHOUSE.fullmatch(sys.argv[2]):
    sys.exit(f"::error::not one ClickHouse release: {sys.argv[2]!r}")
PY
fi

# The release v$1's published bundle names; returns 3 when it has none.
published() {
  mkdir -p "$work/$1"
  bash "$here/published_bundle.sh" "$1" "$work/$1" >&2 || return
  python3 "$here/build_stack_bundle.py" pinned-in "$work/$1/loom-stack.tar.gz"
}

status=0
image=$(published "$version") || status=$?
if [ "$status" = 0 ]; then
  if [ -n "${CLICKHOUSE_IMAGE:-}" ] && [ "$CLICKHOUSE_IMAGE" != "$image" ]; then
    echo "::warning::clickhouse_image=$CLICKHOUSE_IMAGE ignored: v$version's published bundle names $image" >&2
  fi
  echo "$image"
  exit 0
fi
[ "$status" = 3 ] || exit "$status"

image=${CLICKHOUSE_IMAGE:-}
if [ -z "$image" ] && [[ $version != *-* ]]; then
  # An assignment, so a failed git stops the script (inside `for` it would not).
  candidates=$(git tag --points-at HEAD --list "v$version-rc.*" --sort=-v:refname)
  # A candidate whose run published no bundle: the next one down.
  for candidate in $candidates; do
    [[ $candidate =~ -rc\.[0-9]+$ ]] || continue
    status=0
    image=$(published "${candidate#v}") || status=$?
    [ "$status" = 0 ] && break
    [ "$status" = 3 ] || exit "$status"
    echo "::notice::$candidate published no bundle: passed over" >&2
  done
fi
[ -n "$image" ] || image=$(bash "$here/../../dev/clickhouse_release.sh")
bash "$here/refuse_downgrade.sh" "$image" >&2
echo "$image"
