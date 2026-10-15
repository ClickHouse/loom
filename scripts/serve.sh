#!/bin/sh
# The image's web tier. LOOM_WEB_WORKERS comes from the environment, else from
# the env file the server reads (.env outside Docker); unset in both, it starts
# one uvicorn process per CPU this container may use, bounded by its memory and
# at most 8 (server/core/web_workers.py). The count is exported so the server's
# rate limiter divides by the same number. One of the processes takes the
# supervisor lock and runs the background workers.
set -eu

chosen=""
# LOOM_RUNTIME_DIR, like the count, may be pinned in the env file alone.
pinned=${LOOM_RUNTIME_DIR:-}
if [ -z "$pinned" ]; then
  pinned=$(python -m server.core.env_file LOOM_RUNTIME_DIR)
fi
if [ -z "${LOOM_WEB_WORKERS:-}" ]; then
  LOOM_WEB_WORKERS=$(python -m server.core.env_file LOOM_WEB_WORKERS)
fi
if [ -z "$LOOM_WEB_WORKERS" ]; then
  LOOM_WEB_WORKERS=$(python -m server.core.web_workers)
  chosen=1
fi
# A word, zero, a negative or a number past the shell's range fails the test.
if ! [ "$LOOM_WEB_WORKERS" -ge 1 ] 2>/dev/null; then
  echo "loom: LOOM_WEB_WORKERS=$LOOM_WEB_WORKERS is not a positive whole number" >&2
  exit 2
fi
# One directory for the files the processes share, LOOM_RUNTIME_DIR or one
# chosen now, checked before they start. A single process shares none, so it
# needs none unless one is pinned; with no private directory (a read-only root
# without a tmpfs) a chosen count falls back to one process, unless pinned.
if [ -n "$pinned" ] || [ "$LOOM_WEB_WORKERS" -gt 1 ]; then
  status=0
  shared=$(python -m server.core.runtime_files) || status=$?
  if [ "$status" -eq 0 ]; then
    LOOM_RUNTIME_DIR=$shared
    export LOOM_RUNTIME_DIR
  elif [ "$status" -eq 2 ] && [ -n "$chosen" ] && [ -z "$pinned" ]; then
    echo "loom: no runtime directory to share; starting one web process" >&2
    LOOM_WEB_WORKERS=1
  else
    [ "$status" -eq 2 ] && echo "loom: not starting without the runtime directory" >&2
    exit "$status"
  fi
fi
export LOOM_WEB_WORKERS

# LOOM_UVICORN_LOOP is uvicorn's --loop, the stdlib loop unless it names
# another: uvloop forks the whole process for each git or indexer spawn, which
# holds its loop for tens of milliseconds once the heap is a few GB, and the
# workers and the code ops spawn git all day. LOOM_GRACEFUL_SHUTDOWN_S bounds
# the wait for in-flight requests on SIGTERM, so one slow request cannot hold
# the container until docker's SIGKILL skips the lifespan drains.
# LOOM_TRUSTED_PROXIES names the peers whose X-Forwarded-For/Proto uvicorn
# applies. LOOM_LISTEN_HOST is the address to bind: loopback by default, every
# interface in the image.
# exec: uvicorn must be PID 1 to receive docker's SIGTERM (a POSIX shell does
# not forward it to its child).
exec uvicorn server.main:app --host "${LOOM_LISTEN_HOST:-127.0.0.1}" --port "${PORT:-7777}" \
  --workers "$LOOM_WEB_WORKERS" --loop "${LOOM_UVICORN_LOOP:-asyncio}" \
  --timeout-graceful-shutdown "${LOOM_GRACEFUL_SHUTDOWN_S:-10}" \
  --proxy-headers --forwarded-allow-ips="${LOOM_TRUSTED_PROXIES:-127.0.0.1}"
