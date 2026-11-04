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
from server.knowledge.app.extractors import Service, endpoint_for
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
        # format, or (format, media type), -> the URL its sources go to; else the default.
        self.routed: dict[Any, str] = {}
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
            tune.extractors,
            "endpoint_for",
            lambda fmt, media="": self.routed.get((fmt, media)) or self.routed.get(fmt, DEFAULT),
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
            "incomplete_types": [""],
            "complete_types": [""],
        },
        {
            "doc_format": "toml",
            "documents": 3,
            "incomplete": 0,
            "incomplete_uris": [],
            "lost": [],
            "incomplete_types": [],
            "complete_types": [""],
        },
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
    docs.fail = {"sample", "jobs", "services", "documents"}
    docs.unavailable = []
    out, skipped = await _drafts()
    assert not out.proposals and out.chunking is None
    assert "chunking: RuntimeError" in skipped and "extractor.jobs: RuntimeError" in skipped
    # The population read is named apart from the whole section's guard (`documents`).
    assert "documents.population: RuntimeError" in skipped


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


def _two_paragraph_sections(uri: str, sha: str, docs: Docs) -> dict[str, Any]:
    """An ingested document of three sections, each a heading and two paragraphs of 150
    characters: at size 0 each paragraph is a chunk of its own."""
    blocks: list[Block] = []
    for s in range(3):
        head = f"h{s}"
        blocks.append(Block(id=head, type="heading", text=f"Part {s}", depth=1))
        blocks += [
            Block(
                id=f"p{s}{p}",
                type="paragraph",
                text="word " * 30,
                parent=head,
                section_path=(f"Part {s}",),
            )
            for p in range(2)
        ]
    artifact = Artifact("markdown", "ref", "1", tuple(blocks), Coverage("lines", 9, 9))
    docs.artifacts[sha] = artifact.to_json()
    return _row(uri, "", ingestion_id="j", artifact_sha256=sha)


