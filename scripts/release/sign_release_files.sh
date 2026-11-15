#!/usr/bin/env bash
# Signs each file the release serves, downloaded into SERVED, as the release
# workflow's run on vVERSION, into SERVED/<file name>.sigstore.json. A file is
# signed only when it is byte-identical to this run's build in BUILT. One the
# run did not build (the stack bundle and checksum a release kept from an
# earlier run) keeps its Sigstore bundle when that verifies for this workflow
# and tag. Anything else fails: it was changed outside the workflow.
#
#   sign_release_files.sh VERSION SERVED BUILT
set -euo pipefail

[ "$#" -eq 3 ] || {
  echo "usage: sign_release_files.sh VERSION SERVED BUILT" >&2
  exit 2
}
version=$1
served=$2
built=$3
identity="https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v$version"
recovery="see docs/development/release-process.md#a-failed-tag-or-signature"
# Every file is checked before any is signed, so a refusal signs nothing.
sign=()
for file in "$served"/*; do
  case $file in *.sigstore.json) continue ;; esac
  name=$(basename "$file")
  ours=$(find "$built" -type f -name "$name")
  if [ -n "$ours" ] && cmp -s "$file" "$ours"; then
    sign+=("$file")
  elif [ -f "$file.sigstore.json" ] && cosign verify-blob "$file" --bundle "$file.sigstore.json" \
    --certificate-identity "$identity" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null; then
    echo "$name: kept, its Sigstore bundle verifies"
  else
    echo "::error::$name is not the file this run built and has no Sigstore bundle that verifies; it was changed outside the workflow: do not sign it, report it as SECURITY.md describes"
    exit 1
  fi
done
for file in "${sign[@]}"; do
  cosign sign-blob --yes --bundle "$file.sigstore.json" "$file" || {
    echo "::error::could not sign $(basename "$file") (cosign's error above); $recovery"
    exit 1
  }
done
