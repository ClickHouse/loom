"""Resolver cluster reads account for each actual vector scan branch."""

from __future__ import annotations

import pytest

from server.core.errors import VectorCapacityError
from server.memory.store import conflict_resolver as queue
from tests.unit.test_conflict_resolver import _near, _peer_row, _q
from tests.unit.test_vector_admission_review_edges import _dispatch
from tests.unit.test_vector_read_admission import NativeBudgetError


async def test_resolver_cluster_accounts_for_nearest_and_both_anchored_branches(
    monkeypatch,
) -> None:
    near = _near("s2", "near-key", 0.1)
    anchored = _near("s3", "office_city", 0.2)
    rows = [
        _peer_row("near", "s2", "near-key", "near value"),
        _peer_row("anchor", "s3", "office_city", "anchored value"),
    ]
    ch, driver = _dispatch(monkeypatch, [[near], [anchored], rows])

    result = await queue.cluster_peers(_q(), floor=0.6, limit=8, ch=ch)

    assert result == queue.Cluster(
        [{**rows[0], "similarity": 0.9}, {**rows[1], "similarity": 0.8}], frozenset()
    )
    assert driver.started == 3
    assert [work.weight if work is not None else None for work in driver.work] == [1, 2, None]
    for index in (0, 1):
        work = driver.work[index]
        assert work is not None
        assert (work.org, work.namespaces) == ("o", ("ns",))
        assert driver.settings[index]["max_rows_to_read"] == 1_000_000
        assert driver.settings[index]["read_overflow_mode"] == "throw"
    assert "max_rows_to_read" not in driver.settings[2]


@pytest.mark.parametrize("failure_at", [1, 2], ids=["nearest", "anchored"])
async def test_resolver_cluster_native_rows_refusal_is_typed(monkeypatch, failure_at: int) -> None:
    ch, driver = _dispatch(monkeypatch, [[]])
    driver.failure_at = failure_at
    driver.failure = NativeBudgetError(158)

    with pytest.raises(Exception) as raised:
        await queue.cluster_peers(_q(), floor=0.6, limit=8, ch=ch)

    assert isinstance(raised.value, VectorCapacityError)
    assert raised.value.extra["reason"] == "rows"
    assert driver.started == failure_at


async def test_resolver_cluster_without_embedding_dispatches_nothing(monkeypatch) -> None:
    ch, driver = _dispatch(monkeypatch, [])

    assert await queue.cluster_peers(_q(embedding=[]), floor=0.6, limit=8, ch=ch) == queue.Cluster(
        [], frozenset()
    )
    assert driver.started == 0
    assert driver.work == []