@pytest.mark.asyncio
async def test_a_chunk_size_of_zero_drafts_nothing_and_reports_the_sample(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_chunk_size", 0)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 0)
    docs.sample = [_two_paragraph_sections(f"docs/{i}.md", f"{i}" * 64, docs) for i in range(5)]
    out, skipped = await _drafts()
    assert not out.proposals
    assert out.chunking is not None and out.chunking.documents == 5
    assert out.chunking.current.size == 0
    assert (
        "chunking: knowledge_chunk_size is 0 (no window: a text document is one chunk, an "
        "ingested one a chunk per block); no size is drafted" in skipped
    )


@pytest.mark.asyncio
async def test_a_negative_chunk_size_is_named_as_it_is(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_chunk_size", -5)
    out, skipped = await _drafts()
    assert not out.proposals
    assert any(s.startswith("chunking: knowledge_chunk_size is -5 (no window") for s in skipped)


@pytest.mark.asyncio
async def test_a_size_below_the_smallest_drafted_one_still_drafts_a_larger_size(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chunker takes any positive size: 100 cuts every 150-character paragraph, and
    # 400, the first candidate at or above the smallest drafted size that packs both
    # paragraphs of a section, keeps each section whole.
    monkeypatch.setattr(settings, "knowledge_chunk_size", 100)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 80)
    docs.sample = [_two_paragraph_sections(f"docs/{i}.md", f"{i}" * 64, docs) for i in range(5)]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.knowledge_chunking")
    assert p.preview.before.size == 100 and p.preview.after.size == 400
    assert p.preview.after.sections_cut == 0 and p.preview.after.stride == 320


@pytest.mark.asyncio
async def test_the_sample_stops_loading_at_its_total_budget(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tune, "MAX_SAMPLE_CHARS", 3 * 12_100)
    out, _ = await _drafts()
    # Each document is about 12,100 characters: three fit, the other three are unread.
    assert out.chunking.documents == 3 and out.chunking.unread == 3
    assert out.chunking.load_cap == 3 * 12_100


@pytest.mark.asyncio
async def test_two_unreachable_extractors_are_read_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    import httpx

    from server.knowledge.app import extractors

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    monkeypatch.setattr(extractors, "transport", httpx.MockTransport(slow))
    monkeypatch.setattr(extractors, "_declared", {})
    monkeypatch.setattr(settings, "document_extractor_url", "http://one:8790")
    monkeypatch.setattr(settings, "document_extractors_json", '{"pdf": "http://two:8790"}')
    started = time.monotonic()
    services = await extractors.configured_services(timeout_s=0.5)
    # Read together: the whole read takes one timeout, not one per service.
    assert time.monotonic() - started < 0.9
    assert [(s.setting, s.formats) for s in services] == [
        ("LOOM_DOCUMENT_EXTRACTOR_URL", None),
        ('LOOM_DOCUMENT_EXTRACTORS_JSON["pdf"]', None),
    ]


@pytest.mark.asyncio
async def test_each_candidate_size_is_tried_once(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[int] = []
    measure = tune._measure

    def counting(d: Any, size: int, stride: int, limit: Any) -> Any:
        tried.append(size)
        return measure(d, size, stride, limit)

    monkeypatch.setattr(tune, "_measure", counting)
    monkeypatch.setattr(settings, "knowledge_chunk_size", 100)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 80)
    # Sections no candidate keeps whole: every one is tried, each once, nearest first.
    docs.sample = [_long_sections(f"docs/{i}.md") for i in range(6)]
    out, skipped = await _drafts()
    assert tried == [100, 200, 300, 400]
    assert any("no size tried (200, 300, 400)" in s for s in skipped)


@pytest.mark.asyncio
async def test_inputs_over_the_limit_at_the_smallest_size_say_so(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_chunk_size", tune.MIN_CHUNK_SIZE)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 150)
    docs.limit = 10
    out, skipped = await _drafts()
    assert not out.proposals
    assert any(
        s.startswith("chunking: ")
        and s.endswith(
            f"knowledge_chunk_size {tune.MIN_CHUNK_SIZE} is at or below the smallest drafted "
            f"size, {tune.MIN_CHUNK_SIZE}"
        )
        for s in skipped
    ), skipped


@pytest.mark.asyncio
async def test_a_media_type_route_is_the_service_a_source_goes_to(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The media type's route sends application/pdf sources to a service that does not
    # declare pdf; the default one does, and takes a pdf named by its extension alone.
    monkeypatch.setattr(tune.extractors, "endpoint_for", endpoint_for)
    monkeypatch.setattr(settings, "document_extractor_url", DEFAULT)
    monkeypatch.setattr(settings, "document_extractors_json", f'{{"application/pdf": "{SECRET}"}}')
    docs.routes = {"application/pdf": SECRET}
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', SECRET, frozenset()),
    ]
    docs.unavailable = [
        {
            "source_uri": "files/a.pdf",
            "filename": "a.pdf",
            "content_type": "application/pdf",
            "declared_format": "",
            "ingestion_id": "j1",
        },
        {
            "source_uri": "files/b.pdf",
            "filename": "b.pdf",
            "content_type": "",
            "declared_format": "",
            "ingestion_id": "j2",
        },
    ]
    out, skipped = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    # A format's own route wins over the media type's: pdf goes to the default service.
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"] == {
        "application/pdf": "<as set>",
        "pdf": "<the URL LOOM_DOCUMENT_EXTRACTOR_URL names>",
    }
    assert p.evidence.supporting_count == 1 and p.evidence.documents == ["files/a.pdf"]
    assert 'LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"], which does not declare it' in (
        p.rationale
    )
    assert "wins over a media-type route" in p.limits
    # The source the default service takes now is ingested again, not rerouted.
    assert any(
        s.startswith("extractor.pdf: 1 source failed format_unavailable") and "declares it now" in s
        for s in skipped
    ), skipped


@pytest.mark.asyncio
async def test_inputs_over_the_limit_below_the_smallest_size_name_the_value_in_force(
    docs: Docs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_chunk_size", 150)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 120)
    docs.limit = 10
    out, skipped = await _drafts()
    assert not out.proposals
    assert any(
        s.endswith(
            f"knowledge_chunk_size 150 is at or below the smallest drafted size, "
            f"{tune.MIN_CHUNK_SIZE}"
        )
        for s in skipped
    ), skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("stride", [0, -3])
async def test_a_stride_of_zero_or_less_drafts_nothing_and_names_it(
    docs: Docs, monkeypatch: pytest.MonkeyPatch, stride: int
) -> None:
    # The chunkers window nothing at such a stride: a section longer than the size is one
    # chunk. A draft would scale it to a stride of 1.
    monkeypatch.setattr(settings, "knowledge_chunk_stride", stride)
    out, skipped = await _drafts()
    assert not out.proposals
    assert out.chunking is not None and out.chunking.current.sections_cut == 0
    assert any(
        s.startswith(f"chunking: knowledge_chunk_stride is {stride} (no window") for s in skipped
    ), skipped


def _pdf_job(uri: str, media: str = "") -> dict[str, Any]:
    return {
        "source_uri": uri,
        "filename": uri.rsplit("/", 1)[-1],
        "content_type": media,
        "declared_format": "",
        "ingestion_id": "j",
    }


def _lossy_pdf(
    incomplete: int = 3,
    documents: int = 10,
    incomplete_types: tuple[str, ...] = ("",),
    complete_types: tuple[str, ...] = ("",),
) -> dict[str, Any]:
    """pdf extractions, `incomplete` of `documents` lost page_text, with the media types
    of the originals behind the incomplete and the complete ones."""
    return {
        "doc_format": "pdf",
        "documents": documents,
        "incomplete": incomplete,
        "incomplete_uris": [f"files/lost{i}.pdf" for i in range(incomplete)],
        "lost": ["page_text"],
        "incomplete_types": list(incomplete_types),
        "complete_types": list(complete_types),
    }


@pytest.mark.asyncio
async def test_a_format_whose_failed_sources_work_now_is_still_judged_on_its_extractions(
    docs: Docs,
) -> None:
    other = "http://pdf-b:8080"
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["pdf"]', other, frozenset({"pdf"})),
    ]
    docs.unavailable = [_pdf_job("files/a.pdf")]
    docs.coverage = [_lossy_pdf()]
    out, skipped = await _drafts()
    assert any("declares it now" in s for s in skipped)
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.evidence.finding_codes == ["extraction_incomplete"]
    assert p.evidence.supporting_count == 3
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["pdf"] names>'
    )


@pytest.mark.asyncio
async def test_a_route_for_failed_sources_avoids_the_service_that_lost_structure(
    docs: Docs,
) -> None:
    # Failed sources go to a service that does not declare pdf; the default one declares
    # it but extracted the documents that lost page_text; a third declares it too.
    down, third = "http://down:8080", "http://pdf-c:8080"
    docs.routed = {("pdf", "application/pdf"): down}
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["pdf"]', down, frozenset()),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', third, frozenset({"pdf", "docx"})),
    ]
    docs.unavailable = [_pdf_job("files/a.pdf", "application/pdf")]
    # Every extraction lost page_text, each the default service's (no media type).
    docs.coverage = [_lossy_pdf(documents=3, complete_types=())]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["docx"] names>'
    )
    # The route answers both: the failed source and the incomplete extractions.
    assert p.evidence.finding_codes == ["extractor_unavailable", "extraction_incomplete"]
    assert p.evidence.supporting_count == 4


