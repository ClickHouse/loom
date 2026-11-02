"""What a read returns: `fields` on memory.get / bulk_get /
list, `tags` on memory.list and memory.ask, and the large-k defaults on
memory.search (excerpt bodies, no per-hit explanation unless `explain`).

Every knob is opt-in and the payload with nothing set is byte-identical to
what every existing client saw — the tests here pin that first.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.core.tag_sql import TagFilter, tag_where
from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_bulk import MemoryBulkGetInput
from server.memory.api.models_crud import (
    MemoryGetInput,
    MemoryGetOutput,
    MemoryListInput,
    MemoryListOutput,
    _MemoryListEntry,
)
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.store.rows import MemoryRow
from server.operations import read_projection

# ------------------------------------------------------------- defaults


def test_new_input_fields_default_off() -> None:
    assert MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k").fields == []
    assert MemoryListInput(org="o", namespace="n", agent="a").fields == []
    assert MemoryListInput(org="o", namespace="n", agent="a").tags == []
    assert MemoryBulkGetInput(org="o", namespace="n", agent="a", memory_keys=["k"]).fields == []
    assert MemorySearchInput(org="o", namespace="n", query="q").explain is None
    ask = MemoryAskInput(org="o", namespace="n", agent="a", query="q")
    assert ask.tags == [] and ask.tags_soft is False and ask.tag_boost is None


def _get_out(**kw: Any) -> MemoryGetOutput:
    return MemoryGetOutput(
        found=True,
        memory_id="m1",
        value={"a": 1},
        memory_type="semantic",
        tags=["kind:lesson", "subsystem:Storages"],
        status="active",
        recall_count=3,
        created_at=datetime(2026, 9, 1),
        **kw,
    )


def test_get_payload_without_fields_is_unchanged() -> None:
    plain = _get_out().model_dump(mode="json")
    # every declared field is on the wire (`canonical_key` rides only on a
    # duplicate row and `resolved_from` only on a followed read: the two
    # exceptions)
    assert set(plain) == set(MemoryGetOutput.model_fields) - {"canonical_key", "resolved_from"}
    assert "_projection" not in plain


def test_get_projection_keeps_found_and_requested_only() -> None:
    out = _get_out()
    out._projection = read_projection.resolve(
        MemoryGetOutput, ["value", "tags"], always=read_projection.GET_ALWAYS
    )
    dumped = out.model_dump(mode="json")
    assert dumped == {
        "found": True,
        "unchanged": False,
        "value": {"a": 1},
        "tags": ["kind:lesson", "subsystem:Storages"],
    }
    # json.dumps parity: the projection survives the transport's mode="json" dump
    assert json.loads(json.dumps(dumped)) == dumped


def test_unknown_field_is_a_422_not_a_silent_drop() -> None:
    with pytest.raises(ValidationError) as ei:
        read_projection.resolve(
            MemoryGetOutput, ["value", "nope"], always=read_projection.GET_ALWAYS
        )
    assert ei.value.status_code == 422
    assert ei.value.extra["unknown_fields"] == ["nope"]


def test_empty_fields_means_no_projection() -> None:
    assert read_projection.resolve(MemoryGetOutput, [], always=read_projection.GET_ALWAYS) is None
    assert read_projection.resolve(MemoryGetOutput, None, always=read_projection.GET_ALWAYS) is None


def test_list_entry_projection_always_keeps_memory_key_and_sensitivity() -> None:
    e = _MemoryListEntry(
        memory_key="lesson_1",
        memory_type="semantic",
        confidence=1.0,
        version=1,
        created_at=datetime(2026, 9, 1),
        tags=["kind:false_positive"],
        value="never flag X",
    )
    assert set(e.model_dump(mode="json")) == set(_MemoryListEntry.model_fields) - {"canonical_key"}
    e._projection = read_projection.resolve(
        _MemoryListEntry, ["value", "tags"], always=read_projection.LIST_ENTRY_ALWAYS
    )
    # `sensitivity` too: a reader never names it, so it never 422s on an older replica
    assert e.model_dump(mode="json") == {
        "memory_key": "lesson_1",
        "sensitivity": "",
        "tags": ["kind:false_positive"],
        "value": "never flag X",
    }
    # through the envelope too
    out = MemoryListOutput(entries=[e], truncated=False)
    entry = out.model_dump(mode="json")["entries"][0]
    assert set(entry) == {"memory_key", "sensitivity", "tags", "value"}


# ------------------------------------------------------------- handlers


def _row(key: str, **kw: Any) -> MemoryRow:
    base = dict(
        memory_id="00000000-0000-0000-0000-000000000001",
        memory_key=key,
        value={"v": key},
        memory_type="semantic",
        confidence=0.7,
        version=5,
        created_at=datetime(2026, 9, 1),
        updated_at=None,
        expires_at=None,
        valid_at=None,
        valid_to=None,
        pinned=False,
        agent="clickgap",
        status="active",
        tags=["plane:finding", "subsystem:Types"],
        entities=[],
        char_limit=None,
        verified_by="",
        verified_at=None,
        verified_sha256="",
        sensitivity="",
        recall_count=2,
        last_recalled_at=None,
    )
    base.update(kw)
    return MemoryRow(org="o", namespace="ns", user_id="-", session_id="-", **base)


@pytest.mark.asyncio
async def test_handle_memory_get_projects_and_validates_before_the_read(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import handlers as H

    calls: list[str] = []

    async def fake_get_by_key(identity, key, **kw):
        calls.append(key)
        return _row(key)

    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)
    inp = MemoryGetInput(
        org="o",
        namespace="n",
        agent="clickgap",
        memory_key="finding_1",
        fields=["value", "tags", "status"],
    )
    out = await H.handle_memory_get(inp)
    dumped = out.model_dump(mode="json")
    assert dumped == {
        "found": True,
        "unchanged": False,
        "value": {"v": "finding_1"},
        "tags": ["plane:finding", "subsystem:Types"],
        "status": "active",
    }
    # in-process readers still see every attribute
    assert out.memory_id and out.version == 5

    calls.clear()
    with pytest.raises(ValidationError):
        await H.handle_memory_get(
            MemoryGetInput(
                org="o", namespace="n", agent="clickgap", memory_key="k", fields=["bogus"]
            )
        )
    assert calls == []  # rejected before any statement ran

    # unset: the same row, the whole payload
    full = await H.handle_memory_get(
        MemoryGetInput(org="o", namespace="n", agent="clickgap", memory_key="finding_1")
    )
    assert set(full.model_dump(mode="json")) == set(MemoryGetOutput.model_fields) - {
        "canonical_key",
        "resolved_from",
    }


@pytest.mark.asyncio
async def test_memory_get_reads_links_only_when_its_fields_show_code(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import code_link_reads
    from server.memory.app import handlers as H

    read: list[str] = []

    async def get_by_key(identity, key, **kw):
        return _row(key)

    async def links_of(org, memory):
        read.append(org)
        return []

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(code_link_reads, "links_of", links_of)
    for fields in (["value"], [], ["value", "code"]):
        await H.handle_memory_get(
            MemoryGetInput(org="o", namespace="n", agent="a", memory_key="k", fields=fields)
        )
    assert len(read) == 2


@pytest.mark.asyncio
async def test_handle_memory_bulk_get_projects_every_entry(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import query as Q

    async def fake_get_many(identity, keys, **kw):
        return [_row(k) for k in keys if k != "missing"]

    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    out = await Q.handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o",
            namespace="n",
            agent="clickgap",
            memory_keys=["a", "b", "missing"],
            fields=["value"],
        )
    )
    dumped = out.model_dump(mode="json")
    assert dumped["missing"] == ["missing"]
    assert dumped["results"] == {
        "a": {"found": True, "unchanged": False, "value": {"v": "a"}},
        "b": {"found": True, "unchanged": False, "value": {"v": "b"}},
    }


class _CH:
    """Records statements; answers the list reads with two rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if "FROM loom.memory FINAL" in sql and "count()" not in sql:
            return [
                {
                    # the full loom.memory column set the recency path hydrates,
                    # plus the score path's preview / counter columns
                    "memory_id": "00000000-0000-0000-0000-000000000001",
                    "org": "o",
                    "namespace": "n",
                    "agent": "clickgap",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "lesson_1",
                    "memory_type": "semantic",
                    "confidence": 0.7,
                    "version": 1,
                    "pinned": 0,
                    "created_at": datetime(2026, 9, 1),
                    "updated_at": None,
                    "expires_at": None,
                    "valid_at": None,
                    "tombstoned_at": None,
                    "status": "active",
                    "run_id": "-",
                    "source_trace": "",
                    "tags": ["kind:false_positive"],
                    "entities": [],
                    "value": '"never flag X"',
                    "value_truncated": 0,
                    "verified_by": "",
                    "verified_at": None,
                    "verified_sha256": "",
                    "content_sha256": "",
                    "actor": "",
                    "source_class": "",
                    "evidence_count": 1,
                    "recall_count": 4,
                    "last_recalled_at": None,
                }
            ]
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return {"n": 1}


