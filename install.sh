#!/bin/sh
# Install or update the source-free Loom stack. Published as an asset on each
# GitHub Release.
#
#   curl -fsSL …/install.sh | sh                      # with its own ClickHouse
#   curl -fsSL …/install.sh | sh -s -- --clickhouse https://loom@host:8443
#   curl -fsSL …/install.sh | sh -s -- --bundled      # back to its own ClickHouse
#
# --clickhouse (or LOOM_CH_URL) points Loom at a self-managed server or a
# ClickHouse Cloud service instead; --bundled switches an install back (and wins
# over LOOM_CH_URL). The password comes from LOOM_CH_PASSWORD,
# a prompt, or the URL (where `ps` and shell history see it); it is written
# only to .env (mode 600).
#
# LOOM_VERSION installs a release other than this copy's. The whole script is one
# function called on its last line, inside braces, so a download cut short
# anywhere runs nothing.

set -eu

main() {
  # The release build writes its own version here; this file in the repository
  # installs the latest release.
  RELEASE_VERSION=latest
  REPOSITORY=${LOOM_REPOSITORY:-ClickHouse/loom}
  # Where the release files are served; a rehearsal serves its own.
  RELEASES_URL=${LOOM_RELEASES_URL:-"https://github.com/$REPOSITORY/releases"}
  INSTALL_DIR=${LOOM_INSTALL_DIR:-"$HOME/.loom"}
  REQUESTED_VERSION=${LOOM_VERSION:-$RELEASE_VERSION}
  # Only the version asked for by name may be older than the one installed.
  NAMED_VERSION=
  [ "${LOOM_VERSION:-latest}" = latest ] || NAMED_VERSION=${LOOM_VERSION#v}
  SKIP_START=${LOOM_SKIP_START:-0}
  CH_URL=${LOOM_CH_URL:-}
  BUNDLED=0
  CH_URL_FLAG=0

  fail() {
    printf 'Loom installer: %s\n' "$*" >&2
    exit 1
  }

  # Prints 1 when release $1 is below $2, else 0 (X.Y.Z or X.Y.Z-rc.N; a
  # candidate is below its own final release, and rc.1 below rc.2).
  version_below() {
    awk -v a="$1" -v b="$2" 'BEGIN {
      split(a, x, /[.-]/); split(b, y, /[.-]/)
      for (i = 1; i <= 3; i++) {
        if (x[i] + 0 != y[i] + 0) { print ((x[i] + 0 < y[i] + 0) ? 1 : 0); exit }
      }
      if (index(a, "-") && index(b, "-")) { print ((x[5] + 0 < y[5] + 0) ? 1 : 0); exit }
      print ((index(a, "-") && !index(b, "-")) ? 1 : 0)
    }'
  }

  # Settings this run is given that .env keeps, so a later `docker compose` in the
  # install directory, which reads only .env, uses the same address and images.
  KEPT_SETTINGS="LOOM_PORT LOOM_BIND_HOST LOOM_IMAGE_REPOSITORY LOOM_EXTRACTOR_IMAGE_REPOSITORY LOOM_CLICKHOUSE_IMAGE"
  # given KEY: the value this run was given for KEY, empty when none.
  given() {
    eval "printf '%s' \"\${$1-}\""
  }
  for key in $KEPT_SETTINGS; do
    case $(given "$key") in
      *[!A-Za-z0-9._:/@-]*) fail "$key has a character .env cannot keep: $(given "$key")" ;;
    esac
  done
  case ${LOOM_PORT-} in
    '') ;;
    *[!0-9]*) fail "LOOM_PORT must be a port number: $LOOM_PORT" ;;
    *) [ "$LOOM_PORT" -ge 1 ] && [ "$LOOM_PORT" -le 65535 ] \
      || fail "LOOM_PORT must be between 1 and 65535: $LOOM_PORT" ;;
  esac

  while [ $# -gt 0 ]; do
    case $1 in
      --clickhouse)
        [ $# -ge 2 ] || fail "--clickhouse needs a URL"
        CH_URL=$2
        CH_URL_FLAG=1
        shift 2
        ;;
      --clickhouse=*)
        CH_URL=${1#--clickhouse=}
        CH_URL_FLAG=1
        shift
        ;;
      --bundled)
        BUNDLED=1
        shift
        ;;
      *) fail "unknown argument: $1 (the options are --clickhouse URL and --bundled)" ;;
    esac
  done
  if [ "$BUNDLED" = 1 ]; then
    [ "$CH_URL_FLAG" = 0 ] || fail "--bundled and --clickhouse are exclusive"
    CH_URL= # --bundled overrides a LOOM_CH_URL left in the environment
  fi

  command -v docker >/dev/null 2>&1 || fail "Docker is required: https://docs.docker.com/get-docker/"
  compose_version=$(docker compose version --short 2>/dev/null) || fail "the Docker Compose v2 plugin is required"
  compose_version=${compose_version#v}
  compose_minor=${compose_version#*.}
  compose_minor=${compose_minor%%.*}
  # depends_on.required (compose.yaml) needs Compose 2.20 or later.
  if [ "${compose_version%%.*}" -lt 2 ] || { [ "${compose_version%%.*}" -eq 2 ] && [ "$compose_minor" -lt 20 ]; }; then
    fail "Docker Compose $compose_version is too old; 2.20 or later is required"
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  # `docker compose version` answers without the daemon; a pull needs it.
  [ "$SKIP_START" = 1 ] || docker info >/dev/null 2>&1 \
    || fail "cannot reach the Docker daemon: start Docker, or run the installer as a user who may use it (\`docker info\` says which)"

  # Percent-decoding for the user and password of a URL.
  urldecode() {
    printf '%s' "$1" | LC_ALL=C awk '
      BEGIN { for (i = 1; i < 256; i++) byte[sprintf("%02x", i)] = sprintf("%c", i) }
      {
        out = ""
        rest = $0
        while ((i = index(rest, "%")) > 0) {
          code = tolower(substr(rest, i + 1, 2))
          if (code in byte) { out = out substr(rest, 1, i - 1) byte[code]; rest = substr(rest, i + 3) }
          else { out = out substr(rest, 1, i); rest = substr(rest, i + 1) }
        }
        printf "%s", out rest
      }'
  }

  # Sets CH_HOST, CH_PORT, CH_SECURE, CH_USER and CH_PASSWORD from a URL such
  # as https://user:password@host:8443 (http:// defaults to 8123, https:// to
  # 8443; the user is required).
  parse_clickhouse_url() {
    case $1 in
      https://*) CH_SECURE=true CH_PORT=8443 rest=${1#https://} ;;
      http://*) CH_SECURE=false CH_PORT=8123 rest=${1#http://} ;;
      *) fail "--clickhouse takes an http:// or https:// URL, such as https://default@abc.clickhouse.cloud:8443" ;;
    esac
    # CH_PASSWORD_SET: a password was given, even an empty one (`user:@host`).
    CH_USER= CH_PASSWORD= CH_PASSWORD_SET=
    # The user and password go first, up to the last `@`, so a `/` or `:` in the
    # password never reaches the host part, nor any message printed from it.
    case $rest in
      *@*)
        userinfo=${rest%@*}
        rest=${rest##*@}
        case $userinfo in
          *:*) CH_USER=$(urldecode "${userinfo%%:*}") CH_PASSWORD=$(urldecode "${userinfo#*:}") CH_PASSWORD_SET=1 ;;
          *) CH_USER=$(urldecode "$userinfo") ;;
        esac
        ;;
    esac
    rest=${rest%%[/?#]*}
    case $rest in
      \[*\]:*) CH_HOST=${rest%]:*}] CH_PORT=${rest##*]:} ;; # [IPv6]:port
      \[*\]) CH_HOST=$rest ;;
      *:*) CH_HOST=${rest%:*} CH_PORT=${rest##*:} ;;
      *) CH_HOST=$rest ;;
    esac
    [ -n "$CH_HOST" ] || fail "the ClickHouse URL names no host"
    [ -n "$CH_USER" ] || fail "the ClickHouse URL names no user: use one made for Loom, e.g. https://loom@$CH_HOST:$CH_PORT"
    case $CH_HOST in
      localhost | 127.* | ::1 | '[::1]')
        fail "$CH_HOST inside the Loom container is the container itself: use host.docker.internal for a server on this host"
        ;;
    esac
    case $CH_PORT in '' | *[!0-9]*) fail "the ClickHouse URL has an invalid port: $CH_PORT" ;; esac
    # Both go into .env as written, and the user into ch-setup's SQL.
    case $CH_USER in *[!A-Za-z0-9_.@-]*) fail "the ClickHouse user may hold only letters, digits and _ . @ -" ;; esac
    host_chars=$CH_HOST
    case $CH_HOST in \[*\]) host_chars=${CH_HOST#\[} host_chars=${host_chars%\]} ;; esac
    case $host_chars in *[!A-Za-z0-9._:-]*) fail "the ClickHouse URL has an invalid host: $CH_HOST" ;; esac
  }

  # read_password [HINT]: ask on the terminal, without echo.
  read_password() {
    # A subshell: a failed redirection on `:` would end the script itself.
    (: </dev/tty) 2>/dev/null || fail "no ClickHouse password: set LOOM_CH_PASSWORD or put it in the URL"
    printf 'ClickHouse password for %s@%s%s: ' "$CH_USER" "$CH_HOST" "${1:-}" >/dev/tty
    # Ctrl-C at the prompt stops the install, with the terminal echoing again.
    trap 'stty echo </dev/tty; printf "\\n" >/dev/tty; exit 130' HUP INT TERM
    stty -echo </dev/tty
    IFS= read -r CH_PASSWORD </dev/tty || CH_PASSWORD=
    stty echo </dev/tty
    trap - HUP INT TERM
    printf '\n' >/dev/tty
  }

  # dotenv_quote VALUE: VALUE double-quoted for .env, with `\`, `"` and `$`
  # escaped, which Compose reads back exactly (single quotes cannot hold a
  # trailing backslash).
  dotenv_quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"$]/\\&/g')"
  }

  # get_env KEY: KEY's value in .env, empty when the file or the key is missing;
  # one pair of quotes around it, or an unquoted value's trailing comment, is
  # dropped, as Compose reads it.
  get_env() {
    [ -f "$INSTALL_DIR/.env" ] || return 0
    awk -F= -v key="$1" '
      $1 == key { value = substr($0, length(key) + 2) }
      END {
        if (value ~ /^".*"$/ || value ~ /^\047.*\047$/) value = substr(value, 2, length(value) - 2)
        else { sub(/[ \t]+#.*$/, "", value); sub(/[ \t]+$/, "", value) }
        print value
      }
    ' "$INSTALL_DIR/.env"
  }

  if [ -n "$CH_URL" ]; then
    parse_clickhouse_url "$CH_URL"
    if [ -z "$CH_PASSWORD_SET" ] && [ -n "${LOOM_CH_PASSWORD+set}" ]; then
      CH_PASSWORD=$LOOM_CH_PASSWORD CH_PASSWORD_SET=1
    fi
    # A rerun for the same server and user may keep the password .env holds:
    # without a terminal it does, with one Enter does and typing replaces it.
    saved="$(get_env LOOM_CH_HOST):$(get_env LOOM_CH_PORT):$(get_env LOOM_CH_USER)"
    if [ -z "$CH_PASSWORD_SET" ] && [ "$saved" = "$CH_HOST:$CH_PORT:$CH_USER" ] \
      && grep -q '^LOOM_CH_PASSWORD=' "$INSTALL_DIR/.env"; then
      if (: </dev/tty) 2>/dev/null; then
        read_password " (Enter keeps the one saved in .env)"
        [ -n "$CH_PASSWORD" ] || KEEP_PASSWORD=1
      else
        KEEP_PASSWORD=1
        printf 'Using the ClickHouse password saved in .env; set LOOM_CH_PASSWORD to replace it\n'
      fi
      CH_PASSWORD_SET=1
    fi
    [ -n "$CH_PASSWORD_SET" ] || read_password
  fi

  # Compose prefers the shell's variables to .env; .env alone names the target,
  # the image version and the project (LOOM_VERSION was read above).
  unset COMPOSE_PROFILES LOOM_CH_HOST LOOM_CH_PORT LOOM_CH_SECURE LOOM_CH_USER LOOM_CH_PASSWORD \
    LOOM_CH_READER_USER LOOM_VERSION COMPOSE_PROJECT_NAME

  tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/loom-install.XXXXXX")
  trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

  if [ -n "${LOOM_INSTALL_BUNDLE:-}" ]; then
    cp "$LOOM_INSTALL_BUNDLE" "$tmp_dir/loom-stack.tar.gz"
    [ -n "${LOOM_INSTALL_CHECKSUM:-}" ] || fail "LOOM_INSTALL_CHECKSUM is required with LOOM_INSTALL_BUNDLE"
    cp "$LOOM_INSTALL_CHECKSUM" "$tmp_dir/loom-stack.tar.gz.sha256"
  else
    if [ "$REQUESTED_VERSION" = latest ]; then
      release_url="$RELEASES_URL/latest/download"
    else
      case "$REQUESTED_VERSION" in v*) tag=$REQUESTED_VERSION ;; *) tag=v$REQUESTED_VERSION ;; esac
      release_url="$RELEASES_URL/download/$tag"
    fi
    printf 'Downloading Loom %s…\n' "$REQUESTED_VERSION"
    for file in loom-stack.tar.gz loom-stack.tar.gz.sha256; do
      curl -fL --retry 3 --proto '=https' --tlsv1.2 "$release_url/$file" -o "$tmp_dir/$file" \
        || fail "could not download $release_url/$file: the releases with an installer are listed at $RELEASES_URL"
    done
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
  # A saved copy of an older release's installer would otherwise start an
  # older Loom and ClickHouse on data a newer one wrote.
  if [ "$NAMED_VERSION" != "$version" ] && [ -f "$INSTALL_DIR/VERSION" ]; then
    installed=$(sed -n '1p' "$INSTALL_DIR/VERSION")
    # A VERSION without leading numbers (empty, dev) compares as 0.0.0 and blocks
    # nothing; an awk error refuses.
    below=$(version_below "$version" "$installed") || fail "could not compare the installed Loom $installed with $version"
    [ "$below" = 0 ] || fail "Loom $installed is installed and this installer installs the older $version: to downgrade, pin LOOM_CLICKHOUSE_IMAGE in .env first (the upgrades guide, Downgrading an installer install), then set LOOM_VERSION=$version"
  fi

  mkdir -p "$INSTALL_DIR"
  cp "$bundle/compose.yaml" "$INSTALL_DIR/compose.yaml"
  cp "$bundle/VERSION" "$INSTALL_DIR/VERSION"
  # Releases before the deploy/ layout kept these two at the top level.
  rm -rf "$INSTALL_DIR/clickhouse-config" "$INSTALL_DIR/lemmagen3" "$INSTALL_DIR/deploy"
  cp -R "$bundle/deploy" "$INSTALL_DIR/deploy"

  # set_env KEY VALUE: replace KEY's line in .env, or append it. The value goes
  # through the environment, so awk never interprets it.
  set_env() {
    KEY=$1 VALUE=$2 awk '
      BEGIN { key = ENVIRON["KEY"]; line = key "=" ENVIRON["VALUE"] }
      index($0, key "=") == 1 { if (!done) print line; done = 1; next }
      { print }
      END { if (!done) print line }
    ' "$INSTALL_DIR/.env" >"$tmp_dir/env.updated"
    mv "$tmp_dir/env.updated" "$INSTALL_DIR/.env"
  }

  # .env holds the JWT secret and any ClickHouse password, so it is never
  # world-readable, not even between a write and the chmod.
  saved_umask=$(umask)
  umask 077
  if [ ! -f "$INSTALL_DIR/.env" ]; then
    secret=$(LC_ALL=C od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    extractor_token=$(LC_ALL=C od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    project="loom-local-$(LC_ALL=C od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
    sed \
      -e "s/__COMPOSE_PROJECT_NAME__/$project/" \
      -e "s/__LOOM_VERSION__/$version/" \
      -e "s/__LOOM_JWT_SECRET__/$secret/" \
      -e "s/__LOOM_DOCUMENT_EXTRACTOR_TOKEN__/$extractor_token/" \
      "$bundle/env.example" >"$INSTALL_DIR/.env"
    printf 'Created %s/.env\n' "$INSTALL_DIR"
  else
    printf 'Preserved existing %s/.env\n' "$INSTALL_DIR"
    had_env=1
    set_env LOOM_VERSION "$version"
    # An install from before the extractor service: give it the token and the
    # server its URL, as a new install's template does; a URL already there is
    # the operator's and stays. Values are read as Compose reads them (quotes and
    # comments stripped), so `TOKEN=""` is empty.
    if [ -z "$(get_env LOOM_DOCUMENT_EXTRACTOR_TOKEN)" ]; then
      [ -n "$(get_env LOOM_DOCUMENT_EXTRACTOR_URL)" ] \
        || set_env LOOM_DOCUMENT_EXTRACTOR_URL http://extractor:8790
      set_env LOOM_DOCUMENT_EXTRACTOR_TOKEN "$(LC_ALL=C od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    # An install from before profiles always ran the bundled ClickHouse, and its
    # compose file pinned the connection settings, ignoring those lines in .env:
    # keep it that way. Other LOOM_CH_* settings it honoured stay as they are.
    if ! grep -q '^COMPOSE_PROFILES=' "$INSTALL_DIR/.env"; then
      set_env COMPOSE_PROFILES bundled
      awk '/^LOOM_CH_(HOST|PORT|USER|PASSWORD|READER_USER|DATABASE|SECURE)=/ { print "# ignored before profiles: " $0; next } { print }' \
        "$INSTALL_DIR/.env" >"$tmp_dir/env.updated"
      mv "$tmp_dir/env.updated" "$INSTALL_DIR/.env"
    fi
  fi
  if [ -n "$CH_URL" ]; then
    case "${had_env:-0},$(get_env COMPOSE_PROFILES)," in
      1,*bundled,*)
        printf 'The bundled ClickHouse keeps its data in its volume; stop it with:\n'
        printf '  docker compose --project-directory %s --profile bundled stop clickhouse\n' "$INSTALL_DIR"
        ;;
    esac
    set_env COMPOSE_PROFILES ""
    set_env LOOM_CH_HOST "$CH_HOST"
    set_env LOOM_CH_PORT "$CH_PORT"
    set_env LOOM_CH_SECURE "$CH_SECURE"
    set_env LOOM_CH_USER "$CH_USER"
    [ -n "${KEEP_PASSWORD:-}" ] || set_env LOOM_CH_PASSWORD "$(dotenv_quote "$CH_PASSWORD")"
    printf 'Loom will use ClickHouse at %s:%s as %s\n' "$CH_HOST" "$CH_PORT" "$CH_USER"
  fi
  if [ "$BUNDLED" = 1 ]; then
    # The bundled server again: the outside one's connection settings go with it.
    awk '!/^LOOM_CH_(HOST|PORT|SECURE|USER|PASSWORD|READER_USER)=/' \
      "$INSTALL_DIR/.env" >"$tmp_dir/env.updated"
    mv "$tmp_dir/env.updated" "$INSTALL_DIR/.env"
    set_env COMPOSE_PROFILES bundled
    printf 'Loom will use its bundled ClickHouse\n'
  fi
  for key in $KEPT_SETTINGS; do
    [ -z "$(given "$key")" ] || set_env "$key" "$(given "$key")"
  done
  chmod 600 "$INSTALL_DIR/.env"
  umask "$saved_umask"

  if [ "$SKIP_START" = 1 ]; then
    printf 'Installed Loom %s stack files in %s\n' "$version" "$INSTALL_DIR"
    case ",$(get_env COMPOSE_PROFILES)," in
      *,bundled,*) ;;
      *) printf 'Before the first `up`, prepare your ClickHouse with:\n  docker compose --project-directory %s run --rm loom loom ch-setup\n' "$INSTALL_DIR" ;;
    esac
    exit 0
  fi

  compose() {
    docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" \
      -f "$INSTALL_DIR/compose.yaml" "$@"
  }

  printf 'Starting Loom %s (the first pull can take a few minutes)…\n' "$version"
  compose pull
  case ",$(get_env COMPOSE_PROFILES)," in
    *,bundled,*) ;;
    *)
      # Your own ClickHouse: create the database and the reader user, or stop
      # here with the SQL an admin has to run.
      status=0
      compose run --rm --no-deps loom loom ch-setup || status=$?
      case $status in
        0) ;;
        3) fail "that ClickHouse has no Keeper, which Loom needs (above). Give it one and run the installer again, or run it with --bundled to use the bundled ClickHouse, which runs its own" ;;
        *) fail "ClickHouse is not ready for Loom (above); fix it and run the installer again" ;;
      esac
      ;;
  esac
  compose up -d

  port=$(get_env LOOM_PORT)
  port=${port:-7777}
  # Loom listens where it is published: loopback unless LOOM_BIND_HOST names an address.
  case $(get_env LOOM_BIND_HOST) in
    '' | 0.0.0.0) host=127.0.0.1 ;;
    ::) host='[::1]' ;; # Docker publishes [::] on IPv6 only
    *:*) host="[$(get_env LOOM_BIND_HOST)]" ;;
    *) host=$(get_env LOOM_BIND_HOST) ;;
  esac
  health="http://$host:$port/v1/health"
  attempt=0
  while [ "$attempt" -lt 90 ]; do
    if curl -fsS "$health" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      printf '\nLoom is ready.\n'
      printf 'Dashboard: http://%s:%s/\n' "$host" "$port"
      printf 'Files:     %s\n' "$INSTALL_DIR"
      printf 'Logs:      docker compose --project-directory %s logs -f loom\n' "$INSTALL_DIR"
      # --profile bundled also stops a bundled server left from before a switch.
      printf 'Stop:      docker compose --project-directory %s --profile bundled down\n' "$INSTALL_DIR"
      exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done

  compose logs --tail 60 loom >&2 || true
  fail "Loom did not become healthy at $health within 180 seconds"
}

{ main "$@"; }
