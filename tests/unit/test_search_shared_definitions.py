"""memory.search leaves out a shared definition whose namespace-wide source is gone or
labelled medium or high, reads nothing for a search with no shared definition hit, and
fails closed."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app import search_pages
from server.memory.app.search import handler, output, pg_validation
from server.memory.app.search.shared_definitions import without_withheld_definitions
from server.memory.store import MemoryRow
from server.observability import degradations


def _hit(key: str, agent: str = "_shared", namespace: str = "") -> Any:
    return SimpleNamespace(memory_key=key, _row_identity=(agent, "-", "-"), namespace=namespace)


def _head(key: str, sensitivity: str, namespace: str = "n") -> MemoryRow:
    return MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace=namespace,
        agent="-",
        user_id="-",
        session_id="-",
        memory_key=key,
        memory_type="definition",
        value={"term": key},
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        expires_at=None,
        sensitivity=sensitivity,
    )


async def _fresh(_org: str, _ns: str, hits: list[Any]) -> list[bool]:
    """The authority's stale check, finding every hit still served."""
    return [False] * len(hits)


def _parked(key: str, agent: str) -> dict[str, Any]:
    """A hit as a parked page stores it."""
    hit = _MemorySearchHit(
        memory_key=key, memory_type="definition", content_excerpt="x", distance=0.1, similarity=0.9
    )
    hit._row_identity = (agent, "-", "-")
    return search_pages._parked(hit)


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Each namespace's namespace-wide heads, and the reads made."""
    state: dict[str, Any] = {"heads": {}, "reads": [], "fail": False}

    async def heads(identity: Any, keys: list[str], **kw: Any) -> list[Any]:
        if state["fail"]:
            raise RuntimeError("the authority did not answer")
        ident = (identity.agent, identity.user_id, identity.session_id)
        state["reads"].append((identity.namespace, keys, ident, kw["scope"]))
        held = state["heads"].get(identity.namespace, {})
        return [held[k] for k in keys if k in held]

    monkeypatch.setattr(memory_repo, "get_many_by_keys", heads)
    return state


async def _keys(hits: list[Any]) -> list[str]:
    return [h.memory_key for h in await without_withheld_definitions("o", "n", hits)]


@pytest.mark.parametrize("level", ["medium", "high", "MEDIUM"])
async def test_a_shared_definition_with_a_sensitive_source_is_left_out(
    sources: dict[str, Any], level: str
) -> None:
    sources["heads"] = {"n": {"definition/arr": _head("definition/arr", level)}}
    hits = [_hit("definition/arr"), _hit("note"), _hit("definition/arr", agent="bot")]
    # The writer's own row under the key is not the shared copy.
    assert await _keys(hits) == ["note", "definition/arr"]
    # Read from the namespace-wide identity, across the namespace.
    assert sources["reads"] == [("n", ["definition/arr"], ("-", "-", "-"), WHOLE_NAMESPACE)]


@pytest.mark.parametrize("level", ["", "low"])
async def test_a_shared_definition_with_a_clear_source_stays(
    sources: dict[str, Any], level: str
) -> None:
    sources["heads"] = {"n": {"definition/arr": _head("definition/arr", level)}}
    assert await _keys([_hit("definition/arr")]) == ["definition/arr"]


async def test_a_shared_definition_whose_source_is_gone_is_left_out(
    sources: dict[str, Any],
) -> None:
    # Deleted, or tombstoned by the expiry sweep: no label says the copy may be served.
    assert await _keys([_hit("definition/arr"), _hit("note")]) == ["note"]


async def test_a_search_with_no_shared_definition_reads_nothing(sources: dict[str, Any]) -> None:
    hits = [_hit("note"), _hit("definition/arr", agent="bot"), _hit("arr", agent="_shared")]
    kept = await without_withheld_definitions("o", "n", hits)
    # The same list: nothing was read or reordered.
    assert kept is hits
    assert sources["reads"] == []


async def test_each_namespace_reads_its_own_sources(sources: dict[str, Any]) -> None:
    sources["heads"] = {
        "m": {"definition/arr": _head("definition/arr", "high", "m")},
        "n": {"definition/arr": _head("definition/arr", "")},
    }
    hits = [_hit("definition/arr"), _hit("definition/arr", namespace="m")]
    kept = await without_withheld_definitions("o", "n", hits)
    assert [h.namespace for h in kept] == [""]
    assert sorted(r[:2] for r in sources["reads"]) == [
        ("m", ["definition/arr"]),
        ("n", ["definition/arr"]),
    ]


