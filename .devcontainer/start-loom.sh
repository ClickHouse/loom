#!/bin/sh
# Installs the latest Loom release in a codespace, as a user would, and connects
# Claude Code to it. Run once, when the codespace is created.
set -eu

installer=$(mktemp)
trap 'rm -f "$installer"' EXIT
curl -fsSL --retry 3 https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o "$installer"

# The files and .env first, without starting anything.
LOOM_SKIP_START=1 sh "$installer"

# In a codespace the dashboard is served from the forwarded address, which a
# server without sign-in answers only when it is its public URL; desktop VS Code
# forwards to the laptop's localhost instead, whose page then sends that Origin.
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  env_file=$HOME/.loom/.env
  grep -q '^LOOM_PUBLIC_BASE_URL=' "$env_file" \
    || printf 'LOOM_PUBLIC_BASE_URL=https://%s-7777.%s\n' "$CODESPACE_NAME" "$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN" >>"$env_file"
  grep -q '^LOOM_CORS_ORIGINS=' "$env_file" || printf 'LOOM_CORS_ORIGINS=http://localhost:7777\n' >>"$env_file"
fi

# The install again keeps .env, then starts the stack and waits for it.
sh "$installer"

npm install --global --silent @anthropic-ai/claude-code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

cat <<'TEXT'

Loom is running. The dashboard is the forwarded port 7777 (Ports tab).
Try it: run `claude`, sign in, and say "Remember that I deploy only on Tuesdays".
In a new `claude` session, ask "When do I deploy?".
TEXT
