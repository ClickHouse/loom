"""The retry policy: one frozen dataclass, validated field by
field, set on the Agent / Transport and overridable per call.

What is retried and why:

- connection failures (the server never saw the request);
- HTTP statuses in `statuses` — 408 and 5xx by default — for an op the
  registry marks idempotent (its `Idempotency-Key` is deduplicated). Any
  other op is retried only on a 408 or a refusal made before dispatch
  (503 AUTH_UNAVAILABLE): a 5xx after the handler started may have written;
- a read timeout only when the op is idempotent by the registry and not
  read-only (`retry_timeouts="idempotent"`): the request carried an
  `Idempotency-Key` the server dedups on. Any other op that timed out may
  still be executing (a read-only `memory.ask` would run its synthesis twice)
  and is never retried by default;
- a response cut off or a write that broke off, by the same rule as a read
  timeout: the call may have run, as a timed-out one may;
- never a request that could not be sent (a bad URL, a proxy refusal).

The server's `Retry-After` (or its body's `retry_after_s`) replaces the
backoff when within `max_retry_after_s`. A 429 is not a retry: it is pacing, bounded by the
Transport's `ratelimit_max_wait_s`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

RetryTimeouts = Literal["never", "idempotent", "always"]

#: The longest `Retry-After` honoured on a retried status.
DEFAULT_MAX_RETRY_AFTER_S = 60.0


def _default_statuses() -> frozenset[int]:
    return frozenset({408, *range(500, 600)})


@dataclass(frozen=True)
class RetryPolicy:
    """How a call retries. Every field is validated on construction and the
    offending field is named."""

    #: Total attempts including the first; 1 disables retries.
    max_attempts: int = 3
    #: First backoff; doubles per attempt.
    backoff_s: float = 0.1
    #: Ceiling on one backoff sleep.
    backoff_max_s: float = 5.0
    #: 0..1: each sleep is `delay × (1 − jitter/2 + random × jitter)`.
    backoff_jitter: float = 0.5
    #: HTTP statuses retried (a 429 is paced, not retried).
    statuses: frozenset[int] = field(default_factory=_default_statuses)
    #: Retry when the connection failed before a response.
    retry_connection_errors: bool = True
    #: Retry a read timeout, or a call whose response never arrived whole: never, only for
    #: ops the server dedups (registry-idempotent, not read-only), or always.
    retry_timeouts: RetryTimeouts = "idempotent"
    #: Honour a `Retry-After` on a retried status instead of the backoff.
    respect_retry_after: bool = True
    #: Longest `Retry-After` honoured; beyond it the backoff applies.
    max_retry_after_s: float = DEFAULT_MAX_RETRY_AFTER_S
    #: Total time one call may sleep between attempts; `inf` = unbounded.
    total_backoff_budget_s: float = math.inf

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError(
                f"retry.max_attempts must be an integer >= 1, got {self.max_attempts!r}"
            )
        for name in ("backoff_s", "backoff_max_s", "max_retry_after_s", "total_backoff_budget_s"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or v < 0:
                raise ValueError(f"retry.{name} must be a non-negative number, got {v!r}")
        j = self.backoff_jitter
        if (
            not isinstance(j, (int, float))
            or isinstance(j, bool)
            or math.isnan(j)
            or not 0 <= j <= 1
        ):
            raise ValueError(f"retry.backoff_jitter must be within 0..1, got {j!r}")
        if self.retry_timeouts not in ("never", "idempotent", "always"):
            raise ValueError(
                "retry.retry_timeouts must be never | idempotent | always, "
                f"got {self.retry_timeouts!r}"
            )
        statuses = frozenset(self.statuses)
        for s in statuses:
            if not isinstance(s, int) or isinstance(s, bool) or not 100 <= s <= 599:
                raise ValueError(f"retry.statuses holds a non-HTTP status {s!r}")
        object.__setattr__(self, "statuses", statuses)

    def replace(self, **changes: Any) -> RetryPolicy:
        """A copy with `changes` applied and validated; unknown names raise."""
        known = {f.name for f in fields(self)}
        unknown = set(changes) - known
        if unknown:
            raise ValueError(f"retry has no field {sorted(unknown)}; fields are {sorted(known)}")
        if "statuses" in changes and changes["statuses"] is not None:
            changes["statuses"] = frozenset(changes["statuses"])
        return replace(self, **changes)

    def retries_status(self, status: int) -> bool:
        return status in self.statuses

    def retries_timeout(self, *, idempotent: bool) -> bool:
        return self.retry_timeouts == "always" or (
            self.retry_timeouts == "idempotent" and idempotent
        )

    def backoff_for(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """Seconds to sleep after the `attempt`-th failure (1-based)."""
        exponential = min(self.backoff_s * float(2 ** (attempt - 1)), self.backoff_max_s)
        j = self.backoff_jitter
        return float(exponential * (1 - j / 2 + rand() * j))

    def as_dict(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["statuses"] = sorted(self.statuses)
        return d


DEFAULT_RETRY_POLICY = RetryPolicy()


def coerce_policy(value: RetryPolicy | dict[str, Any] | None, base: RetryPolicy) -> RetryPolicy:
    """`None` → base; a dict → `base.replace(**dict)`; a policy → itself."""
    if value is None:
        return base
    if isinstance(value, RetryPolicy):
        return value
    if isinstance(value, dict):
        return base.replace(**value)
    raise TypeError(
        f"retry must be a RetryPolicy or a dict of its fields, got {type(value).__name__}"
    )


__all__ = [
    "DEFAULT_MAX_RETRY_AFTER_S",
    "DEFAULT_RETRY_POLICY",
    "RetryPolicy",
    "RetryTimeouts",
    "coerce_policy",
]
