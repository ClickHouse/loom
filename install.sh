#!/bin/sh
# Install or update the source-free Loom stack. Published as an asset on each
# GitHub Release.
#
#   curl -fsSL …/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
#   … && sh loom-install.sh --clickhouse https://loom@host:8443 && rm loom-install.sh
#   … && sh loom-install.sh --bundled && rm loom-install.sh   # back to its own ClickHouse
#
# Saved first, so a failed download stops with curl's error and exit code.
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

  # Ctrl-C stops the run, and an agent CLI it started (a job of this shell, which
  # ignores the signal) with it.
  cmd_pid=
  dog_pid=
  on_interrupt() {
    [ -z "$cmd_pid" ] || kill "$cmd_pid" 2>/dev/null || true
    [ -z "$dog_pid" ] || kill "$dog_pid" 2>/dev/null || true
    printf '\n' >&2
    exit 130
  }
  trap on_interrupt HUP INT TERM

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

  # LOOM_LOCAL_MODELS: ollama sets Loom's model jobs on the Ollama of this machine
  # without asking, off never offers it.
  case ${LOOM_LOCAL_MODELS:-} in
    '' | ollama | off) ;;
    *) fail "LOOM_LOCAL_MODELS takes ollama or off: $LOOM_LOCAL_MODELS" ;;
  esac

  # LOOM_CONNECT: which coding agents to connect without asking, as a comma list.
  case ${LOOM_CONNECT:-} in
    *[!a-z,-]* | ,* | *, | *,,*) fail "LOOM_CONNECT takes claude-code, codex, vscode, all or none, in lowercase, separated by single commas: $LOOM_CONNECT" ;;
  esac
  case ,${LOOM_CONNECT:-}, in
    ,none,) ;;
    *,none,*) fail "LOOM_CONNECT=none connects no agent, so it takes no other name: $LOOM_CONNECT" ;;
  esac
  rest=${LOOM_CONNECT:-}
  while [ -n "$rest" ]; do
    client=${rest%%,*}
    case $rest in *,*) rest=${rest#*,} ;; *) rest= ;; esac
    case $client in
      claude-code | codex | vscode | all | none) ;;
      *) fail "LOOM_CONNECT takes claude-code, codex, vscode, all or none, separated by commas: $client" ;;
    esac
  done

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
    trap on_interrupt HUP INT TERM
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
  trap 'rm -rf "$tmp_dir"' EXIT

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
    # A new install gets the memory check once it is up (below); an upgrade does not.
    : >"$INSTALL_DIR/.memory-check-pending"
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
  # A model server on this machine (Ollama) for Loom's model jobs, no key:
  # offered on a new install when Ollama answers here. An install already set up keeps
  # its models (the dashboard's Models & keys does not show in .env).
  local_model=qwen2.5:3b
  use_local=
  if [ "${LOOM_LOCAL_MODELS:-}" = ollama ]; then
    use_local=1
  elif [ -z "${LOOM_LOCAL_MODELS:-}" ] && [ -z "${had_env:-}" ] \
    && [ -t 0 ] && [ -t 1 ] \
    && curl -fsS --noproxy '*' --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1; then
    printf 'Ollama answers on this machine. Use it for extraction and the other model jobs (%s, no API key)? [Y/n] ' "$local_model"
    read -r answer || answer=n
    case $answer in '' | [Yy]*) use_local=1 ;; esac
  fi
  if [ -n "$use_local" ]; then
    # The container reaches the host as host.docker.internal (compose.yaml).
    set_env LOOM_LLM_PROVIDER local
    set_env LOOM_LLM_MODEL "$local_model"
    set_env LOOM_LOCAL_LLM_BASE_URL http://host.docker.internal:11434/v1
    set_env LOOM_ASSISTANT_PROVIDER local
    set_env LOOM_ASSISTANT_MODEL "$local_model"
    set_env LOOM_ASSISTANT_ANSWER_CEILING_USD 0
    # The capture verifier drops facts and the conflict resolver retires rows on a
    # hosted model's judgment: both off rather than on the small model.
    set_env LOOM_MEMORY_EXTRACT_VERIFY_MODELS ""
    set_env LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED false
    # A CPU reads a long prompt for minutes; the hosted default would cut it short. A
    # server that timed out once answers no faster on a retry, so a call is tried once,
    # and the extraction workers' passes and the queue's lease outlast several calls.
    set_env LOOM_LLM_REQUEST_TIMEOUT_S 900
    set_env LOOM_LLM_RETRY_ATTEMPTS 1
    set_env LOOM_EXTRACTION_QUEUE_MAX_BATCHES_PER_PASS 1
    set_env LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS 3600
    set_env LOOM_EXTRACTION_QUEUE_LEASE_SECONDS 3900
    # A replay too long for this machine fails on its second try rather than hold the
    # queue for its default eight.
    set_env LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS 2
    set_env LOOM_OBSERVE_EXTRACT_MAX_PASS_SECONDS 3600
    # Jobs whose default names a hosted model take LOOM_LLM_MODEL.
    for key in LOOM_SESSION_BRIEF_LLM_MODEL LOOM_CODE_BISECT_LLM_RERANK_MODEL \
      LOOM_TRACKER_DUPCHECK_RERANK_MODEL LOOM_RETRIEVAL_JUDGE_MODEL; do
      set_env "$key" ""
    done
    printf 'Loom will run its model jobs on Ollama (%s)\n' "$local_model"
    [ -z "${had_env:-}" ] \
      || printf 'A configuration saved in Models & keys wins over .env: choose Use environment configuration there after the restart\n'
    curl -fsS --noproxy '*' --max-time 3 http://localhost:11434/api/tags 2>/dev/null \
      | grep -q "\"$local_model\"" \
      || printf 'Pull the model before the first extraction: ollama pull %s\n' "$local_model"
    printf 'Run Ollama with OLLAMA_CONTEXT_LENGTH=32768: extraction prompts are longer than its default window, which cuts them silently\n'
    [ "$(uname -s)" = Darwin ] \
      || printf 'With Docker Engine on Linux the container reaches Ollama only beyond loopback: run Ollama with OLLAMA_HOST=0.0.0.0 and keep port 11434 closed to the network\n'
  fi
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

  port=$(get_env LOOM_PORT)
  port=${port:-7777}
  # Loom listens where it is published: loopback unless LOOM_BIND_HOST names an address.
  case $(get_env LOOM_BIND_HOST) in
    '' | 0.0.0.0) host=127.0.0.1 ;;
    ::) host='[::1]' ;; # Docker publishes [::] on IPv6 only
    *:*) host="[$(get_env LOOM_BIND_HOST)]" ;;
    *) host=$(get_env LOOM_BIND_HOST) ;;
  esac

  # A port held by another program fails `up` only after the pull and the
  # ClickHouse start, with Docker's bind error: say so before either. The probes
  # bypass any proxy the shell names, which would otherwise answer for the port.
  # This install's own server, running or restarting, may hold the port: `up`
  # keeps or recreates it, so it is not probed when it is published on this port.
  own=$(compose ps -q --status running --status restarting loom 2>/dev/null) || own=
  own_ports=
  [ -z "$own" ] || own_ports=$(docker inspect -f \
    '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}} {{.HostPort}}{{end}}{{end}}' \
    $own 2>/dev/null) || own_ports=
  case "$own_ports " in
    *" $port "*) ;;
    *)
      # Curl's exit says what listens: 7 is a connection that could not be made
      # (refused, or no route to the address), taken as free; 0 (an answer), 1
      # and 8 (a reply that is not HTTP), 18 (a body cut short), 52 (an empty
      # reply), 55 and 56 (a reset while sending or receiving) come only from a
      # listener, and so does any exit after a connect time above zero. A
      # timeout with none is either a listener that never answers (curl 7.29
      # prints a fast connect as 0.000) or an address whose connect times out.
      probe=0
      connected=$(LC_ALL=C curl -s --noproxy '*' -o /dev/null -w '%{time_connect}' \
        --connect-timeout 3 --max-time 5 "http://$host:$port/" 2>/dev/null) || probe=$?
      case $probe,$connected in
        7,*) ;;
        28,0.000 | 28,0.000000) fail "port $port on $host does not answer: a program may hold it without answering, or LOOM_BIND_HOST, LOOM_PORT or a firewall keeps this host from reaching it" ;;
        0,* | 1,* | 8,* | 18,* | 52,* | 55,* | 56,* | *,*[1-9]*)
          other_port=$((port + 1))
          [ "$port" -lt 65535 ] || other_port=7778
          holder="another program"
          # A Loom answers health with its schema version, 503 included.
          curl -s --noproxy '*' --max-time 3 "http://$host:$port/v1/health" 2>/dev/null \
            | grep -q '"schema_version"' \
            && holder="another Loom server (another install, or one started from a checkout)"
          fail "port $port on $host is in use by $holder: stop it, or run the install command again with LOOM_PORT=$other_port in front of sh"
          ;;
        *) fail "could not check port $port on $host (curl exit $probe): check LOOM_BIND_HOST and LOOM_PORT" ;;
      esac
      ;;
  esac

  printf 'Starting Loom %s (the first pull can take a few minutes)…\n' "$version"
  compose pull \
    || fail "could not pull the Loom images (above); behind a proxy, give the Docker daemon its proxy settings: https://docs.docker.com/engine/daemon/proxy/"
  case ",$(get_env COMPOSE_PROFILES)," in
    *,bundled,*) ;;
    *)
      # Your own ClickHouse: create the database and the reader user, or stop
      # here with the SQL an admin has to run.
      status=0
      compose run --rm --no-deps loom loom ch-setup || status=$?
      case $status in
        0) ;;
        3) fail "that ClickHouse has no Keeper, which Loom needs (above). Give it one and run the install command again, or run it again with --bundled to use the bundled ClickHouse, which runs its own" ;;
        *) fail "ClickHouse is not ready for Loom (above); fix it and run the install command again" ;;
      esac
      ;;
  esac
  compose up -d

  health="http://$host:$port/v1/health"
  attempt=0
  until curl -fsS --noproxy '*' "$health" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 90 ]; then
      compose logs --tail 60 loom >&2 || true
      fail "Loom did not become healthy at $health within 180 seconds"
    fi
    sleep 2
  done

  dashboard="http://$host:$port/"
  printf '\nLoom is ready.\n'
  printf 'Dashboard: %s\n' "$dashboard"
  printf 'Files:     %s\n' "$INSTALL_DIR"
  printf 'Logs:      docker compose --project-directory %s logs -f loom\n' "$INSTALL_DIR"
  # --profile bundled also stops a bundled server left from before a switch.
  printf 'Stop:      docker compose --project-directory %s --profile bundled down\n' "$INSTALL_DIR"

  # The memory check: once per new install, a test memory saved, read back and
  # deleted with no token, as agent "-"; the read also says whether sign-in is on.
  org=$(get_env LOOM_SELF_SIGNUP_ORG)
  org=${org:-dev}
  namespace=$(get_env LOOM_DEFAULT_NAMESPACE)
  namespace=${namespace:-local}
  # api OP BODY [SECONDS]: POST to the server (20 s unless given), the body to
  # $tmp_dir/api.out; prints the status, 000 when no answer came.
  api() {
    curl -s --noproxy '*' --max-time "${3:-20}" -o "$tmp_dir/api.out" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' -d "$2" "http://$host:$port/v1/$1" || true
  }
  found() {
    grep -q '"found"[[:space:]]*:[[:space:]]*true' "$tmp_dir/api.out"
  }
  pending="$INSTALL_DIR/.memory-check-pending"
  # A new install, until its check passes: also a rerun after a run that stopped
  # before this point (a taken port, a failed pull) or whose check failed.
  first_up=
  [ ! -f "$pending" ] || first_up=1
  memory="\"org\":\"$org\",\"namespace\":\"$namespace\",\"agent\":\"-\",\"memory_key\":\"loom/installer-check\""
  skip_agents=
  case $org$namespace in
    # Both go into a JSON body as they are.
    *[!A-Za-z0-9._-]*)
      skip_agents=1
      # No check can run with this .env, on this run or a later one.
      rm -f "$pending"
      printf '\nSkipped the memory check and the agent setup: LOOM_SELF_SIGNUP_ORG or LOOM_DEFAULT_NAMESPACE in .env holds a character other than letters, digits and . _ -\n'
      ;;
    *)
      http_status=$(api memory.get "{$memory}")
      if [ "$http_status" = 200 ] && [ -f "$pending" ]; then
        # A first save on a cold server can take a while (its first embedding).
        set_status=$(api memory.set "{$memory,\"value\":\"A check the installer saves and deletes.\"}" 60)
        if [ "$set_status" != 200 ]; then
          # It may still land: the marker stays, and the next run deletes it.
          printf '\nCould not confirm a test memory saved (HTTP %s); the next run checks again and deletes it. `docker compose --project-directory %s logs loom` says why.\n' "$set_status" "$INSTALL_DIR"
        else
          # A replicated ClickHouse can answer the first read before it has the row.
          tries=0
          while :; do
            http_status=$(api memory.get "{$memory}")
            [ "$http_status" = 200 ] && found && break
            tries=$((tries + 1))
            [ "$tries" -lt 3 ] || break
            sleep 1
          done
          read_back=
          if [ "$http_status" = 200 ] && found; then read_back=1; fi
          delete_status=$(api memory.delete "{$memory}")
          if [ -n "$read_back" ] && [ "$delete_status" = 200 ]; then
            rm -f "$pending"
            printf '\nSaved, read back and deleted a test memory: writes and reads work.\n'
          elif [ -n "$read_back" ]; then
            printf '\nSaved and read back a test memory, but could not delete loom/installer-check; the next run tries again.\n'
          elif [ "$http_status" = 200 ]; then
            printf '\nSaved a test memory but could not read it back; the next run checks again, and `docker compose --project-directory %s logs loom` says why.\n' "$INSTALL_DIR"
          fi
        fi
      fi
      case $http_status in
        200) ;;
        401 | 403)
          skip_agents=1
          # A server with sign-in has no check to run: it is not one later either.
          rm -f "$pending"
          printf '\nSign-in is on: agents connect with a token from Settings → Tokens in the dashboard.\n'
          ;;
        *)
          skip_agents=1
          printf '\nCould not read memories (HTTP %s); `docker compose --project-directory %s logs loom` says why.\n' "$http_status" "$INSTALL_DIR"
          ;;
      esac
      ;;
  esac

  # bounded SECONDS CMD...: CMD, stopped after SECONDS (macOS has no `timeout`).
  # Run in this shell, not in a pipeline or $(...), so that Ctrl-C reaches what it
  # started. A child CMD started itself (a git clone) outlives a stop.
  bounded() {
    bound_s=$1
    shift
    "$@" &
    cmd_pid=$!
    # The watchdog takes its sleep with it when it is stopped.
    (
      nap=
      trap '[ -z "$nap" ] || kill "$nap" 2>/dev/null; exit 0' TERM
      sleep "$bound_s" &
      nap=$!
      wait "$nap" && kill "$cmd_pid" 2>/dev/null
    ) >/dev/null 2>&1 &
    dog_pid=$!
    cmd_status=0
    wait "$cmd_pid" || cmd_status=$?
    cmd_pid=
    kill "$dog_pid" 2>/dev/null || true
    dog_pid=
    # 143: the watchdog stopped it.
    [ "$cmd_status" != 143 ] || printf '%s did not finish within %s seconds\n' "$1" "$bound_s" >&2
    return "$cmd_status"
  }
  # wants NAME: whether to connect a client `considered` let through: yes when
  # LOOM_CONNECT named it, otherwise the terminal is asked (a run without one, such
  # as CI, skipped the agents altogether).
  wants() {
    [ -z "${LOOM_CONNECT:-}" ] || return 0
    printf 'Connect %s to Loom? [Y/n] ' "$1"
    read -r answer || return 1
    case $answer in '' | [Yy]*) return 0 ;; *) return 1 ;; esac
  }
  # considered CLIENT: whether this run looks at CLIENT at all: LOOM_CONNECT names it
  # (or all), or it is unset and a terminal may be asked.
  considered() {
    case ",${LOOM_CONNECT:-}," in *,all,* | *",$1,"*) return 0 ;; esac
    [ -z "${LOOM_CONNECT:-}" ]
  }
  # node_ok: Node.js 18 or later on PATH. Claude Code's plugin runs that `node`;
  # Codex's runs it through a launcher that also looks in nvm, fnm, Volta and the
  # like, so for Codex a miss is only a hint.
  node_ok() {
    major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null) || return 1
    case $major in '' | *[!0-9]*) return 1 ;; esac
    [ "$major" -ge 18 ]
  }

  # Agents on this machine, connected the way the README does it, with no token:
  # only a server without sign-in takes that. LOOM_CONNECT=none looks for none.
  agents=
  kept=
  guide=
  # Without a terminal and without LOOM_CONNECT no client can be connected, so
  # none is looked at either, and the guide is linked instead (not after a server
  # that refused the check, which said why).
  if [ -z "$skip_agents" ] && [ -z "${LOOM_CONNECT:-}" ] && ! { [ -t 0 ] && [ -t 1 ]; }; then
    skip_agents=1 guide=1
  fi
  if [ -z "$skip_agents" ] && [ "${LOOM_CONNECT:-}" != none ]; then
    server_url=http://localhost:$port
    [ "$host" = 127.0.0.1 ] || server_url=http://$host:$port
    # The plugins' own server URL, which a plugin installed before keeps.
    default_url=http://localhost:7777
    if considered claude-code && command -v claude >/dev/null 2>&1; then
      claude_config="${CLAUDE_CONFIG_DIR:-${HOME:-}}/.claude.json"
      # A plugin installed before, or a loom MCP entry added without it, is left
      # as it is, with the server it was set up for; reading the plugin list and
      # Claude Code's config starts nothing.
      if ! bounded 60 claude plugin list >"$tmp_dir/claude.plugins"; then
        printf 'Claude Code: could not list its plugins (above), so it is left as it is\n'
      elif grep -Eq '(^|[[:space:]])loom@loom$' "$tmp_dir/claude.plugins"; then
        printf 'Claude Code: the Loom plugin is installed already; `claude mcp list` shows the server it uses, and `claude plugin install loom@loom --config server_url=%s` points it here\n' "$server_url"
        kept=1
      elif grep -q '"loom"[[:space:]]*:' "$claude_config" 2>/dev/null; then
        printf 'Claude Code: a loom MCP server is set up already, for every project or for one (%s), and is left as it is; to use the plugin instead: claude mcp remove loom, then claude plugin install loom@loom --config server_url=%s\n' "$claude_config" "$server_url"
        kept=1
      elif ! node_ok; then
        # Its hooks and MCP header helper run the `node` on PATH, with no search.
        printf 'Claude Code: the Loom plugin runs the Node.js 18 or later on PATH, which is not there; install it and run this again, or add the MCP server alone: claude mcp add --transport http loom "%s/mcp?toolset=coding" -s user\n' "$server_url"
      elif wants "Claude Code"; then
        # A marketplace added before is no error: the install below says what fails.
        # It clones the repository: the slowest call, so it gets 5 minutes.
        bounded 300 claude plugin marketplace add ClickHouse/loom >/dev/null || true
        # The URL is always given: the plugin's default reads LOOM_BASE_URL.
        if bounded 60 claude plugin install loom@loom --config "server_url=$server_url" >/dev/null; then
          printf 'Claude Code: connected to %s (the plugin'"'"'s other settings are optional)\n' "$server_url"
          agents="$agents, Claude Code"
        else
          printf 'Claude Code: could not install the plugin (above); to try again: claude plugin marketplace add ClickHouse/loom; claude plugin install loom@loom --config server_url=%s\n' "$server_url"
        fi
      fi
    fi
    if considered codex && command -v codex >/dev/null 2>&1; then
      codex_config="${CODEX_HOME:-${HOME:-}/.codex}/config.toml"
      # The STATUS column of `codex plugin list`: "installed, enabled", "installed,
      # disabled" or, with only the marketplace added, "not installed".
      if ! bounded 60 codex plugin list >"$tmp_dir/codex.plugins"; then
        printf 'Codex: could not list its plugins (above), so it is left as it is\n'
      elif [ "$(awk '$1 == "loom@loom" { print $2; exit }' "$tmp_dir/codex.plugins")" = installed, ]; then
        printf 'Codex: the Loom plugin is installed already; `codex plugin list` shows whether it is enabled\n'
        kept=1
      elif grep -q '^\[mcp_servers\.loom\]' "$codex_config" 2>/dev/null; then
        printf 'Codex: a loom MCP server is set up already (%s)\n' "$codex_config"
        kept=1
      elif [ "$server_url" != "$default_url" ]; then
        # Codex plugins take no install options: the URL is fixed in the plugin.
        printf 'Codex: its Loom plugin always uses %s; to reach %s, see https://github.com/%s/blob/main/docs/features/integrations/codex.md\n' "$default_url" "$server_url" "$REPOSITORY"
      elif wants Codex; then
        bounded 300 codex plugin marketplace add ClickHouse/loom >/dev/null || true
        if bounded 60 codex plugin add loom@loom >/dev/null; then
          printf 'Codex: connected; run `codex` once and approve the plugin'"'"'s hooks\n'
          agents="$agents, Codex"
          node_ok || printf 'Codex: the plugin runs on Node.js 18 or later, which is not on PATH here; without one its launcher finds, use the MCP server alone instead: codex plugin remove loom@loom && codex mcp add loom --url "%s/mcp?toolset=coding"\n' "$server_url"
        else
          printf 'Codex: could not add the plugin (above); to try again: codex plugin marketplace add ClickHouse/loom; codex plugin add loom@loom\n'
        fi
      fi
    fi
    # VS Code's own CLI adds an MCP server by name; a `loom` entry the user has (for
    # a team server, say) is left as it is. Inside a remote session or WSL, `code`
    # is another machine's, so it is not offered there.
    vscode_mcp="${XDG_CONFIG_HOME:-${HOME:-}/.config}/Code/User/mcp.json"
    [ "$(uname -s)" != Darwin ] || vscode_mcp="${HOME:-}/Library/Application Support/Code/User/mcp.json"
    if ! considered vscode; then
      :
    elif command -v code >/dev/null 2>&1 && [ -n "${REMOTE_CONTAINERS:-}${CODESPACES:-}${SSH_CONNECTION:-}${WSL_DISTRO_NAME:-}" ]; then
      case ",${LOOM_CONNECT:-}," in
        *,vscode,* | *,all,*) printf 'VS Code: not offered in a remote session or WSL, where `code` is another machine'"'"'s\n' ;;
      esac
    elif command -v code >/dev/null 2>&1; then
      if grep -q '"loom"[[:space:]]*:' "$vscode_mcp" 2>/dev/null; then
        printf 'VS Code: a loom MCP server is set up already (%s)\n' "$vscode_mcp"
        kept=1
      elif wants "VS Code"; then
        if bounded 60 code --add-mcp "{\"name\":\"loom\",\"type\":\"http\",\"url\":\"$server_url/mcp?toolset=coding\"}" >/dev/null; then
          printf 'VS Code: added the loom MCP server\n'
          agents="$agents, VS Code"
        else
          printf 'VS Code: could not add the MCP server (above)\n'
        fi
      fi
    fi
    # A client LOOM_CONNECT names that this machine does not have.
    for client in "claude-code:claude:Claude Code" "codex:codex:Codex" "vscode:code:VS Code"; do
      name=${client%%:*} rest=${client#*:}
      case ",${LOOM_CONNECT:-}," in
        *",$name,"*) command -v "${rest%%:*}" >/dev/null 2>&1 \
          || printf '%s: `%s` is not on PATH\n' "${rest#*:}" "${rest%%:*}" ;;
      esac
    done
    if [ -n "$agents" ]; then
      printf '\nConnected: %s. Start a new session and say "Remember that I never deploy on Friday";\n' "${agents#, }"
      printf 'in the next session, ask "When can I deploy?"\n'
    elif [ -n "$kept" ]; then
      printf '\nYour agents keep the Loom setup they had.\n'
    else
      guide=1
    fi
  fi
  [ -z "$guide" ] \
    || printf '\nConnect an agent: https://github.com/%s/blob/main/docs/getting-started/connect-a-coding-agent.md\n' "$REPOSITORY"

  # The dashboard, for a new install on a desktop: a terminal, no SSH session and
  # a way to open a browser there.
  if [ -n "$first_up" ] && [ "${LOOM_NO_OPEN:-0}" != 1 ] && [ -t 1 ] && [ -z "${SSH_CONNECTION:-}" ]; then
    if [ "$(uname -s)" = Darwin ] && command -v open >/dev/null 2>&1; then
      open "$dashboard" >/dev/null 2>&1 || true
    elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$dashboard" >/dev/null 2>&1 &
    fi
  fi
  exit 0
}

{ main "$@"; }
