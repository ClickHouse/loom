#!/usr/bin/env bash
# Checks that each file in DIR carries the keyless signature the release
# workflow's run on vVERSION made, in DIR/<file name>.sigstore.json, as a user
# checks it with cosign 3. A file without its Sigstore bundle fails, and so does
# a Sigstore bundle without its file.
#
#   verify_release_files.sh VERSION DIR
set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: verify_release_files.sh VERSION DIR" >&2
  exit 2
}
version=$1
dir=$2
identity="https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v$version"
recovery="see docs/development/release-process.md#a-failed-tag-or-signature"
checked=0
for file in "$dir"/*; do
  case $file in
    *.sigstore.json)
      [ -f "${file%.sigstore.json}" ] || {
        echo "::error::$(basename "$file") signs no file on the release: delete it from the release; $recovery"
        exit 1
      }
      continue
      ;;
  esac
  [ -f "$file" ] || continue
  name=$(basename "$file")
  [ -f "$file.sigstore.json" ] || {
    echo "::error::$name has no Sigstore bundle ($name.sigstore.json); $recovery"
    exit 1
  }
  cosign verify-blob "$file" --bundle "$file.sigstore.json" --certificate-identity "$identity" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null || {
    echo "::error::could not verify release.yml's signature on $name (cosign's error above); $recovery"
    exit 1
  }
  echo "$name: signed by release.yml@refs/tags/v$version"
  checked=$((checked + 1))
done
[ "$checked" -gt 0 ] || {
  echo "::error::no release files in $dir; $recovery"
  exit 1
}
