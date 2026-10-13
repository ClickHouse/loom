#!/bin/sh
# The image's web tier. LOOM_WEB_WORKERS unset or empty starts one uvicorn
# process per CPU this container may use, bounded by its memory and at most 8
# (server/core/web_workers.py). The count is exported so the server's rate
# limiter divides by the same number. One of the processes takes the supervisor
# lock and runs the background workers.
set -eu

if [ -z "${LOOM_WEB_WORKERS:-}" ]; then
  LOOM_WEB_WORKERS=$(python -m server.core.web_workers)
fi
export LOOM_WEB_WORKERS
# One directory for the files the processes share, chosen before they start.
if [ -z "${LOOM_RUNTIME_DIR:-}" ]; then
  LOOM_RUNTIME_DIR=$(python -m server.core.runtime_files)
fi
export LOOM_RUNTIME_DIR

# LOOM_UVICORN_LOOP is uvicorn's --loop: `asyncio` switches back from uvloop
# without a rebuild. LOOM_GRACEFUL_SHUTDOWN_S bounds the wait for in-flight
# requests on SIGTERM, so one slow request cannot hold the container until
# docker's SIGKILL skips the lifespan drains. LOOM_TRUSTED_PROXIES names the
# peers whose X-Forwarded-For/Proto uvicorn applies. LOOM_LISTEN_HOST is the
# address to bind: loopback by default, every interface in the image.
# exec: uvicorn must be PID 1 to receive docker's SIGTERM (a POSIX shell does
# not forward it to its child).
exec uvicorn server.main:app --host "${LOOM_LISTEN_HOST:-127.0.0.1}" --port "${PORT:-7777}" \
  --workers "$LOOM_WEB_WORKERS" --loop "${LOOM_UVICORN_LOOP:-auto}" \
  --timeout-graceful-shutdown "${LOOM_GRACEFUL_SHUTDOWN_S:-10}" \
  --proxy-headers --forwarded-allow-ips="${LOOM_TRUSTED_PROXIES:-127.0.0.1}"
