#!/bin/sh
# The image's web tier. LOOM_WEB_WORKERS unset or empty starts one uvicorn
# process per CPU this container may use: its cpuset, lowered by a CPU quota
# (`docker run --cpus`, compose `cpus:`). The count is exported so the server's
# rate limiter divides by the same number.
set -eu

if [ -z "${LOOM_WEB_WORKERS:-}" ]; then
  workers=$(nproc)
  if [ -r /sys/fs/cgroup/cpu.max ]; then
    read -r quota period </sys/fs/cgroup/cpu.max
    if [ "$quota" != max ]; then
      allowed=$(((quota + period - 1) / period))
      if [ "$allowed" -lt "$workers" ]; then
        workers=$allowed
      fi
    fi
  fi
  LOOM_WEB_WORKERS=$workers
fi
export LOOM_WEB_WORKERS

# exec: uvicorn must be PID 1 to receive docker's SIGTERM.
exec uvicorn server.main:app --host 0.0.0.0 --port "${PORT:-7777}" \
  --workers "$LOOM_WEB_WORKERS" --loop "${LOOM_UVICORN_LOOP:-auto}" \
  --timeout-graceful-shutdown "${LOOM_GRACEFUL_SHUTDOWN_S:-10}" \
  --proxy-headers --forwarded-allow-ips="${LOOM_TRUSTED_PROXIES:-127.0.0.1}"
