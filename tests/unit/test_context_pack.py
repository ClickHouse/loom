from typing import Any

import pytest

from server.knowledge.api.models_knowledge import _SearchHit
from server.memory.domain.context_layout import _ordered_section_names


def test_ordered_section_names_honors_valid_custom_order_and_appends_rest() -> None:
    assert _ordered_section_names(["memories", "profile", "memories", "unknown"]) == [
        "memories",
        "profile",
        "working_memory",
        "namespace_health",
        "namespace_guide",
        "mentor",
        "memory_stack",
        "procedures",
        "decisions",
        "knowledge",
        "conversations",
        "runs",
    ]


def test_filter_sections_keeps_header_and_named_sections_in_order() -> None:
    from server.code.app.index.packs import SECTION_HEADERS, filter_sections

    pack = "\n".join(
        [
            "# PR #1: title",
            "merge commit: abc · files changed: 2 · symbols touched: 1",
            "",
            SECTION_HEADERS["symbols"],
            "- DB::f  ← called by g",
            "",
            SECTION_HEADERS["co_change"],
            "- src/a.cpp",
            "",
            SECTION_HEADERS["tests"],
            "- tests/t.sql",
            "",
            SECTION_HEADERS["prs"],
            "- #2 fix",
            "",
            SECTION_HEADERS["files"],
            "- src/a.cpp",
        ]
    )
    out = filter_sections(pack, ["tests", "prs"])
    assert out.startswith("# PR #1: title\nmerge commit")
    assert SECTION_HEADERS["tests"] in out and SECTION_HEADERS["prs"] in out
    assert SECTION_HEADERS["symbols"] not in out and "← called by" not in out
    assert SECTION_HEADERS["co_change"] not in out and SECTION_HEADERS["files"] not in out
    assert filter_sections(pack, []) == pack
    assert filter_sections(pack, ["nonsense"]).count("## ") == 0  # header only


def test_context_pack_input_validates_section_names() -> None:
    import pytest
    from pydantic import ValidationError

    from server.code.api.models_context import CodeContextPackInput

    assert CodeContextPackInput(pr_number=1, sections=["tests", "prs"]).sections == ["tests", "prs"]
    with pytest.raises(ValidationError):
        CodeContextPackInput(pr_number=1, sections=["impact"])