# memory.list's tag predicate: both sides lowercased.
_ANY_CASE_TAGS = tag_where(TagFilter(("x",), "folded_any"), {})


@pytest.mark.asyncio
async def test_list_tags_filter_is_the_any_case_predicate_on_both_paths(monkeypatch) -> None:
    from server.memory.app import query as Q
    from server.memory.store import reads

    ch = _CH()
    monkeypatch.setattr(Q, "get_client", lambda: ch)
    monkeypatch.setattr(reads, "_default_client", lambda: ch)
    monkeypatch.setattr(Q, "_record_read_exposure", lambda *a, **k: None)
    monkeypatch.setattr(Q, "_cap_viewer_list_limit", lambda i: i)

    # recency path: list + count + today all carry the predicate
    await Q.handle_memory_list(
        MemoryListInput(
            org="o",
            namespace="n",
            agent="clickgap",
            prefix="lesson_",
            limit=40,
            tags=["kind:false_positive", "kind:maintainer_preference"],
            fields=["value", "tags"],
        )
    )
    recency = [(s, p) for s, p in ch.calls if "FROM loom.memory FINAL" in s]
    assert len(recency) == 3
    for sql, params in recency:
        assert _ANY_CASE_TAGS in sql
        assert params["tags"] == ["kind:false_positive", "kind:maintainer_preference"]
        assert "PREWHERE" not in sql

    # score path
    ch.calls.clear()
    out = await Q.handle_memory_list(
        MemoryListInput(
            org="o",
            namespace="n",
            agent="clickgap",
            sort_by="score",
            limit=10,
            tags=["kind:false_positive"],
            fields=["value"],
        )
    )
    score = [(s, p) for s, p in ch.calls if "FROM loom.memory FINAL" in s]
    assert len(score) == 2 and all(_ANY_CASE_TAGS in s for s, _ in score)
    dumped = out.model_dump(mode="json")
    assert dumped["entries"] == [
        {"memory_key": "lesson_1", "value": "never flag X", "sensitivity": ""}
    ]

    # no tags, no fields: no predicate, the whole entry
    ch.calls.clear()
    out = await Q.handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="clickgap", limit=5)
    )
    assert all("hasAny" not in s for s, _ in ch.calls)
    assert set(out.model_dump(mode="json")["entries"][0]) == set(_MemoryListEntry.model_fields) - {
        "canonical_key"
    }


