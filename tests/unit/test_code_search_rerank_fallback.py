"""`_rerank_units` reports a rerank only when the service produced one.

The rerank service fails open by returning its input list unchanged (a
provider error or an unparseable reply); that path must not count as
`inline_rerank_used = 1`.
"""

from __future__ import annotations

import pytest

from server.code.app.search.rerank import _rerank_units
from server.code.domain.units import CodeUnit


def _units(n: int = 3) -> list[CodeUnit]:
    return [
        CodeUnit(
            unit_id=f"u{i}", path=f"src/a{i}.cpp", symbol=f"f{i}", body="int x;", score=1.0 - i / 10
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_fallback_returns_fused_order_and_no_rerank_flag(monkeypatch):
    async def same_list(*, endpoint, prompt_template, query, candidates, **_kw):
        return candidates  # the service's fail-open shape

    monkeypatch.setattr("server.memory.app.rerank.rerank", same_list)
    units = _units()
    out, reranked = await _rerank_units("builtin:openai", "q", units)
    assert reranked is False
    assert [u.unit_id for u in out] == ["u0", "u1", "u2"]


@pytest.mark.asyncio
async def test_a_real_reorder_is_counted(monkeypatch):
    async def reversed_list(*, endpoint, prompt_template, query, candidates, **_kw):
        return list(reversed(candidates))

    monkeypatch.setattr("server.memory.app.rerank.rerank", reversed_list)
    out, reranked = await _rerank_units("builtin:openai", "q", _units())
    assert reranked is True
    assert [u.unit_id for u in out] == ["u2", "u1", "u0"]
