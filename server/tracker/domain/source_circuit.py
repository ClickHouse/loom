"""Per-source circuit state using the tracker's live thresholds."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)


@dataclass
class SourceCircuit:
    state: str = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
    consecutive_failures: int = 0
    opened_at: float = 0.0
    name: str = "qa_intel"
    # HALF_OPEN admits one probe; the rest are refused until its outcome is
    # recorded or it is released (a brief's twelve-way fan-out would otherwise
    # send all twelve at a service that may still be down). Event-loop only —
    # no lock: every caller is a coroutine on one loop.
    probing: bool = False

    def allow(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if (time.monotonic() - self.opened_at) <= _cooldown_s():
                return False
            self.state = "HALF_OPEN"
            log.info("qa_intel[%s] breaker: cooldown elapsed, entering HALF_OPEN", self.name)
        if self.probing:
            return False
        self.probing = True
        return True

    def release(self) -> None:
        """Free the probe slot without a verdict (an uncounted error, a cancellation)."""
        self.probing = False

    def record(self, success: bool) -> None:
        self.probing = False
        if success:
            if self.state != "CLOSED":
                log.info("qa_intel[%s] breaker: closing (was %s)", self.name, self.state)
            self.state = "CLOSED"
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= _failure_threshold() and self.state != "OPEN":
            self.state = "OPEN"
            self.opened_at = time.monotonic()
            log.warning(
                "qa_intel[%s] breaker: OPEN after %d consecutive failures (cooldown %ds)",
                self.name,
                self.consecutive_failures,
                int(_cooldown_s()),
            )

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "consecutive_failures": self.consecutive_failures}


def _failure_threshold() -> int:
    return max(1, int(settings.tracker_circuit_failure_threshold))


def _cooldown_s() -> float:
    return max(1.0, float(settings.tracker_circuit_cooldown_s))
