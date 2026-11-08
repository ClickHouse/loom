#!/usr/bin/env bash
# Checks that each image's X.Y.Z tag carries the keyless signature the release
# workflow's run on vX.Y.Z made, as a user checks it (anonymously, cosign 3).
#
#   verify_signatures.sh VERSION IMAGE...
set -euo pipefail

version=${1:?version}
shift
identity="https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v$version"
for image in "$@"; do
  cosign verify "$image:$version" --certificate-identity "$identity" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null || {
    echo "::error::could not verify release.yml's signature on $image:$version (cosign's error above); see docs/development/release-process.md#a-failed-tag-or-signature"
    exit 1
  }
  echo "$image:$version: signed by release.yml@refs/tags/v$version"
done
