#!/usr/bin/env bash
# Checks that each release file carries the keyless signature the release
# workflow's run on vX.Y.Z made, in DIR/<file name>.sigstore.json, as a user
# checks it with cosign 3. A file without its bundle fails.
#
#   verify_release_files.sh VERSION DIR FILE...
set -euo pipefail

[ "$#" -ge 3 ] || {
  echo "usage: verify_release_files.sh VERSION DIR FILE..." >&2
  exit 2
}
version=$1
dir=$2
shift 2
identity="https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v$version"
for file in "$@"; do
  name=$(basename "$file")
  bundle="$dir/$name.sigstore.json"
  [ -f "$bundle" ] || {
    echo "::error::$name has no signature bundle ($name.sigstore.json)"
    exit 1
  }
  cosign verify-blob "$file" --bundle "$bundle" --certificate-identity "$identity" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null || {
    echo "::error::could not verify release.yml's signature on $name (cosign's error above); see docs/development/release-process.md#a-failed-tag-or-signature"
    exit 1
  }
  echo "$name: signed by release.yml@refs/tags/v$version"
done
