"""Prose nominations stay grounded suggestions, separate from exact file links."""

from types import SimpleNamespace

import pytest

from server.cli.decisions import print_report
from server.code.app import prose_candidates as pc
from server.code.app import search as retrieval
from server.code.app.search import handler as search
from server.code.app.search.state import CodeSearchResult
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.knowledge.app import document_reads
from server.memory.app import decision_extract as de
from server.memory.domain.corpus_synthesis import CorpusChunk
from server.settings import settings
from tests.unit.test_decision_extract import _World

PROSE = "When a delivery fails, wait longer before each retry and stop after three attempts."
SOURCE = "def retry_delay(attempt):\n    return 2 ** attempt if attempt < 3 else None"
PATH = "src/delivery.py"
COMMIT = "a" * 40


@pytest.fixture
def index(monkeypatch):
    result = CodeSearchResult(
        route="hybrid",
        units=[
            CodeUnit(
                unit_id="chunk",
                path=PATH,
                start_line=1,
                end_line=2,
                symbol="retry_delay",
                body=SOURCE,
            )
        ],
    )
    world = SimpleNamespace(
        as_of={"branch": "main", "commit": COMMIT, "status": "fresh"},
        branch="main",
        clone=COMMIT,
        slice="",
        stage="ready",
        ground_reads=[],
        result=result,
        calls=[],
        rows=[
            dict(
                chunk_id="chunk",
                path=PATH,
                start_line=1,
                end_line=2,
                scope="retry_delay",
                content=SOURCE,
                kind="function",
            )
        ],
        commit=COMMIT,
    )

    async def retrieve(repo, org, namespace, **kwargs):
        world.calls.append(SimpleNamespace(org=org, namespace=namespace, **kwargs))
        return world.result

    async def thresholds(org, ns):
        return 0.3, 0.22

    async def all_state(self, org, namespace):
        return {
            "git_head": {"value": world.commit},
            "clone_head": {"value": world.clone, "detail": world.branch},
            "commit_slice": {"value": world.slice},
            "bootstrap_stage": {"value": world.stage},
        }

    async def chunks(self, org, namespace, model, keys):
        world.ground_reads.append((org, namespace, model, keys))
        return [r for r in world.rows if (r["path"], r["chunk_id"]) in keys]

    monkeypatch.setattr(retrieval, "search", retrieve)
    monkeypatch.setattr(pc, "resolve_code_thresholds", thresholds)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(CodeRepo, "all_state", all_state)
    monkeypatch.setattr(CodeRepo, "chunks_by_keys", chunks)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"org":"o","namespace":"ns","repo":"example/deliveries","git_branch":"main"}]',
    )
    return world


async def preview(monkeypatch, *, dry_run=True, mentions=None, text=PROSE):
    candidate = dict(
        category="design:delivery",
        scenario="how failed deliveries retry",
        outcome="wait longer between retries and stop at three",
        evidence=[text],
        reasons=[],
        rejected=[],
        mentions=mentions or [],
        stage="proposed",
    )
    world = _World(monkeypatch, [candidate])
    world.content = "# Delivery\n\n" + text

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, text, ("Delivery",))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    return await de.extract_document("o", "ns", "doc.md", dry_run=dry_run), world


@pytest.mark.asyncio
async def test_identifier_free_preview_surfaces_grounded_unverified_code(
    monkeypatch, index, capsys
):
    # No path, backticks, class or qualified symbol appears in the source quotation.
    report, writes = await preview(monkeypatch)
    assert not report.error
    assert len(report.records) == 1
    rec = report.records[0]
    discovery = getattr(rec, "code_discovery", None)
    assert discovery is not None, "identifier-free decision needs reviewable code candidates"
    assert discovery.status == "candidates"
    assert [(c.path, c.symbol, c.excerpt) for c in discovery.candidates] == [
        (PATH, "retry_delay", SOURCE)
    ]
    assert discovery.quote == PROSE
    assert rec.body["files"] == []
    assert writes.recorded == []
    print_report(report)
    rendered = capsys.readouterr().out
    assert "candidate (unverified)" in rendered
    assert PATH in rendered and COMMIT in rendered