# ------------------------------------------------------------- search defaults


def _hit(key: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x" * 400,
        distance=0.2,
        similarity=0.8,
        explanation="dense leg, rank 1",
        score_components={"rank_score": 0.8, "similarity": 0.8},
    )


def _inp(**kw: Any) -> MemorySearchInput:
    return MemorySearchInput(org="o", namespace="n", query="q", **kw)


def test_large_k_unpinned_bodies_read_the_excerpt_tier(monkeypatch) -> None:
    from server.memory.app.search import request as S
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_excerpt_bodies_k", 15)
    assert S._apply_large_k_body_default(_inp(top_k=30)).bodies == "excerpt"
    assert S._apply_large_k_body_default(_inp(top_k=16)).bodies == "excerpt"
    # at or below the threshold: untouched
    assert S._apply_large_k_body_default(_inp(top_k=15)).bodies == "full"
    assert S._apply_large_k_body_default(_inp(top_k=5)).bodies == "full"
    # the caller pinned a tier (full included): untouched at any k
    assert S._apply_large_k_body_default(_inp(top_k=30, bodies="full")).bodies == "full"
    assert S._apply_large_k_body_default(_inp(top_k=30, bodies="unit")).bodies == "unit"
    # applied once: the copy marks bodies as set
    once = S._apply_large_k_body_default(_inp(top_k=30))
    assert S._apply_large_k_body_default(once) is once
    # 0 = never
    monkeypatch.setattr(settings, "memory_search_excerpt_bodies_k", 0)
    assert S._apply_large_k_body_default(_inp(top_k=150)).bodies == "full"