def test_context_pack_section_order_is_independent_of_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seven sections are gathered concurrently. The dicts on the wire
    must still be keyed in block order, so two identical calls serialise
    byte-identical: provider prompt caches (and KV-cache layers in front of a
    self-hosted engine) match on a byte-identical prefix, and a reordered key
    is a miss on everything after it."""
    import asyncio
    from types import SimpleNamespace as NS

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack
    from server.memory.domain.context_layout import _DEFAULT_SECTION_ORDER

    # First block in the default order finishes LAST.
    # `runs` is opt-in and not requested here, so it is not built;
    # `namespace_health` renders only for an unhealthy namespace, `namespace_guide`
    # only for one with a guide.
    skipped = ("runs", "namespace_health", "namespace_guide", "mentor")
    order = [n for n in _DEFAULT_SECTION_ORDER if n not in skipped]
    delay = {name: (len(order) - i) * 0.01 for i, name in enumerate(order)}

    def _stub(name: str, out: Any) -> Any:
        async def _h(*a: Any, **k: Any) -> Any:
            await asyncio.sleep(delay[name])
            return out

        return _h

    monkeypatch.setattr(
        "server.memory.app.working_memory.handle_working_memory_render",
        _stub(
            "working_memory",
            NS(rendered="<wm>x</wm>", blocks=[NS(block_key="b1", content="x")], block_count=1),
        ),
    )
    monkeypatch.setattr(
        "server.memory.app.stack.handle_memory_stack",
        _stub(
            "memory_stack",
            NS(
                entries=[NS(section="facts", memory_key="k", memory_type="fact", value="v")],
                tokens_used=3,
                tokens_budget=100,
                truncated=False,
            ),
        ),
    )
    monkeypatch.setattr(
        "server.memory.app.profile.handle_profile_render",
        _stub("profile", NS(rendered="<profile>p</profile>", labels=["p"], section_count=1)),
    )
    monkeypatch.setattr(
        "server.memory.app.search.handle_memory_search",
        _stub(
            "memories",
            NS(
                results=[
                    NS(
                        namespace="ns",
                        memory_key="m1",
                        content_excerpt="hello",
                        source="fact",
                        source_resource_id="",
                        rank_score=0.9,
                        temporal_anchor="",
                        valid_at="",
                        memory_type="fact",
                    ),
                    NS(
                        namespace="ns",
                        memory_key="d1",
                        content_excerpt="chose clickhouse",
                        source="fact",
                        source_resource_id="",
                        rank_score=0.8,
                        temporal_anchor="",
                        valid_at="",
                        memory_type="decision",
                    ),
                ],
                abstain=False,
                abstain_reason="",
                retrieval_confidence=0.9,
                retrieval_risk="low",
                route_agreement=1.0,
            ),
        ),
    )

    async def _records(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(delay["decisions"])
        return {"d1": {"scenario": "pick a store", "outcome": "clickhouse", "stage": "decided"}}

    monkeypatch.setattr("server.memory.app.decision._decision_records", _records)
    monkeypatch.setattr(
        "server.memory.app.procedure.handle_procedure_match",
        _stub(
            "procedures",
            NS(
                matches=[
                    NS(
                        steps=["a", "b"],
                        namespace="ns",
                        memory_key="pr",
                        name="deploy",
                        situation="release",
                    )
                ]
            ),
        ),
    )
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
        _stub(
            "knowledge",
            NS(
                results=[
                    _SearchHit(
                        namespace="ns",
                        source_uri="doc://1",
                        content="kn",
                        score=0.5,
                        chunk_idx=0,
                        tags=[],
                        distance=0.5,
                        similarity=0.5,
                    )
                ]
            ),
        ),
    )
    monkeypatch.setattr(
        "server.knowledge.app.conversations.handle_conversation_search",
        _stub("conversations", NS(hits=[NS(thread_id="t1", msg_id="m1", content_excerpt="chat")])),
    )

    inp = ContextPackInput(
        org="o",
        namespace="ns",
        agent="a",
        query="q",
        situation="release",
        include_knowledge=True,
        include_conversations=True,
        top_k=5,
    )
    first = asyncio.run(handle_context_pack(inp))
    second = asyncio.run(handle_context_pack(inp))

    assert list(first.sections) == order
    assert list(first.section_meta) == order
    assert first.model_dump_json() == second.model_dump_json()

    # A caller-supplied block order is honoured by the dict keys as well.
    custom = asyncio.run(
        handle_context_pack(inp.model_copy(update={"block_order": ["memories", "profile"]}))
    )
    assert list(custom.sections)[:2] == ["memories", "profile"]
    assert list(custom.section_meta)[:2] == ["memories", "profile"]


def test_context_pack_cites_the_knowledge_section_when_the_hit_carries_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from types import SimpleNamespace as NS

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.search.handle_memory_search",
        "server.memory.app.procedure.handle_procedure_match",
        "server.knowledge.app.conversations.handle_conversation_search",
    ):
        monkeypatch.setattr(target, _dead)

    async def _knowledge(*a: Any, **k: Any) -> Any:
        return NS(
            results=[
                _SearchHit(
                    namespace="ns",
                    chunk_idx=0,
                    tags=[],
                    distance=0.1,
                    similarity=0.9,
                    source_uri="kb://handbook",
                    content="deploy on tuesdays",
                    score=0.9,
                    section="Ops > Deploys",
                ),
                _SearchHit(
                    namespace="ns",
                    chunk_idx=0,
                    tags=[],
                    distance=0.5,
                    similarity=0.5,
                    source_uri="kb://flat",
                    content="no headings here",
                    score=0.5,
                    section="",
                ),
            ]
        )

    monkeypatch.setattr("server.knowledge.app.knowledge_search.handle_knowledge_search", _knowledge)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="when do we deploy",
                include_knowledge=True,
            )
        )
    )
    cites = out.section_meta["knowledge"]["citations"]
    assert "knowledge:ns/kb://handbook#Ops > Deploys" in cites
    assert "knowledge:ns/kb://flat" in cites


def _stub_all_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every section returns content; the query-shaped ones echo the query so
    a different question changes their bytes and only theirs."""
    from types import SimpleNamespace as NS

    def _stub(out_for: Any) -> Any:
        async def _h(inp: Any = None, *a: Any, **k: Any) -> Any:
            return out_for(inp) if callable(out_for) else out_for

        return _h

    monkeypatch.setattr(
        "server.memory.app.working_memory.handle_working_memory_render",
        _stub(
            NS(rendered="<wm>note</wm>", blocks=[NS(block_key="b1", content="x")], block_count=1)
        ),
    )
    monkeypatch.setattr(
        "server.memory.app.profile.handle_profile_render",
        _stub(NS(rendered="<profile>p</profile>", labels=["p"], section_count=1)),
    )
    monkeypatch.setattr(
        "server.memory.app.stack.handle_memory_stack",
        _stub(
            lambda inp: NS(
                entries=[
                    NS(
                        section="facts",
                        memory_key="k",
                        memory_type="fact",
                        value=f"stack for {inp.seed_query or 'no seed'}",
                    )
                ],
                tokens_used=3,
                tokens_budget=100,
                truncated=False,
            )
        ),
    )
    monkeypatch.setattr(
        "server.memory.app.search.handle_memory_search",
        _stub(
            lambda inp: NS(
                results=[
                    NS(
                        namespace="ns",
                        memory_key="m1",
                        content_excerpt=f"hit for {inp.query}",
                        source="fact",
                        source_resource_id="",
                        rank_score=0.9,
                        temporal_anchor="",
                        valid_at="",
                        memory_type="fact",
                    )
                ],
                abstain=False,
                abstain_reason="",
                retrieval_confidence=0.9,
                retrieval_risk="low",
                route_agreement=1.0,
            )
        ),
    )
    monkeypatch.setattr(
        "server.memory.app.procedure.handle_procedure_match",
        _stub(
            lambda inp: NS(
                matches=[
                    NS(
                        steps=["a"],
                        namespace="ns",
                        memory_key="pr",
                        name="deploy",
                        situation=inp.situation,
                    )
                ]
            )
        ),
    )


