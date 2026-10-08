#!/usr/bin/env bash
# Executable contract test for cursor-session-hook.sh. Runs the hook against a
# fake `loom` CLI that records its argv, environment and the transcript it was
# handed; needs bash, python3 and coreutils, and no server.
#
#   integrations/cursor/test.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/cursor-session-hook.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/loom-cursor-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

TOKEN="test-token-value"
MARKER="transcript-body-marker"
failures=0

fail() {
  printf 'FAIL %s: %s\n' "$CASE" "$*"
  failures=$((failures + 1))
}
pass() { printf 'ok   %s\n' "$CASE"; }

# The fake CLI: records argv (one per line), whether it saw the token in its
# environment, a copy of --file, and exits with $FAKE_EXIT after $FAKE_SLEEP.
cat >"$WORK/loom" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$FAKE_DIR/argv"
[[ -n "${LOOM_API_KEY:-}" ]] && echo yes >"$FAKE_DIR/token_in_env"
prev=""
for a in "$@"; do
  [[ "$prev" == "--file" ]] && cp "$a" "$FAKE_DIR/file"
  prev="$a"
done
sleep "${FAKE_SLEEP:-0}"
echo "ingested (fake cli stdout)"
exit "${FAKE_EXIT:-0}"
SH
chmod +x "$WORK/loom"

cat >"$WORK/t.jsonl" <<JSONL
{"role":"user","message":{"content":[{"type":"text","text":"question $MARKER"}]}}
{"role":"assistant","message":{"content":[{"type":"text","text":"answer"},{"type":"tool_use","name":"Read","input":{"path":"x"}}]}}
not json
{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{}}]}}
{"role":"system","message":{"content":"ignored"}}
JSONL

# run_hook <stdin> [args...]: fresh fake dir; sets OUT, ERR, CODE.
run_hook() {
  local stdin="$1"
  shift
  rm -rf "$WORK/fake"
  mkdir "$WORK/fake"
  set +e
  printf '%s' "$stdin" | env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$WORK" \
    FAKE_DIR="$WORK/fake" FAKE_EXIT="${FAKE_EXIT:-0}" FAKE_SLEEP="${FAKE_SLEEP:-0}" \
    LOOM_BIN="$WORK/loom" LOOM_API_KEY="$TOKEN" LOOM_NAMESPACE="${NS-ns1}" \
    ${EXTRA_ENV:+$EXTRA_ENV} \
    bash "$HOOK" "$@" >"$WORK/out" 2>"$WORK/err"
  CODE=$?
  set -e
  OUT="$(cat "$WORK/out")"
  ERR="$(cat "$WORK/err")"
}

expect_reply() {
  [[ "$CODE" -eq 0 ]] || fail "hook exit $CODE, want 0"
  [[ "$OUT" == "{}" ]] || fail "stdout '$OUT', want '{}'"
  if [[ "$ERR" == *"$TOKEN"* || "$ERR" == *"$MARKER"* ]]; then
    fail "stderr leaks the token or transcript content"
  fi
}

payload() { printf '{"conversation_id":"%s","hook_event_name":"sessionEnd","transcript_path":"%s"}' "$1" "$2"; }

CASE="jsonl transcript becomes a JSON array of text turns"
run_hook "$(payload conv-1 "$WORK/t.jsonl")"
expect_reply
got="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))' "$WORK/fake/file" 2>/dev/null || true)"
want="[{\"role\": \"user\", \"content\": \"question $MARKER\"}, {\"role\": \"assistant\", \"content\": \"answer\"}]"
[[ "$got" == "$want" ]] || fail "turns $got"
grep -qx -- "--thread-id" "$WORK/fake/argv" || fail "no --thread-id"
grep -qx "cursor-conv-1" "$WORK/fake/argv" || fail "thread id is not cursor-<conversation_id>"
grep -qx "ns1" "$WORK/fake/argv" || fail "namespace not passed"
grep -q -- "$TOKEN" "$WORK/fake/argv" && fail "token passed in argv"
[[ -f "$WORK/fake/token_in_env" ]] || fail "token not in the CLI's environment"
compgen -G "$WORK/loom-cursor.*" >/dev/null && fail "temporary directory left behind"
pass

