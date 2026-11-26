#!/usr/bin/env bash
set -euo pipefail

useradd -m ci
# A clone inside WSL, with Linux modes and line endings, as a WSL user's is.
git -c safe.directory='*' clone --quiet --no-local . /home/ci/repo
chown -R ci /home/ci/repo
npm ci --prefix /home/ci/repo/.github/agent-clis --ignore-scripts --no-fund --no-audit >/dev/null
node /home/ci/repo/scripts/code-toolchains/bind-native.cjs /home/ci/repo/.github/agent-clis claude
root=$(npm root --prefix /home/ci/repo/.github/agent-clis)
codex=$(ls "$root"/@openai/codex-linux-*/vendor/*/bin/codex | head -1)
test -n "$codex" && test -x "$codex" || {
  echo "The locked native agent executable is missing" >&2
  exit 1
}
su ci -s /bin/sh -c "
  set -e
  cd /home/ci/repo
  node --version
  git config --global user.email ci@example.com
  git config --global user.name ci
  node --test integrations/claude-code-plugin/tests/*.test.mjs
  node --test integrations/grep-shim/tests/*.test.mjs
  node integrations/claude-code-plugin/tests/e2e/agent-session.mjs claude --bin $root/@anthropic-ai/claude-code/bin/claude.exe --expect-tool Bash
  node integrations/claude-code-plugin/tests/e2e/agent-session.mjs codex --bin $codex
"
