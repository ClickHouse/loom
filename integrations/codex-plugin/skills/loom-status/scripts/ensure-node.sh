#!/bin/sh
# Loom's coding-agent hooks run through bin/loom-node, which finds Node 18+
# on PATH, under a version manager or in the usual install directories.
#   ensure-node.sh --check   report only: exit 0 when ready, 1 when missing
#   ensure-node.sh           install Node when missing (root or sudo -n);
#                            exit 2 names what blocks the install
set -eu

LAUNCHER="$(dirname "$0")/../../../bin/loom-node"
SETUP_FILE=''
trap 'if [ -n "$SETUP_FILE" ]; then rm -f "$SETUP_FILE"; fi' EXIT HUP INT TERM

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

if [ "${1:-}" = '--check' ]; then
  exec sh "$LAUNCHER" --check
fi

if sh "$LAUNCHER" --check 2>/dev/null; then
  exit 0
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
      echo 'Unsupported Linux package manager; install Node.js 18+, or set LOOM_NODE=/path/to/node.' >&2
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
    echo 'No installer for this OS; install Node.js 18+, or set LOOM_NODE=/path/to/node.' >&2
    exit 2
    ;;
esac

sh "$LAUNCHER" --check
echo 'Restart the coding-agent session.'