CASE="no conversation_id gives a thread id stable across runs"
run_hook "{\"transcript_path\":\"$WORK/t.jsonl\"}"
first="$(grep '^cursor-' "$WORK/fake/argv")"
run_hook "{\"transcript_path\":\"$WORK/t.jsonl\"}"
second="$(grep '^cursor-' "$WORK/fake/argv")"
expect_reply
[[ -n "$first" && "$first" == "$second" ]] || fail "thread ids '$first' vs '$second'"
pass

CASE="an unsafe conversation_id is not used as a thread id"
run_hook "$(payload 'a b/../c' "$WORK/t.jsonl")"
expect_reply
grep -q '^cursor-a b' "$WORK/fake/argv" && fail "unsafe id used"
pass

CASE="off switch sends nothing"
EXTRA_ENV="LOOM_CAPTURE_CONVERSATIONS=false" run_hook "$(payload c "$WORK/t.jsonl")"
expect_reply
[[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"
pass

CASE="missing namespace sends nothing"
NS="" run_hook "$(payload c "$WORK/t.jsonl")"
expect_reply
[[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"
[[ "$ERR" == *LOOM_NAMESPACE* ]] || fail "no diagnostic"
pass

CASE="no transcript_path sends nothing"
run_hook '{"conversation_id":"c","hook_event_name":"stop"}'
expect_reply
[[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"
pass

CASE="malformed hook input falls back to CURSOR_TRANSCRIPT_PATH"
EXTRA_ENV="CURSOR_TRANSCRIPT_PATH=$WORK/t.jsonl" run_hook 'not json'
expect_reply
[[ -f "$WORK/fake/file" ]] || fail "CLI not called"
pass

CASE="a transcript over the size limit sends nothing"
EXTRA_ENV="LOOM_INGEST_MAX_BYTES=10" run_hook "$(payload c "$WORK/t.jsonl")"
expect_reply
[[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"
pass

CASE="an unsupported format sends nothing"
printf 'user: hi\n' >"$WORK/t.txt"
run_hook "$(payload c "$WORK/t.txt")"
expect_reply
[[ ! -f "$WORK/fake/argv" ]] || fail "CLI was called"
pass

CASE="a failing CLI (Loom offline) still replies {} and exit 0"
FAKE_EXIT=7 run_hook "$(payload c "$WORK/t.jsonl")"
expect_reply
[[ "$ERR" == *"exit 7"* ]] || fail "no diagnostic"
pass

CASE="manual mode passes the CLI's exit code through"
FAKE_EXIT=7 run_hook "" "$WORK/t.jsonl"
[[ "$CODE" -eq 7 ]] || fail "exit $CODE, want 7"
pass

CASE="a .md transcript is handed over as is"
printf '# user\nhi\n' >"$WORK/t.md"
run_hook "" "$WORK/t.md"
[[ "$CODE" -eq 0 ]] || fail "exit $CODE"
grep -qx "$WORK/t.md" "$WORK/fake/argv" || fail "file not passed through"
pass

if command -v timeout >/dev/null 2>&1; then
  CASE="a hung CLI is stopped at LOOM_INGEST_TIMEOUT"
  start=$SECONDS
  FAKE_SLEEP=30 EXTRA_ENV="LOOM_INGEST_TIMEOUT=1" run_hook "$(payload c "$WORK/t.jsonl")"
  expect_reply
  [[ $((SECONDS - start)) -lt 10 ]] || fail "took $((SECONDS - start)) s"
  pass
fi

if [[ $failures -ne 0 ]]; then
  printf '%d failure(s)\n' "$failures"
  exit 1
fi
echo "all cursor hook checks passed"