async def test_an_unread_source_withholds_every_shared_definition(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    noted: list[tuple[str, str]] = []
    monkeypatch.setattr(degradations, "note", lambda p, e, *_a, **_k: noted.append((p, e)))
    sources["fail"] = True
    assert await _keys([_hit("definition/arr"), _hit("note")]) == ["note"]
    assert noted == [("memory.search", "shared_definitions_withheld")]


async def test_the_check_runs_after_the_last_pool_and_before_fusion(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared definition the key-match pool adds, the last pool before fusion, never
    reaches fusion when its source is labelled medium."""
    sources["heads"] = {"n": {"definition/arr": _head("definition/arr", "medium")}}
    fused: list[list[str]] = []

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def key_match(ctx: Any) -> None:
        ctx.all_hits.append(_hit("definition/arr"))

    async def staged(_ctx: Any, _name: str, coro: Any, *, active: bool) -> None:
        await coro

    for name in (
        "_run_vector_legs",
        "_run_sub_queries",
        "_run_set_expansion",
        "_run_anchor_pool",
        "_apply_mention_boost",
        "_run_external_rerank",
        "_assemble_final_hits",
        "_maybe_iterative_retry",
    ):
        monkeypatch.setattr(handler, name, nothing)
    monkeypatch.setattr(handler, "_staged", staged)
    monkeypatch.setattr(handler, "key_match_enabled", lambda _input: True)
    monkeypatch.setattr(handler, "_run_key_match_pool", key_match)
    monkeypatch.setattr(
        handler,
        "_apply_global_fusion",
        lambda ctx: fused.append([h.memory_key for h in ctx.all_hits]),
    )
    monkeypatch.setattr(handler, "_apply_chronological_sort", lambda _ctx: None)
    monkeypatch.setattr(handler, "_dedup_redundant_chunks", lambda hits: hits)
    ctx = SimpleNamespace(
        input=SimpleNamespace(org="o", namespace="n", recipe=""),
        actual_work={},
        clock=SimpleNamespace(mark=lambda *_a: None),
        all_hits=[_hit("note")],
        sub_queries=[],
        set_expansion_phrases=[],
        effective_entity_filter=None,
        resolved_dates=None,
    )
    await handler._run_retrieval_stages(ctx, None, None, None, None)  # type: ignore[arg-type]
    assert fused == [["note"]]
    assert ctx.actual_work["shared_definitions_withheld"] == 1


async def test_a_parked_page_withholds_a_definition_relabelled_since_page_one(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page parked and read back as a page stores it, under the default authority: the
    row's identity rides off the wire, so the check tells the shared copy apart."""
    sources["heads"] = {"n": {"definition/arr": _head("definition/arr", "medium")}}

    monkeypatch.setattr(pg_validation, "stale_flags", _fresh)
    rows = [_parked("note", "bot"), _parked("definition/arr", "_shared"), _parked("later", "bot")]
    hits, next_offset, stale = await output._parked_slice(
        SimpleNamespace(org="o", namespace="n", top_k=2), {"hits": rows}, 0, {""}
    )
    assert [h.memory_key for h in hits] == ["note", "later"]
    # Withheld, not stale: the authority still serves it.
    assert (next_offset, stale) == (-1, 0)


async def test_a_parked_page_fails_closed_when_a_source_is_not_read(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    noted: list[str] = []
    monkeypatch.setattr(degradations, "note", lambda p, e, *_a, **_k: noted.append(f"{p}: {e}"))

    monkeypatch.setattr(pg_validation, "stale_flags", _fresh)
    sources["fail"] = True
    rows = [_parked("note", "bot"), _parked("definition/arr", "_shared")]
    hits, _next, stale = await output._parked_slice(
        SimpleNamespace(org="o", namespace="n", top_k=2), {"hits": rows}, 0, {""}
    )
    assert [h.memory_key for h in hits] == ["note"] and stale == 0
    assert noted == ["memory.search: shared_definitions_withheld"]


async def test_a_parked_page_answers_with_the_withheld_note_on_degraded(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def page(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"hits": [_parked("note", "bot"), _parked("definition/arr", "_shared")]}

    async def readable(*_a: Any) -> set[str]:
        return {""}

    monkeypatch.setattr(search_pages, "load_page", page)
    monkeypatch.setattr(output, "_readable_namespaces", readable)
    monkeypatch.setattr(pg_validation, "stale_flags", _fresh)
    sources["fail"] = True
    degradations.begin()
    out = await output._serve_parked_page(
        MemorySearchInput(org="o", namespace="n", query="q", page="page/abc@0", top_k=2)
    )
    assert [h.memory_key for h in out.results] == ["note"]
    assert [d.split(" (")[0] for d in out.degraded] == [
        "memory.search: shared_definitions_withheld"
    ]


async def test_a_parked_page_notes_a_withheld_read_once_across_its_windows(
    sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg_validation, "stale_flags", _fresh)
    sources["fail"] = True
    degradations.begin()
    # The first window comes up short, so a refill window reads a second definition.
    rows = [
        _parked("definition/a", "_shared"),
        _parked("note", "bot"),
        _parked("definition/b", "_shared"),
    ]
    hits, _next, _stale = await output._parked_slice(
        SimpleNamespace(org="o", namespace="n", top_k=2), {"hits": rows}, 0, {""}
    )
    assert [h.memory_key for h in hits] == ["note"]
    assert [d.split(" (")[0] for d in degradations.current()] == [
        "memory.search: shared_definitions_withheld"
    ]
