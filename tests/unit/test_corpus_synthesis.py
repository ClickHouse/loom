"""Long-range understanding is a pass over the whole document."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain import corpus_synthesis as cs


def _c(idx: int, text: str, section: tuple[str, ...] = (), uri: str = "doc://a") -> cs.CorpusChunk:
    return cs.CorpusChunk(source_uri=uri, idx=idx, text=text, section_path=section)


def test_windows_respect_size_and_section_boundaries() -> None:
    chunks = [
        _c(0, "a" * 400, ("intro",)),
        _c(1, "b" * 400, ("intro",)),
        _c(2, "c" * 400, ("body",)),
        _c(3, "d" * 400, ("body",)),
        _c(4, "e" * 2000, ("tail",)),
    ]
    wins = cs.windows(chunks, window_chars=1000)
    # intro (800) closes at the section change because it is over a third full;
    # body (800) likewise; the 2000-char chunk becomes its own window.
    assert [[c.idx for c in w.chunks] for w in wins] == [[0, 1], [2, 3], [4]]
    assert wins[0].cite() == "doc://a#0-1" and wins[2].cite() == "doc://a#4-4"
    # Without sections, packing is by size alone.
    wins = cs.windows([_c(i, "x" * 300) for i in range(7)], window_chars=1000)
    assert [len(w.chunks) for w in wins] == [3, 3, 1]
    assert cs.windows([], window_chars=1000) == []
    # Two documents in one window cite per chunk.
    w = cs.windows([_c(0, "aa", uri="doc://a"), _c(0, "bb", uri="doc://b")], window_chars=100)[0]
    assert w.cite() == "doc://a#0,doc://b#0"


def test_fold_batches_notes_to_the_limit() -> None:
    notes = ["n" * 400, "n" * 400, "n" * 400, "n" * 100]
    assert [len(b) for b in cs.fold(notes, limit_chars=1000)] == [2, 2]
    assert cs.fold(notes, limit_chars=10_000) == [notes]
    assert cs.fold([], limit_chars=10) == []


def test_prompts_carry_task_tags_and_citations() -> None:
    w = cs.windows([_c(3, "The butler left at nine.", ("ch1",))], window_chars=1000)[0]
    mp = cs.map_prompt("Who left?", w)
    assert "TASK the final pass must complete: Who left?" in mp and "[doc://a#3] The butler" in mp
    assert "PART doc://a#3-3" in mp
    rp = cs.reduce_prompt("Who left?", ["The butler left [doc://a#3]"], final=True)
    assert rp.endswith("Answer the task from these notes:") and "NOTES 1:" in rp
    assert "Merge these notes" in cs.reduce_prompt("t", ["a", "b"], final=False)
    assert cs.citations("x [doc://a#3] y [doc://a#3] z [doc://b#0-4] [bad tag]") == [
        "doc://a#3",
        "doc://b#0-4",
    ]


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.model = "fake-synth"

    async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any):
        self.calls.append((system, user, max_tokens))
        if system == cs.MAP_SYSTEM:
            part = user.split("PART ")[1].split(":")[0]
            return SimpleNamespace(text=f"note about [{part}]")
        if "Answer the task from these notes" in user:
            return SimpleNamespace(text="Final answer citing [doc://a#0-1] and [doc://a#2-2].")
        return SimpleNamespace(text="merged notes [doc://a#0-1]")


@pytest.mark.asyncio
async def test_handler_maps_every_window_then_reduces(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.knowledge.app.synthesize as kh
    import server.llm.client as llm_client
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput
    from server.knowledge.store import chunks as knowledge_repo

    async def _over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _over_cap)
    monkeypatch.setattr(
        "server.knowledge.app.document_reads.get_embedder",
        lambda: SimpleNamespace(model_name="fake"),
    )
    rows = [
        SimpleNamespace(
            chunk_idx=0, content="a" * 500, chunk_metadata=json.dumps({"section_path": ["s1"]})
        ),
        SimpleNamespace(
            chunk_idx=1, content="b" * 500, chunk_metadata=json.dumps({"section_path": ["s1"]})
        ),
        SimpleNamespace(
            chunk_idx=2, content="c" * 500, chunk_metadata=json.dumps({"section_path": ["s2"]})
        ),
    ]

    async def _list(org: str, ns: str, uri: str, *, embedding_model: str, ch: Any = None):
        return rows if uri == "doc://a" else []

    monkeypatch.setattr(knowledge_repo, "list_for_doc", _list)
    llm = _LLM()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: llm)
    out = await kh.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(
            org="o",
            namespace="n",
            source_uris=["doc://a", "doc://missing"],
            task="Summarize",
            window_chars=2000,
            max_answer_chars=500,
        )
    )
    assert (
        out.windows == 2 and out.chunks_read == 3 and out.documents == 1 and out.reduce_rounds == 1
    )
    assert out.answer.startswith("Final answer") and out.citations == ["doc://a#0-1", "doc://a#2-2"]
    assert not out.truncated and not out.abstain
    maps = [c for c in llm.calls if c[0] == cs.MAP_SYSTEM]
    assert len(maps) == 2 and all("Summarize" in c[1] for c in maps)
    # Nothing to read → abstain without an LLM call.
    llm2 = _LLM()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: llm2)
    out = await kh.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(org="o", namespace="n", source_uris=["doc://missing"], task="t")
    )
    assert out.abstain and out.abstain_reason == "no_chunks" and llm2.calls == []


@pytest.mark.asyncio
async def test_handler_folds_hierarchically_and_honours_max_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.knowledge.app.synthesize as kh
    import server.llm.client as llm_client
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput
    from server.knowledge.store import chunks as knowledge_repo

    async def _over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _over_cap)
    monkeypatch.setattr(
        "server.knowledge.app.document_reads.get_embedder",
        lambda: SimpleNamespace(model_name="fake"),
    )
    rows = [SimpleNamespace(chunk_idx=i, content="x" * 1900, chunk_metadata="{}") for i in range(6)]

    async def _list(org: str, ns: str, uri: str, *, embedding_model: str, ch: Any = None):
        return rows

    monkeypatch.setattr(knowledge_repo, "list_for_doc", _list)

    class _Long(_LLM):
        async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any):
            self.calls.append((system, user, max_tokens))
            if system == cs.MAP_SYSTEM:
                return SimpleNamespace(text="n" * 1500)  # notes too long to reduce in one batch
            if "Answer the task from these notes" in user:
                return SimpleNamespace(text="answer")
            return SimpleNamespace(text="m" * 300)

    llm = _Long()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: llm)
    out = await kh.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(
            org="o",
            namespace="n",
            source_uris=["doc://a"],
            task="t",
            window_chars=2000,
            max_windows=4,
        )
    )
    assert out.windows == 4 and out.truncated is True and out.chunks_read == 4
    assert out.reduce_rounds == 2 and out.answer == "answer"


@pytest.mark.asyncio
async def test_budget_cap_abstains_before_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.knowledge.app.synthesize as kh
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput

    async def _over_cap(org: str, ns: str) -> bool:
        return True

    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _over_cap)
    out = await kh.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(org="o", namespace="n", source_uris=["doc://a"], task="t")
    )
    assert out.abstain and out.abstain_reason == "budget_capped"


def test_registered() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    assert OPERATIONS_BY_NAME["knowledge.synthesize"].idempotent


def test_a_citation_counts_only_when_it_names_chunks_that_were_read() -> None:
    read = {("doc://a", 0), ("doc://a", 1), ("doc://a", 2), ("doc://b", 0)}
    assert cs.cites_what_was_read("doc://a#1", read)
    assert cs.cites_what_was_read("doc://a#0-2", read)
    assert not cs.cites_what_was_read("doc://a#7", read)  # never shown
    assert not cs.cites_what_was_read("doc://a#2-7", read)  # a range past what was read
    assert not cs.cites_what_was_read("doc://a#2-1", read)  # a backwards range
    assert not cs.cites_what_was_read("doc://c#0", read)  # a document not named
    # a window spanning two documents is cited as a list: every part must be read
    assert cs.cites_what_was_read("doc://a#2,doc://b#0", read)
    assert not cs.cites_what_was_read("doc://a#2,doc://b#1", read)


def test_grounding_removes_the_tags_of_unread_chunks_from_the_answer() -> None:
    read = {("doc://a", 0), ("doc://a", 1)}
    answer, kept, removed = cs.ground_citations(
        "Refunds take 14 days [doc://a#1] [doc://a#9], or 30 [doc://z#0].", read
    )
    assert answer == "Refunds take 14 days [doc://a#1], or 30."
    assert kept == ["doc://a#1"] and removed == ["doc://a#9", "doc://z#0"]
    assert cs.ground_citations("No tags at all.", read) == ("No tags at all.", [], [])
    # an answer of only unread citations is empty
    assert cs.ground_citations("[doc://z#0]", read) == ("", [], ["doc://z#0"])
    # a bracket with anything but citations in it is prose, left as written
    for prose in ("fixed in [PR #42]", "see [#1]", "[items #2, #3]", "[see doc://a#9]"):
        assert cs.ground_citations(prose, read) == (prose, [], [])
    # a list separated by ';' is a citation, checked part by part
    assert cs.ground_citations("x [doc://a#0; doc://a#1]", read) == (
        "x [doc://a#0; doc://a#1]",
        ["doc://a#0; doc://a#1"],
        [],
    )
    # a non-ASCII digit is no index: the bracket is prose
    assert cs.ground_citations("[doc://a#²,doc://a#1]", read) == ("[doc://a#²,doc://a#1]", [], [])
    # a source holding '#' splits at its last one; a padded tag is named without padding
    assert cs.cites_what_was_read("doc://a#frag#0", {("doc://a#frag", 0)})
    assert cs.ground_citations("x [ doc://a#1]", read) == ("x [ doc://a#1]", ["doc://a#1"], [])


def _patch_corpus(monkeypatch: pytest.MonkeyPatch, rows: list[Any], answer: list[str]) -> None:
    """knowledge.synthesize over `rows` (every named document has them), under no cost
    cap, with an LLM that writes notes and then `answer[0]` as the final answer."""
    import server.llm.client as llm_client
    from server.knowledge.store import chunks as knowledge_repo

    async def _over_cap(org: str, ns: str) -> bool:
        return False

    async def _list(org: str, ns: str, uri: str, *, embedding_model: str, ch: Any = None):
        return rows

    class _Final(_LLM):
        async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any):
            self.calls.append((system, user, max_tokens))
            if "Answer the task from these notes" in user:
                return SimpleNamespace(text=answer[0])
            return SimpleNamespace(text="notes")

    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _over_cap)
    monkeypatch.setattr(
        "server.knowledge.app.document_reads.get_embedder",
        lambda: SimpleNamespace(model_name="fake"),
    )
    monkeypatch.setattr(knowledge_repo, "list_for_doc", _list)
    monkeypatch.setattr(llm_client, "get_llm_client", _Final)


@pytest.mark.asyncio
async def test_the_handler_keeps_only_supported_citations_and_abstains_on_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.knowledge.app.synthesize as kh
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput

    rows = [SimpleNamespace(chunk_idx=i, content="x" * 1900, chunk_metadata="{}") for i in range(3)]
    answer = ["From [doc://a#0] and [doc://a#2], not [doc://a#5]."]
    _patch_corpus(monkeypatch, rows, answer)
    inp = KnowledgeSynthesizeInput(
        org="o", namespace="n", source_uris=["doc://a"], task="t", window_chars=2000, max_windows=2
    )
    out = await kh.handle_knowledge_synthesize(inp)
    # two windows of one chunk each were read: chunk 2 and chunk 5 were not
    assert out.truncated and out.chunks_read == 2
    assert out.citations == ["doc://a#0"]
    assert out.unsupported_citations == ["doc://a#2", "doc://a#5"]
    assert out.answer == "From [doc://a#0] and, not." and not out.abstain

    for empty in ("   ", "[doc://a#5]"):  # nothing written, or only unread citations
        answer[0] = empty
        out = await kh.handle_knowledge_synthesize(inp)
        assert out.abstain and out.abstain_reason == "empty_answer" and out.answer == ""


@pytest.mark.asyncio
async def test_a_window_spanning_two_documents_keeps_its_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two short documents share a window, which `Window.cite` names as a comma list:
    an answer citing that tag cites chunks the passes read."""
    import server.knowledge.app.synthesize as kh
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput

    rows = [SimpleNamespace(chunk_idx=0, content="short", chunk_metadata="{}")]
    _patch_corpus(monkeypatch, rows, ["Both say short [doc://a#0,doc://b#0]."])
    out = await kh.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(
            org="o", namespace="n", source_uris=["doc://a", "doc://b"], task="t", window_chars=2000
        )
    )
    assert out.windows == 1
    assert out.citations == ["doc://a#0,doc://b#0"] and out.unsupported_citations == []
    assert out.answer == "Both say short [doc://a#0,doc://b#0]."