@pytest.mark.asyncio
async def test_unrelated_prose_keeps_search_abstention(monkeypatch, index, capsys):
    index.result.abstain = True
    index.result.abstain_code = "off_domain"
    index.result.abstain_reason = "No indexed code is close to this prose"
    report, _ = await preview(monkeypatch, text="The office meal is served before noon.")
    discovery = report.records[0].code_discovery
    assert discovery.status == "abstained" and discovery.candidates == []
    print_report(report)
    output = capsys.readouterr().out
    assert "abstained" in output and "candidate (unverified)" not in output


@pytest.mark.asyncio
async def test_ambiguous_high_similarity_hits_never_become_files(monkeypatch, index):
    duplicate = index.result.units[0].model_copy(
        update={"unit_id": "other", "path": "src/policy.py", "score": 1.0}
    )
    index.result.units.append(duplicate)
    index.rows.append({**index.rows[0], "chunk_id": "other", "path": "src/policy.py"})
    report, _ = await preview(monkeypatch)
    assert len(report.records[0].code_discovery.candidates) == 2
    assert report.records[0].body["files"] == []
    applied, writes = await preview(monkeypatch, dry_run=False)
    assert applied.records[0].code_discovery is None
    assert writes.recorded[0].files == []
    # Discovery is operator-preview work, never an implicit source-sync write stage.
    assert len(index.calls) == 1


@pytest.mark.parametrize(
    "change", ["deleted", "foreign_namespace", "fabricated", "span", "symbol", "test", "page"]
)
@pytest.mark.asyncio
async def test_candidates_outside_current_code_evidence_are_rejected(monkeypatch, index, change):
    hit = index.result.units[0]
    if change == "deleted":
        index.rows.clear()
    elif change == "foreign_namespace":
        hit.namespace = "other-repository-branch"
    elif change == "fabricated":
        hit.path = "src/invented.py"
    elif change == "span":
        hit.start_line = 7
    elif change == "symbol":
        hit.symbol = "invented_symbol"
    else:
        path = "tests/retry.py" if change == "test" else "docs/retry.md"
        hit.path = path
        index.rows[0]["path"] = path
    report, _ = await preview(monkeypatch)
    discovery = report.records[0].code_discovery
    assert discovery.status == "deferred"
    assert discovery.candidates == []
    assert report.records[0].body["files"] == []


@pytest.mark.parametrize(
    "change", ["partial", "failed_leg", "unknown", "stale", "branch", "commit", "tier"]
)
@pytest.mark.asyncio
async def test_incomplete_retrieval_is_deferred_not_empty_success(monkeypatch, index, change):
    if change == "partial":
        index.result.retrieval_work["vector_leg_failed"] = 1
    elif change == "failed_leg":
        index.result.epistemic = "partial"
    elif change == "tier":
        monkeypatch.setattr(pc, "failing_tiers", lambda *args, **kwargs: [{"tier": "ingest"}])
    elif change == "stale":
        monkeypatch.setattr(pc, "mirror_fetch_overdue", lambda fetched: True)
    elif change == "unknown":
        index.clone = ""
    elif change == "branch":
        index.branch = "release"
    else:
        index.commit = "b" * 40
    report, _ = await preview(monkeypatch)
    discovery = report.records[0].code_discovery
    assert discovery.status == "deferred" and discovery.candidates == []


@pytest.mark.asyncio
async def test_a_moving_code_head_discards_the_grounded_candidates(monkeypatch, index):
    async def moving_chunks(self, org, namespace, model, keys):
        index.commit = "b" * 40
        return index.rows

    monkeypatch.setattr(CodeRepo, "chunks_by_keys", moving_chunks)
    report, _ = await preview(monkeypatch)
    discovery = report.records[0].code_discovery
    assert discovery.status == "deferred" and discovery.candidates == []
    assert "changed" in discovery.note


@pytest.mark.asyncio
async def test_unavailable_search_is_reported_without_changing_decision(monkeypatch, index):
    async def failed(*args, **kwargs):
        raise ConnectionError("unavailable")

    monkeypatch.setattr(retrieval, "search", failed)
    report, _ = await preview(monkeypatch)
    assert report.records[0].code_discovery.status == "unavailable"
    assert report.records[0].body["files"] == []


