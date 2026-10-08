#!/bin/sh
# Install or update the source-free Loom evaluator stack.
# Published as an asset on each GitHub Release.

set -eu

REPOSITORY=${LOOM_REPOSITORY:-ClickHouse/loom}
INSTALL_DIR=${LOOM_INSTALL_DIR:-"$HOME/.loom"}
REQUESTED_VERSION=${LOOM_VERSION:-latest}
SKIP_START=${LOOM_SKIP_START:-0}

fail() {
  printf 'Loom installer: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose v2 plugin is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/loom-install.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

if [ -n "${LOOM_INSTALL_BUNDLE:-}" ]; then
  cp "$LOOM_INSTALL_BUNDLE" "$tmp_dir/loom-stack.tar.gz"
  [ -n "${LOOM_INSTALL_CHECKSUM:-}" ] || fail "LOOM_INSTALL_CHECKSUM is required with LOOM_INSTALL_BUNDLE"
  cp "$LOOM_INSTALL_CHECKSUM" "$tmp_dir/loom-stack.tar.gz.sha256"
else
  if [ "$REQUESTED_VERSION" = latest ]; then
    release_url="https://github.com/$REPOSITORY/releases/latest/download"
  else
    case "$REQUESTED_VERSION" in v*) tag=$REQUESTED_VERSION ;; *) tag=v$REQUESTED_VERSION ;; esac
    release_url="https://github.com/$REPOSITORY/releases/download/$tag"
  fi
  printf 'Downloading Loom %s…\n' "$REQUESTED_VERSION"
  curl -fL --retry 3 --proto '=https' --tlsv1.2 \
    "$release_url/loom-stack.tar.gz" -o "$tmp_dir/loom-stack.tar.gz"
  curl -fL --retry 3 --proto '=https' --tlsv1.2 \
    "$release_url/loom-stack.tar.gz.sha256" -o "$tmp_dir/loom-stack.tar.gz.sha256"
fi

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$tmp_dir" && sha256sum -c loom-stack.tar.gz.sha256)
elif command -v shasum >/dev/null 2>&1; then
  expected=$(awk '{print $1}' "$tmp_dir/loom-stack.tar.gz.sha256")
  actual=$(shasum -a 256 "$tmp_dir/loom-stack.tar.gz" | awk '{print $1}')
  [ "$actual" = "$expected" ] || fail "stack bundle checksum mismatch"
else
  fail "sha256sum or shasum is required to verify the release bundle"
fi

mkdir -p "$tmp_dir/unpacked"
tar -xzf "$tmp_dir/loom-stack.tar.gz" -C "$tmp_dir/unpacked"
bundle="$tmp_dir/unpacked/loom-stack"
[ -f "$bundle/compose.yaml" ] && [ -f "$bundle/VERSION" ] || fail "release bundle is incomplete"
version=$(sed -n '1p' "$bundle/VERSION")

mkdir -p "$INSTALL_DIR"
cp "$bundle/compose.yaml" "$INSTALL_DIR/compose.yaml"
cp "$bundle/VERSION" "$INSTALL_DIR/VERSION"
rm -rf "$INSTALL_DIR/clickhouse-config" "$INSTALL_DIR/lemmagen3"
cp -R "$bundle/clickhouse-config" "$INSTALL_DIR/clickhouse-config"
cp -R "$bundle/lemmagen3" "$INSTALL_DIR/lemmagen3"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  secret=$(LC_ALL=C od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  project="loom-local-$(LC_ALL=C od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  sed \
    -e "s/__COMPOSE_PROJECT_NAME__/$project/" \
    -e "s/__LOOM_VERSION__/$version/" \
    -e "s/__LOOM_JWT_SECRET__/$secret/" \
    "$bundle/env.example" >"$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
  printf 'Created %s/.env\n' "$INSTALL_DIR"
else
  printf 'Preserved existing %s/.env\n' "$INSTALL_DIR"
  awk -v version="$version" '
    BEGIN { found = 0 }
    /^LOOM_VERSION=/ { print "LOOM_VERSION=" version; found = 1; next }
    { print }
    END { if (!found) print "LOOM_VERSION=" version }
  ' "$INSTALL_DIR/.env" >"$tmp_dir/env.updated"
  mv "$tmp_dir/env.updated" "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
fi

if [ "$SKIP_START" = 1 ]; then
  printf 'Installed Loom %s stack files in %s\n' "$version" "$INSTALL_DIR"
  exit 0
fi

printf 'Starting Loom %s (the first pull can take a few minutes)…\n' "$version"
docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" \
  -f "$INSTALL_DIR/compose.yaml" pull
docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" \
  -f "$INSTALL_DIR/compose.yaml" up -d

port=$(awk -F= '$1 == "LOOM_PORT" {print $2}' "$INSTALL_DIR/.env" | tail -n 1)
port=${port:-7777}
health="http://127.0.0.1:$port/v1/health"
attempt=0
while [ "$attempt" -lt 90 ]; do
  if curl -fsS "$health" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    printf '\nLoom is ready.\n'
    printf 'Dashboard: http://127.0.0.1:%s/me\n' "$port"
    printf 'Files:     %s\n' "$INSTALL_DIR"
    printf 'Logs:      docker compose --project-directory %s logs -f loom\n' "$INSTALL_DIR"
    printf 'Stop:      docker compose --project-directory %s down\n' "$INSTALL_DIR"
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 2
done

docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" \
  -f "$INSTALL_DIR/compose.yaml" logs --tail 60 loom >&2 || true
fail "Loom did not become healthy at $health within 180 seconds"
