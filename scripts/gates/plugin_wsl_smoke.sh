#!/usr/bin/env bash
set -euo pipefail

useradd -m ci
# The tests use a checkout with Linux filesystem permissions and line endings.
git -c safe.directory='*' clone --quiet --no-local . /home/ci/repo
chown -R ci /home/ci/repo
npm ci --prefix /home/ci/repo/.github/agent-clis --ignore-scripts --no-fund --no-audit >/dev/null
node /home/ci/repo/scripts/code-toolchains/bind-native.cjs /home/ci/repo/.github/agent-clis claude
root=$(npm root --prefix /home/ci/repo/.github/agent-clis)
codex=("$root"/@openai/codex-linux-*/vendor/*/bin/codex)
if [ "${#codex[@]}" -ne 1 ] || [ ! -x "${codex[0]}" ]; then
  echo "Expected one executable for the locked Codex package" >&2
  exit 1
fi

runuser -u ci -- bash --noprofile --norc -euo pipefail -s -- "$root" "${codex[0]}" <<'SMOKE'
root=$1
codex=$2
cd /home/ci/repo
node --version
git config --global user.email ci@example.com
git config --global user.name ci
node --test integrations/claude-code-plugin/tests/*.test.mjs
node --test integrations/grep-shim/tests/*.test.mjs
node integrations/claude-code-plugin/tests/e2e/agent-session.mjs claude --bin "$root/@anthropic-ai/claude-code/bin/claude.exe" --expect-tool Bash
node integrations/claude-code-plugin/tests/e2e/agent-session.mjs codex --bin "$codex"
SMOKE
