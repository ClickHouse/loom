#!/usr/bin/env bash
# Executable contract test for ingest.sh: argument and identity checks against
# a fake `loom` CLI, then the dry run through the real CLI of this checkout
# when a Python with its dependencies is at hand (PYTHON, default python3).
#
#   integrations/generic/test.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SCRIPT="$HERE/ingest.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/loom-ingest-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

TOKEN="test-token-value"
MARKER="transcript-body-marker"
failures=0
fail() {
  printf 'FAIL %s: %s\n' "$CASE" "$*"
  failures=$((failures + 1))
}
pass() { printf 'ok   %s\n' "$CASE"; }

cat >"$WORK/loom" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$FAKE_DIR/argv"
[[ -n "${LOOM_API_KEY:-}" ]] && echo yes >"$FAKE_DIR/token_in_env"
exit "${FAKE_EXIT:-0}"
SH
chmod +x "$WORK/loom"
mkdir -p "$WORK/home"
printf '[{"role":"user","content":"hi %s"},{"role":"assistant","content":"hello"}]' "$MARKER" >"$WORK/t.json"

# run [args...]: sets OUT, ERR, CODE. BIN, NS, KEY override the defaults.
run() {
  rm -rf "$WORK/fake"
  mkdir "$WORK/fake"
  set +e
  env -i PATH="$PATH" HOME="$WORK/home" FAKE_DIR="$WORK/fake" FAKE_EXIT="${FAKE_EXIT:-0}" \
    LOOM_BIN="${BIN:-$WORK/loom}" LOOM_NAMESPACE="${NS-ns1}" LOOM_API_KEY="${KEY-$TOKEN}" \
    ${EXTRA_ENV:+$EXTRA_ENV} \
    bash "$SCRIPT" "$@" >"$WORK/out" 2>"$WORK/err"
  CODE=$?
  set -e
  OUT="$(cat "$WORK/out")"
  ERR="$(cat "$WORK/err")"
}
no_leak() {
  if [[ "$OUT$ERR" == *"$TOKEN"* || "$OUT$ERR" == *"$MARKER"* ]]; then
    fail "output leaks the token or transcript text"
  fi
}
not_called() { [[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"; }

CASE="a valid file is handed to the CLI without the token on argv"
run "$WORK/t.json"
[[ $CODE -eq 0 ]] || fail "exit $CODE"
grep -qx "$WORK/t.json" "$WORK/fake/argv" || fail "file not passed"
grep -qx "generic" "$WORK/fake/argv" || fail "default agent not passed"
grep -q -- "$TOKEN" "$WORK/fake/argv" && fail "token on argv"
[[ -f "$WORK/fake/token_in_env" ]] || fail "token not in the CLI's environment"
pass

CASE="--thread-id is passed through"
run --thread-id aider-42 "$WORK/t.json"
grep -qx "aider-42" "$WORK/fake/argv" || fail "thread id not passed"
pass

CASE="an unsafe --thread-id is refused"
run --thread-id 'a b' "$WORK/t.json"
[[ $CODE -eq 2 ]] || fail "exit $CODE, want 2"
not_called
pass

CASE="no argument is a usage error"
run
[[ $CODE -eq 2 ]] || fail "exit $CODE, want 2"
pass

CASE="missing namespace is refused before sending"
NS="" run "$WORK/t.json"
[[ $CODE -eq 2 ]] || fail "exit $CODE, want 2"
not_called
pass

CASE="no token anywhere is refused before sending"
KEY="" run "$WORK/t.json"
[[ $CODE -eq 2 ]] || fail "exit $CODE, want 2"
not_called
pass

CASE="a saved token is enough"
mkdir -p "$WORK/home/.loom"
printf '%s' "$TOKEN" >"$WORK/home/.loom/token"
KEY="" run "$WORK/t.json"
[[ $CODE -eq 0 ]] || fail "exit $CODE"
no_leak
rm -f "$WORK/home/.loom/token"
pass

CASE="a missing file is refused"
run "$WORK/nope.json"
[[ $CODE -eq 3 ]] || fail "exit $CODE, want 3"
not_called
pass

CASE="an unsupported format is refused"
printf 'x' >"$WORK/t.txt"
run "$WORK/t.txt"
[[ $CODE -eq 4 ]] || fail "exit $CODE, want 4"
not_called
pass

CASE="a file over the size limit is refused"
EXTRA_ENV="LOOM_INGEST_MAX_BYTES=10" run "$WORK/t.json"
[[ $CODE -eq 4 ]] || fail "exit $CODE, want 4"
not_called
pass

CASE="the CLI's exit code is the script's"
FAKE_EXIT=6 run "$WORK/t.json"
[[ $CODE -eq 6 ]] || fail "exit $CODE, want 6"
pass

PYTHON="${PYTHON:-python3}"
if (cd "$REPO" && "$PYTHON" -c 'import server.cli' 2>/dev/null); then
  printf '#!/usr/bin/env bash\ncd %q && exec %q -m server.cli "$@"\n' "$REPO" "$PYTHON" >"$WORK/real-loom"
  chmod +x "$WORK/real-loom"

  CASE="dry run through the real CLI prints the count and no transcript text"
  BIN="$WORK/real-loom" run --dry-run "$WORK/t.json"
  [[ $CODE -eq 0 ]] || fail "exit $CODE: $ERR"
  [[ "$OUT" == *"would ingest 2 turns"* ]] || fail "no summary: $OUT"
  no_leak
  pass

  CASE="dry run through the real CLI names a malformed item without its text"
  printf '[{"role":"user","content":"%s"},{"role":"user"}]' "$MARKER" >"$WORK/bad.json"
  BIN="$WORK/real-loom" run --dry-run "$WORK/bad.json"
  [[ $CODE -eq 4 ]] || fail "exit $CODE, want 4"
  [[ "$ERR" == *"item 1 missing role/content"* ]] || fail "no item diagnostic: $ERR"
  no_leak
  pass
else
  echo "skip real-CLI dry run: $PYTHON cannot import server.cli (set PYTHON to a Loom virtualenv's python)"
fi

if [[ $failures -ne 0 ]]; then
  printf '%d failure(s)\n' "$failures"
  exit 1
fi
echo "all generic ingest checks passed"
