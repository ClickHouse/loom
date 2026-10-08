"""The process-wide cache of each source's contract report: what a read
consults to project the contract's columns onto the source's own spelling,
and what the admission gate checks before it describes the schema again.
A passing report lives `tracker_contract_ttl_s`; a failing one a minute.
"""

from __future__ import annotations

import time

from server.settings import settings
from server.tracker.domain.contract import ContractReport

_reports: dict[str, tuple[float, ContractReport]] = {}
_FAILED_TTL_S = 60.0  # a fixed view is picked up within a minute


def _ttl_s() -> float:
    return max(1.0, float(settings.tracker_contract_ttl_s))


def cached(credential: str) -> ContractReport | None:
    hit = _reports.get(credential)
    if hit is None or hit[0] <= time.monotonic():
        return None
    return hit[1]


def reset_for_tests() -> None:
    _reports.clear()


def remember(credential: str, report: ContractReport) -> None:
    """Cache a verdict: a fresh check's, or one obtained outside it (the
    onboarding probe just described this source with the credential being
    registered)."""
    ttl = _ttl_s() if report.ok else _FAILED_TTL_S
    _reports[credential] = (time.monotonic() + ttl, report)


def mark_for_tests(credential: str, report: ContractReport, ttl_s: float = 3600.0) -> None:
    _reports[credential] = (time.monotonic() + ttl_s, report)
