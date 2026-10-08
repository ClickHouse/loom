#!/usr/bin/env bash
# Ingest one exported chat transcript into Loom's conversation plane through
# `loom ingest-transcript`. Host-neutral: any tool that can export a chat to
# a file. README.md next to this file holds the formats and exit codes.
#
#   ingest.sh [--dry-run] [--thread-id ID] <transcript.json|.md>
#
# Checks the file, its size and the identity before anything is sent. The
# token stays in the environment (the CLI reads LOOM_API_KEY or
# ~/.loom/token), and no diagnostic prints the token or transcript text.

set -euo pipefail

MAX_BYTES="${LOOM_INGEST_MAX_BYTES:-16777216}"

usage() {
  echo "usage: $0 [--dry-run] [--thread-id ID] <transcript.json|.md>" >&2
  exit 2
}
die() {
  printf 'ingest: %s\n' "$1" >&2
  exit "$2"
}

dry_run=0
thread_id=""
file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --thread-id)
      [[ $# -ge 2 ]] || usage
      thread_id="$2"
      shift
      ;;
    -h | --help) usage ;;
    -*) usage ;;
    *)
      [[ -z "$file" ]] || usage
      file="$1"
      ;;
  esac
  shift
done
[[ -n "$file" ]] || usage

if [[ -n "$thread_id" && ! "$thread_id" =~ ^[A-Za-z0-9._:-]{1,128}$ ]]; then
  die "--thread-id takes letters, digits and . _ : - (at most 128)" 2
fi
[[ "$MAX_BYTES" =~ ^[0-9]+$ ]] || die "LOOM_INGEST_MAX_BYTES must be a whole number" 2
[[ -n "${LOOM_NAMESPACE:-}" ]] || die "LOOM_NAMESPACE is not set" 2
if [[ -n "${LOOM_API_KEY:-}" ]]; then
  token_source="LOOM_API_KEY"
elif [[ -s "$HOME/.loom/token" ]]; then
  token_source="$HOME/.loom/token"
else
  die "no token: set LOOM_API_KEY or save one to ~/.loom/token" 2
fi
LOOM_BIN="${LOOM_BIN:-loom}"
command -v "$LOOM_BIN" >/dev/null 2>&1 || die "loom CLI not found (set LOOM_BIN)" 2

[[ -f "$file" && -r "$file" ]] || die "not a readable file: $file" 3
case "$file" in
  *.json | *.JSON | *.md | *.MD | *.markdown) ;;
  *) die "unsupported format: a .json array of {role, content} or a .md file with # user / # assistant sections" 4 ;;
esac
size=$(wc -c <"$file" | tr -d ' ')
((size <= MAX_BYTES)) || die "transcript is $size bytes, over the $MAX_BYTES-byte limit (LOOM_INGEST_MAX_BYTES)" 4

cmd=("$LOOM_BIN" ingest-transcript
  --namespace "$LOOM_NAMESPACE"
  --agent "${LOOM_AGENT:-generic}"
  --file "$file")
[[ -z "$thread_id" ]] || cmd+=(--thread-id "$thread_id")

if [[ $dry_run -eq 1 ]]; then
  # The CLI's dry run parses the whole file and previews turns; keep only its
  # summary line so no transcript text reaches the terminal or a log.
  printf 'server:    %s\nnamespace: %s\ntoken:     %s\nfile:      %s (%s bytes)\n' \
    "${LOOM_BASE_URL:-http://localhost:7777 (default)}" "$LOOM_NAMESPACE" "$token_source" "$file" "$size"
  set +e
  out="$("${cmd[@]}" --dry-run 2>&1)"
  code=$?
  set -e
  if [[ $code -eq 0 ]]; then
    printf '%s\n' "$out" | head -n 1
  else
    # The CLI's ERROR lines name a path, an item index or a JSON position.
    printf '%s\n' "$out" | grep '^ERROR:' >&2 || true
    printf 'ingest: dry run failed (loom exit %s); nothing was sent\n' "$code" >&2
  fi
  exit "$code"
fi

exec "${cmd[@]}"
