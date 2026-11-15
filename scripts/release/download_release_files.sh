#!/usr/bin/env bash
# Downloads every file the GitHub Release vVERSION carries into DIR, signed in
# to nothing, as a user downloads them. A release with no files fails.
#
#   download_release_files.sh VERSION DIR
set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: download_release_files.sh VERSION DIR" >&2
  exit 2
}
version=$1
dir=$2
release=$(curl -fsSL "https://api.github.com/repos/ClickHouse/loom/releases/tags/v$version")
urls=$(jq -r '.assets[].browser_download_url' <<<"$release")
[ -n "$urls" ] || {
  echo "::error::v$version has no release files"
  exit 1
}
mkdir -p "$dir"
for url in $urls; do
  curl -fsSL --output-dir "$dir" -O "$url"
done