@pytest.mark.asyncio
async def test_authorization_refusal_reads_no_index(monkeypatch, index):
    from server.code.app import prose_candidates as candidates

    async def denied(org, namespace, role):
        return False

    monkeypatch.setattr(candidates, "row_in_scope", denied)
    result = await candidates.discover_candidates("other-org", "ns", PROSE)
    assert result.status == "unavailable" and index.calls == []


@pytest.mark.asyncio
async def test_scope_and_resource_bounds_are_explicit(monkeypatch, index):
    from server.code.app.prose_candidates import discover_candidates

    result = await discover_candidates("o", "ns", PROSE * 200)
    assert result.status == "candidates"
    request = index.calls[0]
    assert (request.org, request.namespace) == ("o", "ns")
    assert len(request.query) <= 2000
    assert request.top_k == 3 and request.deadline_ms == 1500
    assert request.llm_legs == "none" and request.rerank_endpoint == "none"
    assert getattr(request, "extra_terms", None) is None
    assert all((org, ns) == ("o", "ns") for org, ns, model, keys in index.ground_reads)


@pytest.mark.asyncio
async def test_quoted_exact_file_preserves_existing_link_path(monkeypatch, index):
    report, _ = await preview(monkeypatch, text=PROSE + " See src/delivery.py.")
    assert report.records[0].body["files"] == [PATH]
    assert report.records[0].code_discovery is None and index.calls == []


@pytest.mark.asyncio
async def test_discovery_cap_and_ungrounded_quotes_do_not_search(monkeypatch, index):
    from server.memory.domain.decision_extract import Candidate

    target = de.Target("source", "ns", "source")
    cand = Candidate("design:delivery", "retry policy", "retry three times", evidence=[PROSE])
    capped = await de._discover_code("o", target, cand, [PROSE], 8)
    assert capped.status == "deferred" and "limit" in capped.note
    ungrounded = await de._discover_code("o", target, cand, ["Different document"], 0)
    assert ungrounded.status == "deferred" and index.calls == []


@pytest.mark.asyncio
async def test_deadline_covers_stalled_metadata_reads(monkeypatch, index):
    import asyncio

    from server.code.app import prose_candidates as candidates

    timeout = asyncio.timeout
    monkeypatch.setattr(candidates.asyncio, "timeout", lambda seconds: timeout(0))

    async def stalled(self, org, namespace, *keys):
        await asyncio.Event().wait()

    monkeypatch.setattr(CodeRepo, "all_state", stalled)
    result = await candidates.discover_candidates("o", "ns", PROSE)
    assert result.status == "deferred" and result.candidates == []
    assert "deadline" in result.note and index.calls == []


@pytest.mark.parametrize("missing", ["repository", "head", "hits"])
@pytest.mark.asyncio
async def test_missing_index_and_empty_retrieval_have_distinct_states(monkeypatch, index, missing):
    from server.code.app.prose_candidates import discover_candidates

    ns = "ns"
    if missing == "repository":
        ns = "unconfigured"
    elif missing == "head":
        index.commit = ""
    else:
        index.result.units.clear()
    result = await discover_candidates("o", ns, PROSE)
    assert result.status == ("abstained" if missing == "hits" else "unavailable")
    assert result.candidates == []


@pytest.mark.asyncio
async def test_a_document_preview_defers_decisions_after_its_search_budget(monkeypatch, index):
    quotes = [f"When a delivery fails at gate {i}, stop retrying." for i in range(9)]
    raw = [
        dict(
            category="design:delivery",
            scenario=f"retry policy at gate {i}",
            outcome=f"stop retrying at gate {i}",
            evidence=[quote],
            reasons=[],
            rejected=[],
            mentions=[],
            stage="proposed",
        )
        for i, quote in enumerate(quotes)
    ]
    world = _World(monkeypatch, raw)
    world.content = "# Delivery\n\n" + "\n".join(quotes)

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, "\n".join(quotes), ("Delivery",))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await de.extract_document("o", "ns", "doc.md", dry_run=True, max_per_section=9)
    assert len(report.records) == 9
    assert len(index.calls) == 8
    assert [r.code_discovery.status for r in report.records] == ["candidates"] * 8 + ["deferred"]
    assert all(r.body["files"] == [] for r in report.records)


