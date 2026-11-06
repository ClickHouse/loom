"""served_totals: what the inspector shows as "Last recalled"."""

from datetime import UTC, datetime

from server.core.datetimes import UNIX_EPOCH
from server.memory.domain.served import served_totals

_T1 = datetime(2026, 1, 2, 3, 4, 5)
_T2 = datetime(2026, 1, 3, 3, 4, 5)


def test_counts_credits_and_served_events_of_every_agent() -> None:
    rows = [{"agent": "*", "n": 4, "last_at": _T1}, {"agent": "codex", "n": 2, "last_at": _T2}]
    assert served_totals(rows, 1, _T1) == {"served_count": 6, "last_served_at": _T2}


def test_no_event_and_no_credit_reads_none() -> None:
    assert served_totals([], 0, None) == {"served_count": 0, "last_served_at": None}
    # an imported "never" stored as the epoch
    out = served_totals([], 0, UNIX_EPOCH)
    assert out == {"served_count": 0, "last_served_at": None}


def test_an_imported_credit_with_no_events_is_the_floor() -> None:
    # a bulk import seeds recall_count / last_recalled_at with no event behind them
    aware = _T2.replace(tzinfo=UTC)
    out = served_totals([{"agent": "*", "n": 1, "last_at": _T1}], 9, aware)
    assert out == {"served_count": 9, "last_served_at": _T2}