@pytest.mark.asyncio
async def test_a_route_for_failed_sources_keeps_the_service_a_media_type_route_reaches(
    docs: Docs,
) -> None:
    # pdf named by its extension goes to the default service, which does not declare it;
    # application/pdf sources go to A, which does; C, routed for csv, declares pdf too.
    a, c = "http://pdf-a:8080", "http://csv-c:8080"
    docs.routes = {"text/csv": c, "application/pdf": a}
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"markdown"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["text/csv"]', c, frozenset({"csv", "pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', a, frozenset({"pdf"})),
    ]
    docs.unavailable = [_pdf_job("files/a.pdf")]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    # Routing pdf to C would move the application/pdf sources that work on A.
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"] names>'
    )


@pytest.mark.asyncio
async def test_a_route_to_the_service_that_lost_structure_says_it_answers_only_the_failures(
    docs: Docs,
) -> None:
    down = "http://down:8080"
    docs.routed = {("pdf", "application/pdf"): down}
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["pdf"]', down, frozenset()),
    ]
    docs.unavailable = [_pdf_job("files/a.pdf", "application/pdf")]
    docs.coverage = [_lossy_pdf()]
    out, skipped = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    # The only service that declares pdf made the incomplete extractions: the route
    # answers the failed source alone, and says so.
    assert p.evidence.finding_codes == ["extractor_unavailable"]
    assert p.evidence.supporting_count == 1
    assert any(
        s.startswith("extractor.pdf: 3 of 10 documents extracted lost page_text")
        and "the route drafted for its failed sources goes to" in s
        for s in skipped
    ), skipped


@pytest.mark.asyncio
async def test_incomplete_extractions_go_to_the_service_that_extracts_the_rest_whole(
    docs: Docs,
) -> None:
    # application/pdf originals go to W, which extracted them whole; the rest go to the
    # default service, which made the incomplete ones. Both declare pdf.
    w = "http://pdf-w:8080"
    docs.routed = {("pdf", "application/pdf"): w}
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', w, frozenset({"pdf"})),
    ]
    docs.coverage = [_lossy_pdf(incomplete_types=("",), complete_types=("application/pdf",))]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"] names>'
    )
    assert p.evidence.finding_codes == ["extraction_incomplete"]
    # Nothing that extracts whole moves: W's sources stay on W.
    assert "extracts whole now" not in p.rationale


