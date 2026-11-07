#!/usr/bin/env bash
# Prints `true` when every image already carries the version's tag on ghcr.io,
# `false` when none does, and fails when only some do (a docker-tag that stopped
# halfway: re-run the failed jobs of the tag's run, which tag the same build).
# A run on a tag whose images are out (the registry dispatch, a later dispatch)
# then builds and tags none: an uncached rebuild would move the version's tags
# onto other bytes.
#
#   REVISION=<commit> published_images.sh VERSION ghcr.io/clickhouse/loom ghcr.io/clickhouse/loom-extractor
#
# A published image must have been built from REVISION (its
# org.opencontainers.image.revision label): a tag moved to another commit after
# its images were pushed fails here, since those images would ship beside the
# new commit's files. Signs in to nothing: a release's images are public. A
# package never pushed (an anonymous token refused with 403) or a missing tag
# (404) is "not out"; any other answer fails, so an outage never reads as "not
# out". GHCR (default https://ghcr.io) is for tests.
set -euo pipefail

version=${1:?version}
shift
: "${REVISION:?the commit this run builds}"
GHCR=${GHCR:-https://ghcr.io}
ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
answer=$(mktemp)
trap 'rm -f "$answer"' EXIT

# get PATH: the registry's answer to an anonymous GET in $answer, its status printed.
get() {
  curl -sSL -o "$answer" -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H "Accept: $ACCEPT" "$GHCR/v2/$path/$1" || true
}

fail() {
  echo "::error::$*" >&2
  exit 1
}

out=() missing=()
for image in "$@"; do
  path=${image#ghcr.io/}
  status=$(curl -sS -o "$answer" -w '%{http_code}' \
    "$GHCR/token?service=ghcr.io&scope=repository:$path:pull" || true)
  case $status in
    200) token=$(jq -r '.token // empty' "$answer") ;;
    403)
      missing+=("$image")
      continue
      ;;
    *) fail "$image: the registry answered HTTP ${status:-no status} for a pull token" ;;
  esac
  status=$(get "manifests/$version")
  case $status in
    200) ;;
    404)
      missing+=("$image")
      continue
      ;;
    *) fail "$image:$version: the registry answered HTTP ${status:-no status}" ;;
  esac
  # The revision the image was built from, read from one platform's config.
  platform=$(jq -r '[.manifests[]? | select(.platform.os != "unknown")][0].digest // empty' "$answer")
  if [ -n "$platform" ]; then
    [ "$(get "manifests/$platform")" = 200 ] || fail "$image:$version: its $platform manifest is unreadable"
  fi
  config=$(jq -r '.config.digest // empty' "$answer")
  [ -n "$config" ] && [ "$(get "blobs/$config")" = 200 ] || fail "$image:$version: its config is unreadable"
  built=$(jq -r '.config.Labels["org.opencontainers.image.revision"] // empty' "$answer")
  [ "$built" = "$REVISION" ] \
    || fail "$image:$version was built from ${built:-an unnamed commit}, not $REVISION: a fix after the images are published needs a new version, never a moved tag"
  echo "$image:$version is published, built from this commit" >&2
  out+=("$image")
done
if [ "${#out[@]}" -gt 0 ] && [ "${#missing[@]}" -gt 0 ]; then
  fail "only ${out[*]} carries $version, not ${missing[*]}: re-run the failed jobs of the tag's run, which tag the same build (a new build would move the published tags)"
fi
[ "${#missing[@]}" -eq 0 ] && echo true || echo false
