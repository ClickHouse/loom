"""The admission gates that fail open (the rate limiter, traffic
classification): each failure is counted per gate, and logged at most once per
interval with the count since the last line. The transports' gates and the
routes outside the op registry that draw the same limits share it."""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# A fail-open gate logs at most once per this many seconds, with the count of
# failures since its last line.
FAIL_OPEN_LOG_INTERVAL_S = 60.0

_FAIL_OPEN_TOTAL: dict[str, int] = {}
_FAIL_OPEN_SINCE_LOG: dict[str, int] = {}
_FAIL_OPEN_LAST_LOG: dict[str, float] = {}


def note(gate: str, op_name: str, err: BaseException) -> None:
    """Count one fail-open and log it at most once per interval per gate."""
    _FAIL_OPEN_TOTAL[gate] = _FAIL_OPEN_TOTAL.get(gate, 0) + 1
    _FAIL_OPEN_SINCE_LOG[gate] = _FAIL_OPEN_SINCE_LOG.get(gate, 0) + 1
    now = time.monotonic()
    last = _FAIL_OPEN_LAST_LOG.get(gate)
    if last is None or now - last >= FAIL_OPEN_LOG_INTERVAL_S:
        n = _FAIL_OPEN_SINCE_LOG.pop(gate, 1)
        _FAIL_OPEN_LAST_LOG[gate] = now
        log.warning(
            "%s failed open (%d call(s) since the last line; latest %s): %s",
            gate,
            n,
            op_name,
            err,
        )


def counts() -> dict[str, int]:
    """Fail-opens per gate since the process started."""
    return dict(_FAIL_OPEN_TOTAL)