def test_context_pack_stable_prefix_is_byte_identical_across_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pack leads with what does not depend on the
    question, and says where that part ends. Two different questions from
    the same identity share `rendered[:stable_prefix_chars]` byte for byte
    and report the same `stable_hash`; the query-shaped tail differs."""
    import asyncio

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    _stub_all_sections(monkeypatch)
    base = dict(org="o", namespace="ns", agent="a", situation="release", top_k=5)
    a = asyncio.run(handle_context_pack(ContextPackInput(query="how do I deploy?", **base)))
    b = asyncio.run(handle_context_pack(ContextPackInput(query="what broke last night?", **base)))

    # working_memory and profile lead; memory_stack is seeded (situation) so it is volatile.
    assert a.stable_sections == ["working_memory", "profile"]
    assert list(a.sections)[:3] == ["working_memory", "profile", "memory_stack"]
    assert a.stable_prefix_chars > 0
    assert a.rendered[: a.stable_prefix_chars] == b.rendered[: b.stable_prefix_chars]
    assert a.stable_hash == b.stable_hash and len(a.stable_hash) == 64
    assert a.rendered != b.rendered
    # The prefix ends exactly at the boundary before the first volatile section.
    assert a.rendered[a.stable_prefix_chars :].startswith("<memory_stack>")
    assert a.rendered[: a.stable_prefix_chars].endswith("</profile>\n\n")


def test_context_pack_startup_without_a_query_is_entirely_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    _stub_all_sections(monkeypatch)
    # Zero-query startup recall is pins + recency, not a search.
    monkeypatch.setattr(
        "server.memory.store.list_by_namespace",
        _async_rows([]),
    )
    out = asyncio.run(
        handle_context_pack(ContextPackInput(org="o", namespace="ns", agent="a", mode="startup"))
    )
    assert out.stable_sections == ["working_memory", "profile", "memory_stack"]
    assert out.stable_prefix_chars == len(out.rendered)
    assert out.stable_hash


def test_context_pack_volatile_first_block_order_has_no_stable_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    _stub_all_sections(monkeypatch)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="q",
                block_order=["memories", "profile"],
            )
        )
    )
    assert out.stable_sections == []
    assert out.stable_prefix_chars == 0 and out.stable_hash == ""


def _async_rows(rows: list[Any]) -> Any:
    async def _h(*a: Any, **k: Any) -> list[Any]:
        return rows

    return _h


def test_context_pack_marks_a_conflicted_memory_on_the_query_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row state rides the hit's `status`; the query path marks a conflicted
    memory the way startup recall does."""
    import asyncio

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.procedure.handle_procedure_match",
        "server.knowledge.app.conversations.handle_conversation_search",
        "server.knowledge.app.knowledge_search.handle_knowledge_search",
    ):
        monkeypatch.setattr(target, _dead)

    from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit

    def _hit(key: str, status: str) -> Any:
        return _MemorySearchHit(
            memory_key=key,
            memory_type="semantic",
            content_excerpt=f"fact {key}",
            distance=0.2,
            similarity=0.8,
            status=status,
            namespace="ns",
        )

    async def _search(*a: Any, **k: Any) -> Any:
        return MemorySearchOutput.model_construct(
            results=[_hit("a", "conflicted"), _hit("b", "active")]
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    out = asyncio.run(
        handle_context_pack(ContextPackInput(org="o", namespace="ns", agent="a", query="q"))
    )
    lines = [ln for ln in out.rendered.splitlines() if "] fact a" in ln or "] fact b" in ln]
    assert any("] fact a" in ln and "conflict=true" in ln for ln in lines), lines
    assert not any("] fact b" in ln and "conflict=true" in ln for ln in lines), lines
