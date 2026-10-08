"""code.callers / code.callees: `min_confidence` reaches the edge query, and
every returned edge still carries its own confidence."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.settings import settings

SEED = {
    "symbol_id": "s1",
    "qualified_name": "DB::Foo::bar",
    "name": "bar",
    "signature": "void bar()",
    "path": "src/Foo.cpp",
    "start_line": 1,
    "end_line": 9,
    "tier": "scip",
    "kind": "method",
}
EDGES = [
    {
        "tier": "scip",
        "edge_kind": "calls",
        "from_symbol_id": "c1",
        "to_symbol_id": "s1",
        "path": "src/A.cpp",
        "line": 5,
        "from_qualified_name": "DB::A::run",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 1.0,
    },
    {
        "tier": "treesitter",
        "edge_kind": "calls",
        "from_symbol_id": "c2",
        "to_symbol_id": "s1",
        "path": "src/B.cpp",
        "line": 7,
        "from_qualified_name": "check",
        "to_qualified_name": "DB::Foo::bar",
        "confidence": 0.6,
    },
]


class _FakeRepo:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def symbols_by_name(self, org, ns, name, limit=10):
        return [SEED]

    async def symbols_by_ids(self, org, ns, ids):
        return [SEED]

    async def edges_for_symbols(self, org, ns, ids, **kw):
        self.kwargs.append(kw)
        minc = float(kw.get("min_confidence") or 0.0)
        return [e for e in EDGES if e["confidence"] >= minc]

    async def all_state(self, org, ns):
        return {}


def _run(**fields) -> tuple[Any, _FakeRepo]:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph as h

    fake = _FakeRepo()

    async def no_dispatch(repo, org, ns, seeds):
        return []

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "_dispatch_seeds", new=no_dispatch),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_callers(
                CodeCallersInput(name="DB::Foo::bar", include_ambiguous=False, **fields)
            )

    return asyncio.run(go()), fake


def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_default_keeps_every_grade_and_reports_confidence(monkeypatch):
    _settings(monkeypatch)
    out, fake = _run()
    assert fake.kwargs[0]["min_confidence"] == 0.0
    assert [(e.tier, e.confidence) for e in out.edges] == [("scip", 1.0), ("treesitter", 0.6)]


def test_min_confidence_drops_the_name_table_grade(monkeypatch):
    _settings(monkeypatch)
    out, fake = _run(min_confidence=0.7)
    assert fake.kwargs[0]["min_confidence"] == 0.7
    assert [e.from_symbol for e in out.edges] == ["DB::A::run"], "the 0.6 `check` edge is gone"


def test_min_confidence_is_bounded():
    import pytest
    from pydantic import ValidationError

    from server.code.api.models_symbol import CodeCallersInput

    with pytest.raises(ValidationError):
        CodeCallersInput(name="x", min_confidence=1.5)


def test_a_cancelled_call_cancels_its_staleness_read(monkeypatch):
    """The staleness read runs inside the handler's gather, so a caller that gives up (a
    deadline, a closed connection) takes it down too instead of leaving it running."""
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph as h

    _settings(monkeypatch)
    fake = _FakeRepo()
    stale_cancelled = asyncio.Event()

    async def no_dispatch(repo, org, ns, seeds):
        return []

    async def forever(*_a, **_k):
        await asyncio.sleep(3600)

    async def stale(*_a, **_k):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stale_cancelled.set()
            raise

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "_dispatch_seeds", new=no_dispatch),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
            patch.object(h, "_declined_safe", new=forever),
            patch.object(h, "_scip_stale", new=stale),
        ):
            try:
                await asyncio.wait_for(
                    h.handle_code_callers(
                        CodeCallersInput(name="DB::Foo::bar", include_ambiguous=False)
                    ),
                    timeout=0.2,
                )
            except TimeoutError:
                pass
            await asyncio.sleep(0)
            return stale_cancelled.is_set()

    assert asyncio.run(go()) is True
