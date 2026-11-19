"""Mirror vector discovery owns attribution; the shared parser also serves scalar reads."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from server.core.errors import VectorAdmissionError
from server.core.vectors.admission import VectorWork, current_work
from server.knowledge.app import source_search as app
from server.knowledge.app.search import HybridSearchQuery
from server.knowledge.app.source_access import Admitted


@pytest.mark.parametrize("outcome", ["success", "refusal", "cancellation"])
async def test_vector_discovery_scope_covers_open_consume_and_close(monkeypatch, outcome):
    mirror = "acl-" + "1" * 24
    sid = "11111111-1111-4111-8111-111111111111"
    expected = VectorWork("acme", (mirror,), 1)
    candidate = {
        "namespace": mirror,
        "source_uri": "gdrive:file#" + sid,
        "chunk_idx": 0,
        "doc_version": 1,
        "revision": 0,
        "distance": 0.25,
        "lexical": 0.0,
        "matched": [],
    }
    plan = SimpleNamespace(
        org="acme",
        discovery_sources={(mirror, sid): "docs"},
        discovery_reader=None,
        failed=False,
        unread=False,
    )
    admitted = Admitted(
        "docs", candidate, {"namespace": mirror, "source_uri": candidate["source_uri"]}
    )
    phases = []
    refusal = VectorAdmissionError("discovery refused", reason="rows")

    async def check(batch, *, chunks):
        assert chunks and batch == [candidate]
        return [admitted]

    plan.check = check
    monkeypatch.setattr(app.pg, "enabled", lambda: False)

    @asynccontextmanager
    async def stream(sql, params):
        assert "cosineDistance" in sql
        assert params["org"] == "acme" and params["nss"] == [mirror]
        phases.append(("open", current_work()))

        async def rows():
            phases.append(("consume", current_work()))
            if outcome == "refusal":
                raise refusal
            if outcome == "cancellation":
                raise asyncio.CancelledError
            yield candidate

        try:
            yield rows()
        finally:
            phases.append(("close", current_work()))

    monkeypatch.setattr(app.store, "candidates", stream)
    query = HybridSearchQuery("acme", ("docs",), "fake", [1.0], 2)
    assert current_work() is None
    if outcome == "refusal":
        with pytest.raises(VectorAdmissionError) as caught:
            await app.collect_leg(plan, query, "semantic", limit=2)
        assert caught.value is refusal
    elif outcome == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await app.collect_leg(plan, query, "semantic", limit=2)
    else:
        result = await app.collect_leg(plan, query, "semantic", limit=2)
        assert result == [admitted]
        assert result[0].candidate == candidate
    assert phases == [("open", expected), ("consume", expected), ("close", expected)]
    assert not plan.failed and not plan.unread
    assert current_work() is None
