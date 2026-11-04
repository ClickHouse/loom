"""context.pack mode=startup does a zero-query pins+recency recall — not a
paid semantic search. A session bootstrap should get useful memories from
cheap recency + pins without an embedding round-trip; real queries still go
through search (covered elsewhere)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest


def _row(key: str, value: str, *, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        value=value,
        confidence=1.0,
        pinned=False,
        valid_at=None,
        created_at=datetime(2026, 1, 1),
        status=status,
    )


def test_startup_pack_zero_query_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    calls = {"pinned": 0, "recent": 0, "search": 0}

    async def _list(
        ident: Any,
        *,
        pinned_only: bool = False,
        order_by: str = "created_at_desc",
        limit: int = 200,
        **kw: Any,
    ) -> list[Any]:
        if pinned_only:
            calls["pinned"] += 1
            return [_row("pin1", "Pinned: dedupe with FINAL")]
        calls["recent"] += 1
        # rec list re-includes pin1 to prove dedup keeps it once.
        return [_row("rec1", "Recent A"), _row("pin1", "dup"), _row("rec2", "Recent B")]

    monkeypatch.setattr(memory_repo, "list_by_namespace", _list)

    # No ClickHouse in a unit test — make the OTHER sections fail-soft fast.
    class _DeadCH:
        def __getattr__(self, _n: str) -> Any:
            async def _boom(*a: Any, **k: Any) -> Any:
                raise RuntimeError("no CH in unit test")

            return _boom

    monkeypatch.setattr("server.core.ch._ch", _DeadCH())

    async def _no_search(*a: Any, **k: Any) -> Any:  # must NOT be reached
        calls["search"] += 1
        raise AssertionError("startup recall must not call memory.search")

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _no_search)

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="",
                mode="startup",
                top_k=5,
            )
        )
    )

    assert out.sections.get("memories"), "startup pack recalled no memories"
    meta = out.section_meta["memories"]
    assert meta["recall_mode"] == "startup_pins_recency"
    assert meta["query_free"] is True
    assert calls["pinned"] == 1 and calls["recent"] == 1
    assert calls["search"] == 0, "zero-query startup must not run a paid search"
    assert "Pinned" in out.sections["memories"]  # pin surfaced, recency-merged


@pytest.mark.parametrize(
    ("user_id", "session_id", "read"),
    [("-", "-", ("*", "*")), ("u1", "-", ("u1", "*")), ("u1", "s1", ("u1", "s1"))],
)
def test_startup_recall_reads_the_identity_sentinel_as_unset(
    monkeypatch: pytest.MonkeyPatch, user_id: str, session_id: str, read: tuple[str, str]
) -> None:
    """`Agent.call` stamps user_id / session_id "-"; like the pack's query path, the
    startup recall reads it as no filter, not as the rows written with no user."""
    from server.memory import store as memory_repo
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    seen: list[tuple[str, str]] = []

    async def _list(ident: Any, **kw: Any) -> list[Any]:
        seen.append((ident.user_id, ident.session_id))
        return []

    monkeypatch.setattr(memory_repo, "list_by_namespace", _list)

    class _DeadCH:
        def __getattr__(self, _n: str) -> Any:
            async def _boom(*a: Any, **k: Any) -> Any:
                raise RuntimeError("no CH")

            return _boom

    monkeypatch.setattr("server.core.ch._ch", _DeadCH())
    asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                user_id=user_id,
                session_id=session_id,
                query="",
                mode="startup",
                top_k=5,
            )
        )
    )
    assert seen and set(seen) == {read}


def test_startup_recall_leaves_transcripts_out_before_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every session's tool-call transcripts would fill the k*2 newest rows once the
    session axis is open, so the recall leaves them (and the types an unfiltered search
    leaves out) out in the statement, not after it."""
    from server.memory import store as memory_repo
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack
    from server.memory.domain.memory_types import excluded_from_unfiltered_search

    seen: list[dict[str, Any]] = []

    async def _list(ident: Any, **kw: Any) -> list[Any]:
        seen.append(kw)
        return []

    monkeypatch.setattr(memory_repo, "list_by_namespace", _list)

    class _DeadCH:
        def __getattr__(self, _n: str) -> Any:
            async def _boom(*a: Any, **k: Any) -> Any:
                raise RuntimeError("no CH")

            return _boom

    monkeypatch.setattr("server.core.ch._ch", _DeadCH())
    for session_id, excluded in (("-", list(excluded_from_unfiltered_search())), ("s1", [])):
        seen.clear()
        asyncio.run(
            handle_context_pack(
                ContextPackInput(
                    org="o",
                    namespace="ns",
                    agent="a",
                    session_id=session_id,
                    query="",
                    mode="startup",
                    top_k=5,
                )
            )
        )
        assert seen and all(kw["exclude_transcripts"] for kw in seen)
        assert all(list(kw["exclude_memory_types"]) == excluded for kw in seen)


def test_task_mode_without_query_recalls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only startup is zero-query-friendly; other modes stay query-driven."""
    from server.memory import store as memory_repo
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    called = {"list": 0}

    async def _list(*a: Any, **k: Any) -> list[Any]:
        called["list"] += 1
        return [_row("x", "should not be pulled")]

    monkeypatch.setattr(memory_repo, "list_by_namespace", _list)

    class _DeadCH:
        def __getattr__(self, _n: str) -> Any:
            async def _boom(*a: Any, **k: Any) -> Any:
                raise RuntimeError("no CH")

            return _boom

    monkeypatch.setattr("server.core.ch._ch", _DeadCH())

    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="",
                mode="task",
                top_k=5,
            )
        )
    )
    assert "memories" not in out.sections
    assert called["list"] == 0  # no recall fan-out for non-startup no-query
