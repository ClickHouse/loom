#!/usr/bin/env bash
# Download the loom-stack.tar.gz the GitHub Release v<version> carries into <dir>.
#
#   scripts/release/published_bundle.sh <version> <dir>
#
# Exits 3 when there is no such release or it carries no bundle. The release
# workflow never replaces a published bundle (replacing an asset deletes it
# first), so a release without one was never installable. Any other failure
# exits 1: an API error is never taken for "not published".
set -euo pipefail

version=$1
dir=$2
err=$(mktemp)
trap 'rm -f "$err"' EXIT

if ! assets=$(gh api "repos/$GITHUB_REPOSITORY/releases/tags/v$version" -q '.assets[].name' 2>"$err"); then
  if grep -q "(HTTP 404)" "$err"; then
    exit 3
  fi
  cat "$err" >&2
  exit 1
fi
if ! grep -qx loom-stack.tar.gz <<<"$assets"; then
  exit 3
fi
gh release download "v$version" --repo "$GITHUB_REPOSITORY" --pattern loom-stack.tar.gz --dir "$dir"
