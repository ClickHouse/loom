#!/usr/bin/env bash
# Downloads every file the GitHub Release vVERSION carries into DIR, signed in
# to nothing, as a user downloads them. The release listing sends GH_TOKEN when
# it is set, which only lifts the API's anonymous rate limit. A release with no
# files fails.
#
#   download_release_files.sh VERSION DIR
set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: download_release_files.sh VERSION DIR" >&2
  exit 2
}
version=$1
dir=$2
auth=()
[ -z "${GH_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $GH_TOKEN")
release=$(curl -fsSL "${auth[@]}" "https://api.github.com/repos/ClickHouse/loom/releases/tags/v$version")
urls=$(jq -r '.assets[].browser_download_url' <<<"$release")
[ -n "$urls" ] || {
  echo "::error::v$version has no release files; see docs/development/release-process.md#a-failed-tag-or-signature"
  exit 1
}
mkdir -p "$dir"
while read -r url; do
  curl -fsSL --output-dir "$dir" -O "$url"
done <<<"$urls"
