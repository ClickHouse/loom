#!/bin/sh
# The image's web tier. LOOM_WEB_WORKERS unset or empty starts one uvicorn
# process per CPU this container may use, bounded by its memory
# (server/core/web_workers.py). The count is exported so the server's rate
# limiter divides by the same number.
set -eu

if [ -z "${LOOM_WEB_WORKERS:-}" ]; then
  LOOM_WEB_WORKERS=$(python -m server.core.web_workers)
fi
export LOOM_WEB_WORKERS

# exec: uvicorn must be PID 1 to receive docker's SIGTERM.
exec uvicorn server.main:app --host 0.0.0.0 --port "${PORT:-7777}" \
  --workers "$LOOM_WEB_WORKERS" --loop "${LOOM_UVICORN_LOOP:-auto}" \
  --timeout-graceful-shutdown "${LOOM_GRACEFUL_SHUTDOWN_S:-10}" \
  --proxy-headers --forwarded-allow-ips="${LOOM_TRUSTED_PROXIES:-127.0.0.1}"
