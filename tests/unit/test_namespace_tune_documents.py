"""namespace.tune's document drafts: the chunk size and stride, and an extractor route per
format.

A chunking draft rests on the namespace's documents cut again in process at the current
values and at the proposed ones, by the chunker each one's write path uses (a text
document's text, an ingested one's stored artifact); it is drafted only when the sample
breaks a rule, and the sample is reported either way. An extractor route is drafted only
for a format the jobs or the coverage name and another configured service declares, and
never shows a service's URL. Decision extraction has no instruction to draft. A section
that cannot be read is skipped, not raised. The statements run on clickhouse-local in
test_namespace_tune_documents_sql.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.api.models_namespace_tune import NamespaceTuneInput
from server.knowledge.app import tune
from server.knowledge.app.extractors import Service
from server.knowledge.domain.extract.artifact import Artifact, Block, Coverage
from server.settings import settings

SECRET = "http://user:token@pdf-extractor:8080"
DEFAULT = "http://extractor:8790"


def _long_sections(uri: str, sections: int = 3, chars: int = 4000) -> dict[str, Any]:
    body = "\n\n".join(f"# Part {i}\n\n" + ("word " * (chars // 5)) for i in range(sections))
    return _row(uri, body)


def _row(uri: str, text: str, **kw: Any) -> dict[str, Any]:
    return {
        "source_uri": uri,
        "title": "T",
        "description": "",
        "ingestion_id": "",
        "artifact_sha256": "",
        "doc_format": "markdown",
        "chars": len(text),
        "text": text,
        **kw,
    }


class Docs:
    """Everything the document drafts read, as plain values a test changes."""

    def __init__(self) -> None:
        self.sample = [_long_sections(f"docs/{i}.md") for i in range(6)]
        self.live = 6
        self.limit: int | None = 8191
        self.unavailable: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []
        self.services = [Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"markdown"}))]
        self.routes: dict[str, str] = {}
        self.routed: dict[str, str] = {}
        self.artifacts: dict[str, bytes] = {}
        self.fail: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def reader(name: str, value: Any) -> Any:
            async def read(*a: Any, **k: Any) -> Any:
                if name in self.fail:
                    raise RuntimeError(f"{name} failed")
                return value()

            return read

        monkeypatch.setattr(
            tune.inspect_store,
            "document_groups",
            reader("documents", lambda: [{"live": self.live}]),
        )
        monkeypatch.setattr(
            tune.store,
            "document_writers",
            reader("writers", lambda: [{"agent": "sync", "rows": self.live, "total": 1}]),
        )
        monkeypatch.setattr(tune.store, "sample_documents", reader("sample", lambda: self.sample))
        monkeypatch.setattr(
            tune.inspect_store,
            "format_unavailable_sources",
            reader("jobs", lambda: self.unavailable),
        )
        monkeypatch.setattr(
            tune.inspect_store, "extraction_by_format", reader("coverage", lambda: self.coverage)
        )
        monkeypatch.setattr(
            tune.extractors, "configured_services", reader("services", lambda: self.services)
        )
        monkeypatch.setattr(tune, "_current_routes", reader("routes", lambda: self.routes))
        monkeypatch.setattr(
            tune.extractors, "endpoint_for", lambda fmt, media="": self.routed.get(fmt, DEFAULT)
        )
        monkeypatch.setattr(
            tune, "_embedder", reader("embedder", lambda: ("text-embedding-3-small", self.limit))
        )

        async def locate(org: str, ns: str, sha: str) -> Any:
            data = self.artifacts.get(sha)
            return None if data is None else type("S", (), {"size": len(data), "location": ""})()

        async def read(org: str, ns: str, sha: str, *, stored: Any) -> Any:
            return self.artifacts.get(sha)

        monkeypatch.setattr(tune.blobs, "locate", locate)
        monkeypatch.setattr(tune.blobs, "read", read)
        monkeypatch.setattr(settings, "knowledge_chunk_size", 1500)
        monkeypatch.setattr(settings, "knowledge_chunk_stride", 1230)
        monkeypatch.setattr(settings, "namespace_tune_document_sample", 50)


@pytest.fixture
def docs(monkeypatch: pytest.MonkeyPatch) -> Docs:
    d = Docs()
    d.install(monkeypatch)
    return d


async def _drafts(**kw: Any) -> tuple[Any, list[str]]:
    skipped: list[str] = []

    async def guarded(section: str, read: Any, default: Any) -> Any:
        try:
            return await read()
        except Exception as e:  # noqa: BLE001 — the handler's guard, as tests see it
            skipped.append(f"{section}: {type(e).__name__}")
            return default

    inp = NamespaceTuneInput(**{"org": "o", "namespace": "ns", **kw})
    return await tune.document_drafts(inp, guarded, skipped), skipped


def _target(out: Any, target: str) -> list[Any]:
    return [p for p in out.proposals if p.target == target]


@pytest.mark.asyncio
async def test_sections_cut_by_the_size_draft_a_larger_one_with_its_before_and_after(
    docs: Docs,
) -> None:
    out, skipped = await _drafts()
    (p,) = _target(out, "settings.knowledge_chunking")
    before, after = p.preview.before, p.preview.after
    # Every 4000-character section is cut at 1500; at 4500 none is.
    assert (before.size, before.stride, before.sections, before.sections_cut) == (
        1500,
        1230,
        18,
        18,
    )
    assert (after.size, after.sections_cut, after.over_input_limit) == (4500, 0, 0)
    # The stride keeps its ratio to the size.
    assert after.stride == round(1230 * 4500 / 1500)
    assert out.chunking.current == before and out.chunking.documents == 6
    assert out.chunking.cap == 50 and out.chunking.population == 6 and out.chunking.unread == 0
    assert after.chunks < before.chunks
    assert p.apply.op == "settings.set" and p.apply.via == "admin_route"
    assert p.apply.args == {"name": "knowledge_chunk_size", "value": 4500, "expected": 1500}
    (stride,) = p.apply_also
    assert stride.args == {"name": "knowledge_chunk_stride", "value": 3690, "expected": 1230}
    assert p.apply.required_role == "platform_admin" and p.effect == "future"
    assert p.evidence.supporting_count == 6 and p.evidence.counter_count == 0
    assert p.evidence.documents == [f"docs/{i}.md" for i in range(5)]
    assert "every namespace" in p.scope and "loom rechunk" in p.historical
    assert p.affected_agents == ["sync"] and p.affected_agents_total == 1
    # No document text leaves the draft.
    assert "word word" not in out.chunking.model_dump_json() + p.model_dump_json()
    assert tune.DECISIONS_SKIPPED in skipped


@pytest.mark.asyncio
async def test_no_draft_when_the_sections_fit(docs: Docs) -> None:
    docs.sample = [_long_sections(f"docs/{i}.md", chars=600) for i in range(6)]
    out, skipped = await _drafts()
    assert not out.proposals
    # The sample is the evidence that nothing needs changing.
    assert out.chunking.current.sections == 18 and out.chunking.current.sections_cut == 0
    assert not [s for s in skipped if s.startswith("chunking")]


@pytest.mark.asyncio
async def test_an_unknown_input_limit_drafts_no_larger_size_and_says_why(docs: Docs) -> None:
    docs.limit = None
    out, skipped = await _drafts()
    assert not out.proposals
    assert out.chunking.input_token_limit is None
    assert out.chunking.current.over_input_limit is None
    assert any("input limit is not known" in s for s in skipped)


@pytest.mark.asyncio
async def test_too_few_documents_judge_no_section_rule(docs: Docs) -> None:
    docs.sample = docs.sample[:2]
    out, skipped = await _drafts()
    assert not out.proposals
    assert any("fewer than the 5" in s for s in skipped)


@pytest.mark.asyncio
async def test_inputs_over_the_limit_draft_the_largest_smaller_size_that_fits(
    docs: Docs,
) -> None:
    # About 375 tokens per 1500-character window; a 300-token limit is passed by each.
    docs.limit = 300
    out, _ = await _drafts()
    (p,) = _target(out, "settings.knowledge_chunking")
    assert p.preview.before.over_input_limit > 0
    assert p.preview.after.over_input_limit == 0
    assert p.preview.after.size < 1500
    assert "300-token limit" in p.rationale


@pytest.mark.asyncio
async def test_an_ingested_document_is_cut_from_its_stored_artifact(docs: Docs) -> None:
    blocks = tuple(Block(id=f"b{i}", type="sql_statement", text=f"SELECT {i};") for i in range(30))
    artifact = Artifact("sql", "ref", "1", blocks, Coverage("statements", 30, 30))
    docs.artifacts["a" * 64] = artifact.to_json()
    docs.sample = [
        _row("files/q.sql", "", ingestion_id="j1", artifact_sha256="a" * 64, doc_format="sql"),
        _row("files/gone.sql", "", ingestion_id="j2", artifact_sha256="b" * 64, doc_format="sql"),
    ]
    out, _ = await _drafts()
    # One chunk per statement, as knowledge.ingest cut them; none cut by the size.
    assert out.chunking.current.chunks == 30 and out.chunking.current.sections_cut == 0
    assert out.chunking.documents == 1 and out.chunking.unread == 1
    assert not out.proposals


@pytest.mark.asyncio
async def test_a_text_too_long_to_sample_is_counted_unread(docs: Docs) -> None:
    docs.sample.append(_row("docs/huge.md", "", chars=tune.MAX_DOCUMENT_CHARS + 1))
    out, _ = await _drafts()
    assert out.chunking.documents == 6 and out.chunking.unread == 1


@pytest.mark.asyncio
async def test_a_format_no_service_takes_drafts_a_route_to_one_that_declares_it(
    docs: Docs,
) -> None:
    docs.sample = []
    docs.live = 0
    docs.routes = {"docx": SECRET}
    docs.services.append(
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', SECRET, frozenset({"docx", "pdf"}))
    )
    docs.unavailable = [
        {
            "source_uri": f"files/{i}.pdf",
            "filename": f"{i}.pdf",
            "content_type": "",
            "declared_format": "",
            "ingestion_id": f"j{i}",
        }
        for i in range(3)
    ]
    out, skipped = await _drafts()
    (p,) = out.proposals
    assert p.target == "settings.document_extractors_json"
    assert p.apply.via == "environment" and p.apply.args_complete is False
    routes = p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]
    assert routes == {
        "docx": "<as set>",
        "pdf": '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["docx"] names>',
    }
    assert p.evidence.finding_codes == ["extractor_unavailable"]
    assert p.evidence.supporting_count == 3 and p.evidence.documents[0] == "files/0.pdf"
    # A service's URL can carry a credential: it is never shown.
    assert "token" not in p.model_dump_json() and "pdf-extractor" not in p.model_dump_json()
    # No document: no chunk sample and no decision line.
    assert out.chunking is None and tune.DECISIONS_SKIPPED not in skipped


@pytest.mark.asyncio
async def test_a_format_no_configured_service_declares_is_said_not_invented(
    docs: Docs,
) -> None:
    docs.unavailable = [
        {
            "source_uri": "files/a.pdf",
            "filename": "a.pdf",
            "content_type": "",
            "declared_format": "",
            "ingestion_id": "j1",
        }
    ]
    out, skipped = await _drafts(sample_limit=1)
    assert not _target(out, "settings.document_extractors_json")
    assert any(
        s.startswith("extractor.pdf:") and "no configured extractor declares pdf" in s
        for s in skipped
    )


@pytest.mark.asyncio
async def test_a_format_the_routed_service_declares_now_is_not_drafted(docs: Docs) -> None:
    docs.services = [Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"}))]
    docs.unavailable = [
        {
            "source_uri": "files/a.pdf",
            "filename": "a",
            "content_type": "application/pdf",
            "declared_format": "",
            "ingestion_id": "j1",
        }
    ]
    out, skipped = await _drafts()
    assert not _target(out, "settings.document_extractors_json")
    assert any("declares it now" in s for s in skipped)


@pytest.mark.asyncio
async def test_lost_structure_drafts_another_declaring_service_or_says_none(docs: Docs) -> None:
    docs.coverage = [
        {
            "doc_format": "sql",
            "documents": 5,
            "incomplete": 2,
            "incomplete_uris": ["files/a.sql", "files/b.sql"],
            "lost": ["sql_structure"],
        },
        {"doc_format": "toml", "documents": 3, "incomplete": 0, "incomplete_uris": [], "lost": []},
    ]
    out, skipped = await _drafts()
    assert not _target(out, "settings.document_extractors_json")
    assert any(
        "extractor.sql: 2 of 5 documents extracted lost sql_structure" in s
        and "no other configured extractor" in s
        for s in skipped
    )
    # The toml documents lost nothing: no line about them.
    assert not [s for s in skipped if s.startswith("extractor.toml")]

    docs.services.append(
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["sql"]', SECRET, frozenset({"sql"}))
    )
    docs.routed = {"sql": DEFAULT}
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.evidence.finding_codes == ["extraction_incomplete"]
    assert p.evidence.supporting_count == 2 and p.evidence.counter_count == 3
    assert p.evidence.documents == ["files/a.sql", "files/b.sql"]
    assert "what it keeps" in p.limits


@pytest.mark.asyncio
async def test_an_unread_section_is_skipped_not_raised(docs: Docs) -> None:
    docs.fail = {"sample", "jobs", "services"}
    docs.unavailable = []
    out, skipped = await _drafts()
    assert not out.proposals and out.chunking is None
    assert "chunking: RuntimeError" in skipped and "extractor.jobs: RuntimeError" in skipped


@pytest.mark.asyncio
async def test_the_handler_names_each_document_draft_by_its_value(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.app import namespace_tune

    async def nothing(*a: Any, **k: Any) -> None:
        raise RuntimeError("not read here")

    for name in ("writing_agents", "plain_tags", "key_prefixes"):
        monkeypatch.setattr(namespace_tune.store, name, nothing)
    for name in (
        "handle_namespace_audit",
        "handle_namespace_list_categories",
        "handle_namespace_guide_get",
    ):
        monkeypatch.setattr(namespace_tune, name, nothing)

    async def policy(org: str, ns: str) -> Any:
        from server.memory.app.extraction_policy import CallerPolicy

        return CallerPolicy(None, "only an org admin reads it", "")

    monkeypatch.setattr(namespace_tune.extraction_policy, "for_caller", policy)
    inp = NamespaceTuneInput(org="o", namespace="ns")
    first = await namespace_tune.handle_namespace_tune(inp)
    again = await namespace_tune.handle_namespace_tune(inp)
    (p,) = first.proposals
    assert p.id and p.id == again.proposals[0].id
    assert first.chunking is not None and first.chunking.current.sections_cut == 18
