"""An unreadable migration ledger stops the run; only a missing one means "fresh".

If `_applied` answered {} for any error, a timeout or an auth failure on a
live cluster would look like a fresh one and every migration would run again.
"""

from __future__ import annotations

import pytest

from server.core.schema import migrate as _migrate


class _CH:
    def __init__(self, error: str) -> None:
        self.error = error

    def query(self, sql, *a, **k):
        raise RuntimeError(self.error)


@pytest.mark.parametrize(
    "error",
    [
        "Code: 60. DB::Exception: Unknown table expression identifier 'loom._migrations'. (UNKNOWN_TABLE)",
        "Code: 81. DB::Exception: Database loom does not exist. (UNKNOWN_DATABASE)",
    ],
)
def test_a_missing_ledger_is_a_fresh_cluster(error: str) -> None:
    assert _migrate.applied_migrations(_CH(error)) == {}


@pytest.mark.parametrize(
    "error",
    [
        "Code: 159. DB::Exception: Timeout exceeded. (TIMEOUT_EXCEEDED)",
        "Code: 516. DB::Exception: default: Authentication failed. (AUTHENTICATION_FAILED)",
        "HTTPSConnectionPool: Max retries exceeded",
    ],
)
def test_any_other_ledger_failure_stops_the_run(error: str) -> None:
    with pytest.raises(RuntimeError):
        _migrate.applied_migrations(_CH(error))
