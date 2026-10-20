"""memory.search hands its read identity to the derived-fact aggregate and the session-digest
leg, so an own_user / own_session read's aggregate and digests leave other users' and other
sessions' rows out (the services apply the bound; see test_derived_facts_plane and
test_session_summary_retrieval)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import computed, side_legs

# derived_facts rows of two users of one namespace
_ROWS = [
    {"user_id": "u1", "session_id": "s1", "value": 2.0},
    {"user_id": "u1", "session_id": "s2", "value": 3.0},
    {"user_id": "u2", "session_id": "s9", "value": 100.0},
]


def _matching(kw: dict[str, Any]) -> list[dict[str, Any]]:
    """What the service's identity WHERE keeps (memory/app/retrieval/derived.py)."""
    rows = _ROWS
    if kw.get("user_id") is not None:
        rows = [r for r in rows if r["user_id"] == kw["user_id"]]
    if kw.get("session_id") is not None:
        rows = [r for r in rows if r["session_id"] == kw["session_id"]]
    if kw.get("session_ids"):
        rows = [r for r in rows if r["session_id"] in kw["session_ids"]]
    return rows


def _ctx(inp: MemorySearchInput) -> Any:
    return SimpleNamespace(
        input=inp,
        derived_aggregate_cache={},
        query_shape="aggregational",
        stated_value_update=False,
        parser_derived_intent={"op": "sum", "subject": "user", "predicate": "spent"},
        search_budget=SimpleNamespace(note_llm_call=lambda *_a: None),
        fast_posture=False,
    )


@pytest.fixture
def aggregate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import server.memory.app.retrieval as ms

    calls: list[dict[str, Any]] = []

    async def _fetch(_ch: Any, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        rows = _matching(kw)
        return {"value": sum(r["value"] for r in rows), "count": len(rows)}

    monkeypatch.setattr(ms, "fetch_derived_aggregate", _fetch)
    monkeypatch.setattr(computed, "get_client", lambda: None)
    return calls


async def test_an_own_user_aggregate_counts_only_that_users_rows(
    aggregate: list[dict[str, Any]],
) -> None:
    inp = MemorySearchInput(
        org="o", namespace="n", agent="a", query="q", user_id="u1", read_scope="own_user"
    )
    out = await computed._maybe_derived_aggregate(_ctx(inp))
    assert out is not None and out["value"] == 5.0 and out["count"] == 2
    (kw,) = aggregate
    assert (kw["agent"], kw["user_id"], kw["session_id"], kw["session_ids"]) == (
        "a",
        "u1",
        None,
        [],
    )


async def test_an_own_session_aggregate_keeps_to_the_session(
    aggregate: list[dict[str, Any]],
) -> None:
    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="*",
        query="q",
        user_id="u1",
        session_id="s2",
        read_scope="own_session",
    )
    out = await computed._maybe_derived_aggregate(_ctx(inp))
    assert out is not None and out["value"] == 3.0
    assert aggregate[0]["agent"] == "*"


async def test_a_shared_read_is_not_bound_to_the_callers_agent(
    aggregate: list[dict[str, Any]],
) -> None:
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q", scope="shared")
    await computed._maybe_derived_aggregate(_ctx(inp))
    assert aggregate[0]["agent"] == "*"


async def test_the_digest_leg_gets_the_session_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.retrieval as ms

    seen: list[dict[str, Any]] = []

    async def _summaries(_ch: Any, **kw: Any) -> list[Any]:
        seen.append(kw)
        return []

    monkeypatch.setattr(ms, "fetch_session_summaries", _summaries)
    monkeypatch.setattr(side_legs, "get_client", lambda: None)
    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        query="q",
        user_id="u1",
        session_id="s1",
        session_ids=["s1", "s2"],
        read_scope="own_session",
    )
    ctx = SimpleNamespace(
        input=inp,
        embedder=SimpleNamespace(model_name="m"),
        namespaces=["n"],
        query_vec=[0.0],
    )
    assert await side_legs._launch_summary_leg(ctx) == []  # type: ignore[arg-type]
    (kw,) = seen
    assert (kw["user_id"], kw["session_id"], kw["session_ids"]) == ("u1", "s1", ["s1", "s2"])
