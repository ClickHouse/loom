"""memory.ask's primary retrieval keeps two kinds of row out of synthesis.

The first is a declared-`external` row nobody attested. It is held out while
LOOM_MEMORY_ASK_EXCLUDE_UNVERIFIED_EXTERNAL is on (the default). The second is
a superseded row, which leaves the pool unless the caller asked for retired
rows with `include_superseded`. Either way the search result the caller sees
is untouched: only the synthesis pool changes. The pure filters have their own
tests; these check that `_retrieve` applies them under the settings that
govern them.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app.ask import pool
from server.settings import settings


def _hit(key: str, **over: Any) -> NS:
    base: dict[str, Any] = {
        "memory_key": key,
        "source_class": "asserted",
        "verified": False,
        "status": "active",
        "content_excerpt": f"{key} fact",
        "value": f"{key} fact",
    }
    base.update(over)
    return NS(**base)


HITS = [
    _hit("vendor_doc", source_class="external"),
    _hit("vendor_doc_attested", source_class="external", verified=True),
    _hit("old_price", status="superseded"),
    _hit("price"),
]


def _run(monkeypatch: pytest.MonkeyPatch, *, include_superseded: bool = False) -> pool._AskRun:
    async def _search(_inp: Any) -> NS:
        return NS(results=list(HITS))

    async def _no_masks(*_: Any, **__: Any) -> list[Any]:
        return []

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr(pool, "superseded_rows_for_pool", _no_masks)
    return pool._AskRun(
        search_input=NS(include_superseded=include_superseded),
        question="what does it cost?",
        settings=settings,
        target_k=10,
        window=10,
        recency_weight=0.0,
    )


def _keys(hits: list[Any]) -> list[str]:
    return [h.memory_key for h in hits]


async def test_defaults_hold_out_unattested_external_and_superseded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.memory_ask_exclude_unverified_external is True
    run = _run(monkeypatch)
    await pool._retrieve(run)
    assert _keys(run.ranked) == ["vendor_doc_attested", "price"]
    assert run.superseded_dropped == 1
    assert _keys(run.retrieval.results) == _keys(HITS), "the caller's hits are untouched"


async def test_the_probation_setting_off_lets_external_rows_through(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    settings_override(memory_ask_exclude_unverified_external=False)
    run = _run(monkeypatch)
    await pool._retrieve(run)
    assert _keys(run.ranked) == ["vendor_doc", "vendor_doc_attested", "price"]


async def test_a_caller_asking_for_retired_rows_keeps_them_in_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch, include_superseded=True)
    await pool._retrieve(run)
    assert "old_price" in _keys(run.ranked) and run.superseded_dropped == 0
    assert "vendor_doc" not in _keys(run.ranked), "probation is independent of supersession"
