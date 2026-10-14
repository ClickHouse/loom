#!/usr/bin/env bash
# Does a freshly started colour run the repository-job sandbox? redeploy.sh
# runs this after the colour is healthy and before the proxy swaps to it: a
# profile that loads but refuses bwrap would otherwise leave repository jobs
# unsandboxed without a word.
#
# Each container started under the loom-repo-sandbox profile (decided from what
# it runs with, not the compose text) must pass sandbox_available(); one that
# does not run under it is skipped. A container that cannot be inspected or
# fails the probe stops the deploy, and the colour's containers are removed:
# the build already replaced the previous one there, so it is no rollback
# target, and its workers must not take the lease and run jobs unsandboxed.
#
# Usage: dev/sandbox_probe.sh <colour>
# Exit:  0 go · 1 stop (the colour's containers removed)

set -euo pipefail

colour=${1:?usage: dev/sandbox_probe.sh <colour>}
containers=("loom-$colour" "loom-$colour-workers")

stop() {
  echo "!!! $1; not swapping."
  echo "    Removing loom-$colour: --rollback must never start this build, and its workers must"
  echo "    not take the lease. See \"Repository-job sandbox\" in docs/operations/code-index-operations.md"
  docker rm -f "${containers[@]}" >/dev/null || true
  exit 1
}

for c in "${containers[@]}"; do
  opts=$(docker inspect -f '{{.HostConfig.SecurityOpt}}' "$c") || stop "cannot inspect $c"
  # The whole token: `[seccomp=… apparmor=loom-repo-sandbox systempaths=…]`.
  if [[ " ${opts//[\[\]]/ } " != *" apparmor=loom-repo-sandbox "* ]]; then
    continue
  fi
  # sandbox.py logs why a probe fails at WARNING, which shows here.
  docker exec "$c" python -c \
    'from server.code.app.index import sandbox as s; raise SystemExit(0 if s.sandbox_available() else 1)' \
    || stop "$c cannot run the repository-job sandbox (the warning above says why)"
done
