#!/usr/bin/env bash
# The prerequisites a release publish needs that only a repository or
# organization owner can grant, checked in seconds before the builds start.
#
#   preflight.sh strict   # a release: any missing prerequisite fails the run
#   preflight.sh report   # a dry run: each one is a warning
#
# Reads GITHUB_REPOSITORY, GITHUB_ACTOR, GH_TOKEN (the job's token, with
# packages: write), REPOSITORY_PRIVATE ("true"/"false") and IMAGES (the ghcr.io
# image names, space-separated). GHCR (default https://ghcr.io) is for tests.
set -euo pipefail

mode=${1:?strict or report}
case $mode in strict | report) ;; *)
  echo "usage: $0 strict|report" >&2
  exit 2
  ;;
esac
GHCR=${GHCR:-https://ghcr.io}
missing=0

say() {
  if [ "$mode" = strict ]; then echo "::error::$1"; else echo "::warning::$1"; fi
  missing=$((missing + 1))
}

# Every publish is public and permanent, and a registry version can never be
# reused, so nothing is published from a private repository.
if [ "$REPOSITORY_PRIVATE" != false ]; then
  say "$GITHUB_REPOSITORY is private: a release publishes public packages, so it runs only once the repository is public"
fi

for image in $IMAGES; do
  path=${image#ghcr.io/}
  settings="https://github.com/orgs/${path%%/*}/packages/container/package/${path#*/}"
  # Push: a blob upload session the registry opens only for a writer, then cancelled.
  token=$(curl -sS -u "$GITHUB_ACTOR:$GH_TOKEN" \
    "$GHCR/token?service=ghcr.io&scope=repository:$path:pull,push" | jq -r '.token // empty' || true)
  status=000 location=
  if [ -n "$token" ]; then
    headers=$(curl -sS -o /dev/null -D - -X POST -H "Authorization: Bearer $token" \
      -H "Content-Length: 0" "$GHCR/v2/$path/blobs/uploads/" || true)
    status=$(awk 'toupper($1) ~ /^HTTP/ { code = $2 } END { print code + 0 }' <<<"$headers")
    location=$(awk 'tolower($1) == "location:" { sub(/\r$/, "", $2); print $2 }' <<<"$headers")
  fi
  if [ "$status" = 202 ]; then
    case $location in /*) location=$GHCR$location ;; esac
    [ -z "$location" ] || curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $token" "$location" || true
    echo "$image: this workflow can push"
  else
    say "$image: this workflow cannot push (HTTP $status). In $settings, under Manage Actions access, add $GITHUB_REPOSITORY with the Write role"
  fi
  # Pull: an install pulls without signing in, so the package must be public.
  if curl -sSf -o /dev/null "$GHCR/token?service=ghcr.io&scope=repository:$path:pull" 2>/dev/null; then
    echo "$image: anyone can pull"
  else
    say "$image: anyone cannot pull it, and an install signs in to nobody. In $settings, under Danger Zone, set the visibility to Public"
  fi
done

if [ "$missing" -gt 0 ] && [ "$mode" = strict ]; then
  echo "::error::$missing release prerequisite(s) missing (docs/development/release-process.md#before-the-first-release)"
  exit 1
fi
echo "$missing release prerequisite(s) missing"
