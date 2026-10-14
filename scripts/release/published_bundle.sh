#!/usr/bin/env bash
# Download the loom-stack.tar.gz the GitHub Release v<version> carries, and its
# checksum file when it has one, into <dir>. The version `latest` names the
# repository's latest release.
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
release=releases/tags/v$version
if [ "$version" = latest ]; then
  release=releases/latest
fi
err=$(mktemp)
trap 'rm -f "$err"' EXIT

if ! found=$(gh api "repos/$GITHUB_REPOSITORY/$release" -q '.tag_name, .assets[].name' 2>"$err"); then
  if grep -q "(HTTP 404)" "$err"; then
    exit 3
  fi
  cat "$err" >&2
  exit 1
fi
tag=$(head -n 1 <<<"$found")
assets=$(tail -n +2 <<<"$found")
if ! grep -qx loom-stack.tar.gz <<<"$assets"; then
  exit 3
fi
gh release download "$tag" --repo "$GITHUB_REPOSITORY" --pattern 'loom-stack.tar.gz*' --dir "$dir" || exit 1
