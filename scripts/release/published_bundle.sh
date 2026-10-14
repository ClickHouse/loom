#!/usr/bin/env bash
# Download the loom-stack.tar.gz the GitHub Release v<version> carries, and its
# checksum file when it has one, into <dir>. The version `recent` instead
# downloads the bundle of each of the 30 newest published releases, prereleases
# included, into <dir>/<tag>/.
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
err=$(mktemp)
trap 'rm -f "$err"' EXIT

if [ "$version" = recent ]; then
  bundled='.[] | select(.draft | not) | select(any(.assets[]; .name == "loom-stack.tar.gz")) | .tag_name'
  if ! tags=$(gh api "repos/$GITHUB_REPOSITORY/releases?per_page=30" -q "$bundled" 2>"$err"); then
    cat "$err" >&2
    exit 1
  fi
  if [ -z "$tags" ]; then
    exit 3
  fi
  for tag in $tags; do
    mkdir -p "$dir/$tag"
    gh release download "$tag" --repo "$GITHUB_REPOSITORY" --pattern loom-stack.tar.gz --dir "$dir/$tag" || exit 1
  done
  exit 0
fi

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
