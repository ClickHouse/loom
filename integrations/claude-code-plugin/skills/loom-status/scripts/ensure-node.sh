#!/bin/sh
# Loom's coding-agent hooks are .mjs files launched by /bin/sh, so a Node
# runtime in a login-shell-only version manager does not satisfy them.
#   ensure-node.sh --check   report only: exit 0 when ready, 1 when missing
#   ensure-node.sh           install Node when missing (root or sudo -n);
#                            exit 2 names what blocks the install
set -eu

HOOK_PATH=${LOOM_NODE_HOOK_PATH:-/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/home/linuxbrew/.linuxbrew/bin}
NODE_PATH=''
NODE_VERSION=''
SETUP_FILE=''
trap 'if [ -n "$SETUP_FILE" ]; then rm -f "$SETUP_FILE"; fi' EXIT HUP INT TERM

hook_node_ready() {
  NODE_PATH=$(PATH="$HOOK_PATH" /bin/sh -c 'command -v node' 2>/dev/null) || return 1
  NODE_VERSION=$("$NODE_PATH" --version 2>/dev/null) || return 1
  major=${NODE_VERSION#v}
  major=${major%%.*}
  case "$major" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$major" -ge 18 ]
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

require_privilege() {
  if [ "$(id -u)" -ne 0 ] && { ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; }; then
    echo 'Node is required, but installation needs root or passwordless sudo. Run this helper from a privileged shell.' >&2
    exit 2
  fi
}

download_setup() {
  SETUP_FILE=$(mktemp "${TMPDIR:-/tmp}/loom-node-setup.XXXXXX")
  curl -fsSL --connect-timeout 10 --max-time 120 "$1" -o "$SETUP_FILE"
}

if hook_node_ready; then
  printf 'Loom hook runtime ready: %s (%s)\n' "$NODE_PATH" "$NODE_VERSION"
  exit 0
fi

if [ "${1:-}" = '--check' ]; then
  echo 'Loom hooks need Node.js 18+ in the system shell PATH.' >&2
  exit 1
fi

case "$(uname -s)" in
  Linux)
    require_privilege
    if command -v apt-get >/dev/null 2>&1; then
      if ! command -v curl >/dev/null 2>&1; then
        as_root apt-get update
        as_root apt-get install -y curl
      fi
      download_setup 'https://deb.nodesource.com/setup_22.x'
      as_root bash "$SETUP_FILE"
      as_root apt-get install -y nodejs
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
      pkg=dnf
      command -v dnf >/dev/null 2>&1 || pkg=yum
      if ! command -v curl >/dev/null 2>&1; then
        as_root "$pkg" install -y curl
      fi
      download_setup 'https://rpm.nodesource.com/setup_22.x'
      as_root bash "$SETUP_FILE"
      as_root "$pkg" install -y nodejs
    elif command -v apk >/dev/null 2>&1; then
      as_root apk add --no-cache nodejs
    else
      echo 'Unsupported Linux package manager; install system-wide Node.js 18+ and retry.' >&2
      exit 2
    fi
    ;;
  Darwin)
    if ! command -v brew >/dev/null 2>&1; then
      echo 'Homebrew is needed to install Node.js automatically on macOS.' >&2
      exit 2
    fi
    brew upgrade node || brew install node
    ;;
  *)
    echo 'Unsupported OS; install system-wide Node.js 18+ and retry.' >&2
    exit 2
    ;;
esac

if ! hook_node_ready; then
  echo 'Node was installed, but /bin/sh still cannot find Node.js 18+ in its system PATH.' >&2
  exit 1
fi
printf 'Loom hook runtime ready: %s (%s). Restart the coding-agent session.\n' "$NODE_PATH" "$NODE_VERSION"