def test_explanation_leaves_the_wire_above_k_unless_explain(monkeypatch) -> None:
    from server.memory.app.search import request as S
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_excerpt_bodies_k", 15)

    def dumped(inp: MemorySearchInput) -> dict[str, Any]:
        hits = [_hit("a")]
        S.apply_explain_default(hits, inp)
        out = MemorySearchOutput(results=hits, embedding_model="m")
        return out.model_dump(mode="json")["results"][0]

    small = dumped(_inp(top_k=10))
    assert small["explanation"] == "dense leg, rank 1" and small["score_components"]
    large = dumped(_inp(top_k=30))
    assert "explanation" not in large and "score_components" not in large
    assert large["memory_key"] == "a" and large["rank_score"] == 0.0 or "rank_score" in large
    kept = dumped(_inp(top_k=30, explain=True))
    assert kept["explanation"] == "dense leg, rank 1"
    dropped_small = dumped(_inp(top_k=5, explain=False))
    assert "explanation" not in dropped_small
    # in-process readers keep the attribute
    hits = [_hit("a")]
    S.apply_explain_default(hits, _inp(top_k=30))
    assert hits[0].explanation == "dense leg, rank 1"


def test_wire_hit_without_elision_is_unchanged() -> None:
    d = _hit("a").model_dump(mode="json")
    assert d["explanation"] == "dense leg, rank 1" and "_elide" not in d


# ------------------------------------------------------------- ask


def test_ask_tags_ride_the_retrieval_leg_as_searchs_predicate() -> None:
    from server.memory.app.ask.handler import _body_kwargs, _tag_kwargs

    plain = MemoryAskInput(org="o", namespace="n", agent="a", query="q")
    assert _tag_kwargs(plain) == {}
    hard = MemoryAskInput(org="o", namespace="n", agent="a", query="q", tags=["subsystem:Storages"])
    assert _tag_kwargs(hard) == {"tags": ["subsystem:Storages"], "tags_soft": False}
    soft = MemoryAskInput(
        org="o",
        namespace="n",
        agent="a",
        query="q",
        tags=["subsystem:Storages"],
        tags_soft=True,
        tag_boost=0.5,
    )
    assert _tag_kwargs(soft) == {
        "tags": ["subsystem:Storages"],
        "tags_soft": True,
        "tag_boost": 0.5,
    }
    # the leg the ask builds accepts them
    MemorySearchInput(org="o", namespace="n", query="q", **_tag_kwargs(soft))
    # an ask never reads the excerpt tier: unset bodies pin "full" on the leg
    assert _body_kwargs(plain) == {"bodies": "full"}
    assert _body_kwargs(
        MemoryAskInput(org="o", namespace="n", agent="a", query="q", bodies="none")
    ) == {"bodies": "none"}


def test_context_pack_pins_full_bodies() -> None:
    import inspect

    from server.memory.app import context_pack as context

    assert 'bodies="full"' in inspect.getsource(context)