@pytest.mark.asyncio
async def test_failed_sources_go_to_a_service_that_extracts_the_format_whole(
    docs: Docs,
) -> None:
    # The default service extracts pdf whole; application/pdf sources fail on X, which a
    # media-type route sends them to and which does not declare pdf. T declares pdf too.
    x, t = "http://x:8080", "http://pdf-t:8080"
    docs.routed = {("pdf", "application/pdf"): x}
    docs.routes = {"application/pdf": x}
    services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', x, frozenset()),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', t, frozenset({"pdf", "docx"})),
    ]
    docs.services = services
    docs.unavailable = [_pdf_job("files/a.pdf", "application/pdf")]
    whole = _lossy_pdf(incomplete=0, documents=7, complete_types=("",))
    docs.coverage = [whole]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        "<the URL LOOM_DOCUMENT_EXTRACTOR_URL names>"
    )
    # It answers the failed source; there is no incomplete extraction to answer.
    assert p.evidence.finding_codes == ["extractor_unavailable"]
    assert p.evidence.documents == ["files/a.pdf"]
    assert "extracts whole now" not in p.rationale


@pytest.mark.asyncio
async def test_a_route_off_a_service_that_extracts_some_whole_names_what_it_moves(
    docs: Docs,
) -> None:
    # The default service extracted some pdfs whole and the rest incomplete; only T, which
    # made none of them, can answer the incomplete ones, so the whole ones move to T too.
    t = "http://pdf-t:8080"
    docs.services = [
        Service("LOOM_DOCUMENT_EXTRACTOR_URL", DEFAULT, frozenset({"pdf"})),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', t, frozenset({"pdf", "docx"})),
    ]
    docs.coverage = [_lossy_pdf(incomplete_types=("",), complete_types=("",))]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert p.apply.args["LOOM_DOCUMENT_EXTRACTORS_JSON"]["pdf"] == (
        '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["docx"] names>'
    )
    assert (
        "The route also sends there the pdf sources LOOM_DOCUMENT_EXTRACTOR_URL extracts "
        "whole now." in p.rationale
    )


@pytest.mark.asyncio
async def test_a_format_no_extractor_is_configured_for_says_so(docs: Docs) -> None:
    # No default URL and no route for pdf: its sources go to no service.
    t = "http://pdf-t:8080"
    docs.routed = {"pdf": ""}
    docs.services = [
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', t, frozenset({"pdf", "docx"})),
    ]
    docs.unavailable = [_pdf_job("files/a.pdf")]
    docs.coverage = [_lossy_pdf(incomplete=0, complete_types=("",))]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert "no extractor is configured for it" in p.rationale
    assert "no service" not in p.rationale


@pytest.mark.asyncio
async def test_failed_sources_with_and_without_an_extractor_are_told_apart(docs: Docs) -> None:
    # No default URL; application/pdf goes to X, which does not declare pdf, and a pdf of
    # another media type goes to no service.
    x, t = "http://x:8080", "http://pdf-t:8080"
    docs.routed = {("pdf", "application/pdf"): x, "pdf": ""}
    docs.services = [
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', x, frozenset()),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', t, frozenset({"pdf", "docx"})),
    ]
    docs.unavailable = [
        _pdf_job("files/a.pdf", "application/pdf"),
        _pdf_job("files/b.pdf", "application/octet-stream"),
    ]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert (
        "pdf: 2 sources failed format_unavailable; 1 goes to "
        'LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"], which does not declare it; '
        "1 has no extractor configured;" in p.rationale
    ), p.rationale


async def test_failed_sources_of_two_routed_services_take_a_plural_verb(docs: Docs) -> None:
    # Two media types of pdf go to two services, neither of which declares pdf.
    x, y, t = "http://x:8080", "http://y:8080", "http://pdf-t:8080"
    docs.routed = {("pdf", "application/pdf"): x, ("pdf", "application/x-pdf"): y, "pdf": ""}
    docs.services = [
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/pdf"]', x, frozenset()),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["application/x-pdf"]', y, frozenset()),
        Service('LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]', t, frozenset({"pdf", "docx"})),
    ]
    docs.unavailable = [
        _pdf_job("files/a.pdf", "application/pdf"),
        _pdf_job("files/b.pdf", "application/x-pdf"),
    ]
    out, _ = await _drafts()
    (p,) = _target(out, "settings.document_extractors_json")
    assert "which do not declare it" in p.rationale, p.rationale
