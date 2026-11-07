#!/usr/bin/env bash
# Prints `true` when every image already carries the version's tag on ghcr.io,
# else `false`. A run on a tag whose images are out (the registry dispatch, a
# later dispatch) then builds and tags none: an uncached rebuild would move the
# version's tags onto other bytes.
#
#   published_images.sh VERSION ghcr.io/clickhouse/loom ghcr.io/clickhouse/loom-extractor
#
# Signs in to nothing: a release's images are public. A package never pushed
# (an anonymous token refused with 403) or a missing tag (404) is "not out";
# any other answer fails the step, so an outage never reads as "not out".
# GHCR (default https://ghcr.io) is for tests.
set -euo pipefail

version=${1:?version}
shift
GHCR=${GHCR:-https://ghcr.io}
ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json"
answer=$(mktemp)
trap 'rm -f "$answer"' EXIT

published=true
for image in "$@"; do
  path=${image#ghcr.io/}
  status=$(curl -sS -o "$answer" -w '%{http_code}' \
    "$GHCR/token?service=ghcr.io&scope=repository:$path:pull" || echo 000)
  case $status in
    200) ;;
    403)
      published=false
      continue
      ;;
    *)
      echo "::error::$image: the registry answered HTTP $status for a pull token" >&2
      exit 1
      ;;
  esac
  token=$(jq -r '.token // empty' "$answer")
  status=$(curl -sS -o /dev/null -w '%{http_code}' -I -H "Authorization: Bearer $token" \
    -H "Accept: $ACCEPT" "$GHCR/v2/$path/manifests/$version" || echo 000)
  case $status in
    200) echo "$image:$version is published" >&2 ;;
    404) published=false ;;
    *)
      echo "::error::$image:$version: the registry answered HTTP $status" >&2
      exit 1
      ;;
  esac
done
echo "$published"