@pytest.mark.parametrize("phase", ["before_first_round", "before", "during"])
@pytest.mark.asyncio
async def test_cached_freshness_never_labels_partial_ingest_as_published_commit(
    monkeypatch, index, phase
):
    # Fetch publishes the next tip before chunk writes; git_head advances only last.
    async def partial_ingest(self, org, namespace, model, keys):
        index.clone = "b" * 40
        index.slice = f"{COMMIT}..{index.clone}:1"
        return [{**index.rows[0], "content": SOURCE + "\n# revision two"}]

    if phase.startswith("before"):
        index.clone = "b" * 40
        if phase == "before":
            index.slice = f"{COMMIT}..{index.clone}:1"
    else:
        monkeypatch.setattr(CodeRepo, "chunks_by_keys", partial_ingest)
    report, _ = await preview(monkeypatch)
    discovery = report.records[0].code_discovery
    assert discovery.status == "deferred" and discovery.candidates == []
    assert report.records[0].body["files"] == []


@pytest.mark.parametrize("incomplete", ["unpublished_tip", "slice", "bootstrap", "wrong_branch"])
@pytest.mark.asyncio
async def test_uncached_publication_must_be_complete(monkeypatch, index, incomplete):
    from server.code.app.prose_candidates import discover_candidates

    if incomplete == "unpublished_tip":
        index.clone = ""
    elif incomplete == "slice":
        index.slice = f"{COMMIT}..{COMMIT}:1"
    elif incomplete == "bootstrap":
        index.stage = "indexing"
    else:

        async def wrong_branch(self, org, ns):
            return {
                "git_head": {"value": COMMIT},
                "clone_head": {"value": COMMIT, "detail": "release"},
            }

        monkeypatch.setattr(CodeRepo, "all_state", wrong_branch)
    out = await discover_candidates("o", "ns", PROSE)
    assert out.status == "deferred" and out.candidates == [] and index.calls == []


@pytest.mark.asyncio
async def test_source_quotations_never_persist_in_code_namespace_telemetry(monkeypatch, index):
    from server.code.app import search as retrieval
    from server.code.app import search_cursor
    from server.code.app.search.state import CodeSearchResult
    from server.memory.domain.decision_extract import Candidate

    traces, pools = [], []
    result = CodeSearchResult(units=index.result.units, pool=index.result.units * 4)

    async def as_of(*args):
        return index.as_of

    async def no_note(*args):
        return ""

    async def finish(inp):
        return await search._finish_search(
            inp,
            "o",
            "ns",
            CodeRepo(),
            result,
            started=0,
            embedding_model="fake-ngram-3072",
            memo_key=None,
        )

    async def retrieve(*args, **kwargs):
        return result

    monkeypatch.setattr(search, "handle_code_search", finish)
    monkeypatch.setattr(search, "index_as_of", as_of)
    monkeypatch.setattr(search, "embedding_coverage_note", no_note)
    monkeypatch.setattr(search, "schedule_code_trace", lambda **row: traces.append(row["query"]))
    monkeypatch.setattr(
        search_cursor,
        "put_pool",
        lambda org, ns, key, payload, **kw: pools.append(payload["query"]),
    )
    monkeypatch.setattr(settings, "code_search_cursor_enabled", True)
    monkeypatch.setattr(retrieval, "search", retrieve)
    candidate = Candidate("design:delivery", "retry policy", "stop after three", evidence=[PROSE])
    target = de.Target("private-documents", "ns", "private-documents")
    discovery = await de._discover_code("o", target, candidate, [PROSE], 0)
    assert discovery.status == "candidates"
    assert traces == [] and pools == [], (
        "source text must remain outside code-namespace persistence"
    )


@pytest.mark.asyncio
async def test_disabled_code_intelligence_does_not_retrieve(monkeypatch, index):
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    result = await pc.discover_candidates("o", "ns", PROSE)
    assert result.status == "unavailable" and index.calls == []


@pytest.mark.asyncio
async def test_index_movement_defers_even_an_empty_search(monkeypatch, index):
    async def moving(repo, org, ns, **kwargs):
        index.clone = "b" * 40
        return CodeSearchResult(abstain=True, abstain_code="no_match")

    monkeypatch.setattr(retrieval, "search", moving)
    result = await pc.discover_candidates("o", "ns", PROSE)
    assert result.status == "deferred" and result.candidates == []
