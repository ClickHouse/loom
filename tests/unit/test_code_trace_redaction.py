"""A code_search_trace row for a restricted namespace stores
the query digest and no returned paths / scope, flagged `redacted = 1`; a
standard namespace's row is unchanged."""

from __future__ import annotations

import asyncio
import time

import pytest

import server.code.app.common as cc
import server.identity.app.namespace_classification as nc

ORG, PRIV, PUB = "clickhouse", "code-example-private", "code-clickhouse"


@pytest.fixture
def capture(monkeypatch):
    rows: list[dict] = []

    class _Inner:
        async def insert(self, table, data, column_names=None, settings=None):
            rows.append(dict(zip(column_names, data[0], strict=True)))

    class _Client:
        client = _Inner()

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    from server.settings import settings

    monkeypatch.setattr(settings, "code_trace_log_enabled", True, raising=False)
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({PRIV}))
    yield rows
    nc.reset()


def _schedule(ns: str) -> None:
    cc.schedule_code_trace(
        org=ORG,
        ns=ns,
        op="code.search",
        query="keeper session timeout",
        route="hybrid",
        returned_ids=["u1", "u2"],
        returned_paths=["src/Coordination/KeeperServer.cpp", "src/b.cpp"],
        returned_scores=[0.9, 0.5],
        top_k=10,
        tokens_returned=400,
        abstain=False,
        retrieval_risk="low",
        embedding_model="m",
        started_monotonic=time.monotonic(),
        trace_run_id="run-1",
        path_prefix="src/Coordination/",
    )


async def _flush() -> None:
    await asyncio.gather(*list(cc._TRACE_TASKS))


@pytest.mark.asyncio
async def test_restricted_row_is_digested(capture) -> None:
    _schedule(PRIV)
    await _flush()
    (row,) = capture
    assert row["query"].startswith("sha256:") and "keeper" not in row["query"]
    assert row["returned_paths"] == [] and row["path_prefix"] == ""
    assert row["redacted"] == 1
    # what measures the plane stays
    assert row["returned_ids"] == ["u1", "u2"] and row["result_count"] == 2
    assert row["returned_scores"] == [0.9, 0.5]


@pytest.mark.asyncio
async def test_standard_row_is_unchanged(capture) -> None:
    _schedule(PUB)
    await _flush()
    (row,) = capture
    assert row["query"] == "keeper session timeout"
    assert row["returned_paths"] == ["src/Coordination/KeeperServer.cpp", "src/b.cpp"]
    assert row["path_prefix"] == "src/Coordination/" and row["redacted"] == 0
