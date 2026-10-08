"""The rrf lexical order is token overlap, and a namespace row that still
names a retired lexical knob keeps its other defaults."""

from __future__ import annotations

import pytest

from server.memory.app import retrieval as ms
from server.memory.app import search_bm25
from tests.unit.test_memory_search_service import StubCH, _q


def _leg(ch: StubCH) -> tuple[str, dict]:
    """The fact-leg statement among the calls the search issued."""
    for sql, params in ch.query_all_calls:
        if "score_tokens" in params:
            return sql, params
    raise AssertionError("no fact-leg statement issued")


@pytest.mark.asyncio
async def test_rrf_content_arm_is_token_overlap_and_reads_no_corpus_stats(monkeypatch) -> None:
    calls: list[int] = []

    async def stats(ch, *, org, namespace, tokens):
        calls.append(1)
        return [2.0 for _ in tokens], 300.0

    monkeypatch.setattr(search_bm25, "_fetch_bm25_corpus_stats", stats)
    ch = StubCH()
    ch.query_all_returns.append([])
    await ms.vector_search(_q(search_mode="rrf", query_text="cornaws cluster regressed"), ch=ch)
    sql, params = _leg(ch)
    assert "(tok, idf) ->" not in sql and "bm25_idfs" not in params
    assert "t -> if(positionCaseInsensitive(e.memory_key, t) > 0, 1.0, 0.0)" in sql
    assert calls == []  # no corpus-stats read was paid


def test_a_stored_row_naming_an_unknown_knob_keeps_its_other_defaults() -> None:
    from server.memory.app import namespace_retrieval_defaults as nrd

    raw = '{"rrf_idf_lexical": true, "rrf_lexical_pool": false, "search_mode": "rrf"}'
    assert nrd.parse(raw) == {"search_mode": "rrf"}
    assert "rrf_idf_lexical" not in nrd.FIELDS and "rrf_lexical_pool" not in nrd.FIELDS


def test_registering_an_unknown_knob_is_refused() -> None:
    from pydantic import ValidationError

    from server.identity.api.models import NamespaceRetrievalDefaults

    with pytest.raises(ValidationError):
        NamespaceRetrievalDefaults.model_validate({"rrf_idf_lexical": True})
