#!/usr/bin/env bash
# Cursor hook: hand one agent conversation's transcript to
# `loom ingest-transcript`. README.md next to this file holds the contract:
# the events it reads, the transcript shape, identity and what is captured.
#
# Hook mode (no argument): the hook JSON arrives on stdin. Every outcome
# exits 0 and prints `{}` on stdout, because stdout is the hook's JSON reply
# (a `stop` reply's followup_message would be submitted as the next prompt)
# and exit 2 would block the agent. Manual mode (a transcript path as $1):
# the exit code is the ingest's.
#
# Diagnostics go to stderr and name no transcript content and no token.

set -uo pipefail

MAX_BYTES="${LOOM_INGEST_MAX_BYTES:-16777216}"
TIMEOUT_S="${LOOM_INGEST_TIMEOUT:-120}"
MAX_PAYLOAD_BYTES=1048576

log() { printf 'loom-cursor-hook: %s\n' "$*" >&2; }

manual=0
[[ $# -gt 0 ]] && manual=1

# In hook mode the reply is `{}` and the exit 0 whatever happened.
finish() {
  local code="$1"
  if [[ $manual -eq 1 ]]; then
    exit "$code"
  fi
  printf '{}\n'
  exit 0
}

if [[ "${LOOM_CAPTURE_CONVERSATIONS:-true}" == "false" ]]; then
  finish 0
fi

payload="{}"
if [[ $manual -eq 0 && ! -t 0 ]]; then
  payload="$(head -c "$MAX_PAYLOAD_BYTES")"
fi

if [[ -z "${LOOM_NAMESPACE:-}" ]]; then
  log "LOOM_NAMESPACE is not set; nothing sent"
  finish 2
fi
if [[ -z "${LOOM_API_KEY:-}" && ! -s "$HOME/.loom/token" ]]; then
  log "no token: set LOOM_API_KEY or save one to ~/.loom/token; nothing sent"
  finish 2
fi
LOOM_BIN="${LOOM_BIN:-loom}"
if ! command -v "$LOOM_BIN" >/dev/null 2>&1; then
  log "loom CLI not found (set LOOM_BIN); nothing sent"
  finish 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "python3 not found; nothing sent"
  finish 2
fi
if ! [[ "$MAX_BYTES" =~ ^[0-9]+$ && "$TIMEOUT_S" =~ ^[0-9]+$ ]]; then
  log "LOOM_INGEST_MAX_BYTES and LOOM_INGEST_TIMEOUT must be whole numbers; nothing sent"
  finish 2
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/loom-cursor.XXXXXX")" || {
  log "cannot create a temporary directory; nothing sent"
  finish 1
}
trap 'rm -rf "$tmpdir"' EXIT

# Resolve the transcript and a stable thread id, and turn Cursor's JSONL into
# the JSON array the CLI reads. Prints one tab-separated line:
#   ok <file> <thread-id> <turns>   or   skip <reason>
read -r -d '' PREPARE <<'PY'
import hashlib, json, os, re, sys

out_path, explicit, max_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    payload = {}
if not isinstance(payload, dict):
    payload = {}

path = explicit or payload.get("transcript_path") or os.environ.get("CURSOR_TRANSCRIPT_PATH") or ""
if not isinstance(path, str) or not path:
    print("skip\tthe hook input names no transcript_path (turn on transcripts in Cursor)")
    sys.exit()
path = os.path.abspath(os.path.expanduser(path))
if not os.path.isfile(path):
    print("skip\ttranscript file not found")
    sys.exit()
size = os.path.getsize(path)
if size > max_bytes:
    print(f"skip\ttranscript is {size} bytes, over the {max_bytes}-byte limit")
    sys.exit()

conv = payload.get("conversation_id")
if isinstance(conv, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", conv):
    thread = f"cursor-{conv}"
else:
    thread = "cursor-" + hashlib.sha256(path.encode()).hexdigest()[:16]

suffix = os.path.splitext(path)[1].lower()
if suffix in (".json", ".md", ".markdown"):
    print(f"ok\t{path}\t{thread}\t-")
    sys.exit()
if suffix != ".jsonl":
    print(f"skip\tunsupported transcript format {suffix or '(none)'}")
    sys.exit()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


turns, unreadable = [], 0
with open(path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        message = row.get("message")
        text = text_of(message.get("content") if isinstance(message, dict) else None)
        if text.strip():
            turns.append({"role": row["role"], "content": text})
if not turns:
    print(f"skip\tno user or assistant text in the transcript ({unreadable} unreadable lines)")
    sys.exit()
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(turns, fh)
print(f"ok\t{out_path}\t{thread}\t{len(turns)}")
PY

result="$(printf '%s' "$payload" | python3 -c "$PREPARE" "$tmpdir/transcript.json" "${1:-}" "$MAX_BYTES" 2>/dev/null)"
IFS=$'\t' read -r status file thread turns <<<"$result"
case "$status" in
  ok) ;;
  skip)
    log "$file; nothing sent"
    finish 3
    ;;
  *)
    log "could not read the hook input or the transcript; nothing sent"
    finish 1
    ;;
esac

cmd=("$LOOM_BIN" ingest-transcript
  --namespace "$LOOM_NAMESPACE"
  --agent "${LOOM_AGENT:-cursor}"
  --thread-id "$thread"
  --file "$file")
if command -v timeout >/dev/null 2>&1; then
  cmd=(timeout "$TIMEOUT_S" "${cmd[@]}")
elif command -v gtimeout >/dev/null 2>&1; then
  cmd=(gtimeout "$TIMEOUT_S" "${cmd[@]}")
fi

# The CLI reads LOOM_BASE_URL and LOOM_API_KEY from the environment, so the
# token never appears in argv. Its stdout goes to stderr: stdout is the reply.
"${cmd[@]}" >&2
code=$?
if [[ $code -ne 0 ]]; then
  log "ingest of thread $thread failed (exit $code, ${turns} turns); running it again is safe: turns that already landed are skipped by msg_id"
fi
finish "$code"
