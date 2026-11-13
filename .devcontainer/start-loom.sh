#!/bin/sh
# Installs the latest Loom release in a codespace, as a user would, and connects
# Claude Code to it. Run when the container is created; after a rebuild it finds
# the install in LOOM_INSTALL_DIR (devcontainer.json) and keeps it.
set -eu

: "${LOOM_INSTALL_DIR:?set by containerEnv in devcontainer.json}"
# The dev container's user may not own a directory under /workspaces yet.
if ! mkdir -p "$LOOM_INSTALL_DIR" 2>/dev/null || [ ! -w "$LOOM_INSTALL_DIR" ]; then
  sudo mkdir -p "$LOOM_INSTALL_DIR"
  sudo chown "$(id -u):$(id -g)" "$LOOM_INSTALL_DIR"
fi

# The docker-in-docker daemon may still be starting; the installer stops at once
# when it cannot reach it.
tries=0
until docker info >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || {
    echo "the Docker daemon did not start within 60 seconds" >&2
    exit 1
  }
  sleep 1
done

installer=$(mktemp)
trap 'rm -f "$installer"' EXIT
curl -fsSL --retry 3 https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o "$installer"

# The files and .env first, without starting anything.
LOOM_SKIP_START=1 sh "$installer"

# In a codespace the dashboard is served from the forwarded address, which a
# server without sign-in answers only when it is its public URL.
codespace_url=
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  codespace_url=https://$CODESPACE_NAME-7777.$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN
  grep -q '^LOOM_PUBLIC_BASE_URL=' "$LOOM_INSTALL_DIR/.env" \
    || printf 'LOOM_PUBLIC_BASE_URL=%s\n' "$codespace_url" >>"$LOOM_INSTALL_DIR/.env"
fi

# The install again keeps .env, then starts the stack and waits for it; Claude Code
# is connected below, so the installer connects no agent.
LOOM_CONNECT=none sh "$installer"

npm install --global --silent @anthropic-ai/claude-code
claude plugin marketplace list 2>/dev/null | grep -Eq '[[:space:]]loom$' \
  || claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# The installer printed the dashboard at 127.0.0.1, the codespace's own address.
printf '\nLoom is running in the codespace. Dashboard: %s\n' "${codespace_url:-the forwarded port 7777 (Ports tab)}"
cat <<'TEXT'
Try it: run `cd ~ && claude`, sign in, and say "Remember that I never deploy
on Friday." In a new `claude` session, ask "When can I deploy?".
TEXT
