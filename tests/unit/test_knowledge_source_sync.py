"""The document-source sync pass (server/knowledge/app/source_sync.py) over an
in-memory registry and document store and a shared-folder store answered by a
mock transport: what a first, an incremental and a full turn list, fetch, write
and retire, the stamp and status each leaves, and the bounds. Every response is
written here; none is a recording of real content."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from server.clients import gdrive
from server.core import leases
from server.core.errors import ValidationError
from server.core.hashing import sha256_text
from server.core.secrets_at_rest import seal
from server.knowledge.app import extractors, source_sync, writes
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import convert
from server.knowledge.domain.extract.artifact import Artifact, Block, Coverage
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, KnowledgeSource
from server.knowledge.store import documents
from server.memory.domain.corpus_synthesis import ground_citations
from server.memory.domain.provenance import pipeline_actor_var
from server.observability import audit
from server.settings import settings

ORG, NS = "acme", "code-acme-app"
FOLDER = "1AbCdEfGhIjKlMnOp"
OTHER = "2QrStUvWxYzAbCdEf"
DOCS = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
KEY = '{"type": "service_account", "client_email": "a@b", "private_key": "k"}'


def _file(file_id: str, mime: str, modified: str, **kw: Any) -> dict[str, Any]:
    kind = {DOCS: "document", SLIDES: "presentation"}.get(mime, "file")
    return {
        "id": file_id,
        "name": f"{file_id} title",
        "mimeType": mime,
        "modifiedTime": modified,
        "webViewLink": f"https://docs.example.com/{kind}/d/{file_id}/edit?usp=drivesdk",
        **kw,
    }


DECK = {
    "slides": [
        {
            "pageElements": [
                {
                    "shape": {
                        "placeholder": {"type": "TITLE"},
                        "text": {
                            "textElements": [{"textRun": {"content": "Why a planner pass\n"}}]
                        },
                    }
                },
                {
                    "shape": {
                        "text": {"textElements": [{"textRun": {"content": "The rewrite moves.\n"}}]}
                    }
                },
            ]
        }
    ]
}


class _World:
    """The registry, sync state and documents, and what the folder store answers."""

    def __init__(self) -> None:
        self.sources: list[KnowledgeSource] = []
        self.inactive: set[tuple[str, str]] = set()
        self.states: dict[str, dict[str, str]] = {}
        # Live documents by source_uri: {actor, tags, metadata}.
        self.docs: dict[str, dict[str, Any]] = {}
        self.files: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.audits: list[dict[str, Any]] = []
        self.upserts: list[Any] = []
        self.fail_listing: int = 0
        # Formats an extractor is configured for, and those its service declares.
        self.routes: set[str] = {"pdf", "pptx", "docx"}
        self.declared: set[str] = {"pdf", "pptx", "docx"}
        self.extractor_down = False
        self.extract_error: Exception | None = None
        # Per-file extraction failures, by file id.
        self.extract_errors: dict[str, Exception] = {}
        self.capability_calls = 0
        self.incomplete_search = False
        # Folders the account no longer sees; per-file answers that override the store's.
        self.lost_folders: set[str] = set()
        # A response, or a callable that answers (or raises) for that file.
        self.file_answers: dict[str, Any] = {}
        # The extraction leg: each extract_document call (uri, actor, attribution), the
        # report it answers (or a callable that makes one, or an exception), the orgs
        # switched off (an exception: the switch unread), the namespaces over their cap
        # share and each cap read's arguments, and the cost rows emitted.
        self.mined: list[tuple[str, str, Any]] = []
        self.mine_answer: Any = None
        self.switched_off: Any = frozenset()
        self.over_cap: set[tuple[str, str]] = set()
        self.cap_reads: list[dict[str, Any]] = []
        self.cost_rows: list[dict[str, Any]] = []
        # The source lease the current turn holds; a test loses it by setting `held` false.
        self.lease = SimpleNamespace(held=True)

    # store
    async def all_active_sources(self) -> list[KnowledgeSource]:
        return [s for s in self.sources if s.active]

    async def pending_delete_sources(self) -> list[KnowledgeSource]:
        return [s for s in self.sources if not s.active and s.pending_delete]

    async def inactive_namespaces(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        return {p for p in pairs if p in self.inactive}

    async def read_states(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        return {n: self.states[n] for n in names if n in self.states}

    async def write_state(self, org: str, ns: str, name: str, *, state_value: str, detail: Any):
        self.states[name] = {"state_value": state_value, "detail": json.dumps(detail)}

    async def get_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return next((s for s in self.sources if s.name == name and s.active), None)

    async def latest_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return next((s for s in reversed(self.sources) if s.name == name), None)

    async def insert_source(self, src: KnowledgeSource) -> None:
        self.sources = [s for s in self.sources if s.name != src.name] + [src]

    async def source_revisions(
        self, org: str, ns: str, name: str, *, searchable_only: bool = True, pictures: bool = False
    ) -> list[dict[str, str]]:
        return [
            {
                "source_uri": uri,
                "file_id": d["metadata"]["ingested_from"],
                "source_revision": d["metadata"]["source_revision"],
                "text_sha256": d["metadata"].get("text_sha256", ""),
                "inline_pictures": "1" if pictures and "]: <data:" in d.get("content", "") else "",
                "stored_content_sha256": sha256_text(d.get("content", ""))
                if pictures and "]: <data:" in d.get("content", "")
                else "",
            }
            for uri, d in self.docs.items()
            if d["actor"] == SYNC_WORKER_ACTOR
            and f"source:{name}" in d["tags"]
            and (d.get("chunks", True) or not searchable_only)
        ]

    async def source_documents(self, org: str, ns: str, name: str) -> list[dict[str, Any]]:
        return await self.source_revisions(org, ns, name, searchable_only=False)

    async def count_documents(self, org: str, ns: str, name: str) -> int:
        return len(await self.source_documents(org, ns, name))

    # the folder store
    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        file_id = path.rstrip("/").split("/")[-1 if not path.endswith("/export") else -2]
        if file_id in self.file_answers:
            answer = self.file_answers[file_id]
            return answer(request) if callable(answer) else answer
        if path in (f"/drive/v3/files/{FOLDER}", f"/drive/v3/files/{OTHER}"):
            if file_id in self.lost_folders:
                return httpx.Response(404)
            return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.folder"})
        if path == "/drive/v3/files":
            if self.fail_listing:
                return httpx.Response(self.fail_listing, json={"error": {"status": "x"}})
            q = request.url.params["q"]
            after = q.split("modifiedTime > '")[1].rstrip("'") if "modifiedTime >" in q else ""
            files = [
                f
                for f in self.files
                if f["modifiedTime"] > after and f"mimeType = '{f['mimeType']}'" in q
            ]
            return httpx.Response(
                200, json={"files": files, "incompleteSearch": self.incomplete_search}
            )
        if path.endswith("/export"):
            return httpx.Response(200, text="# Design\n\nWe chose the planner.")
        if request.url.host == "slides.googleapis.com":
            return httpx.Response(200, json=DECK)
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"%PDF-1.7 bytes")
        return httpx.Response(404)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 40)
    for name in (
        "all_active_sources",
        "pending_delete_sources",
        "inactive_namespaces",
        "read_states",
        "write_state",
        "get_source",
        "latest_source",
        "insert_source",
        "source_revisions",
        "source_documents",
        "count_documents",
    ):
        monkeypatch.setattr(source_sync.store, name, getattr(w, name))
    monkeypatch.setattr(source_ops, "store", source_sync.store)

    async def _trunk(org: str, ns: str) -> bool:
        return ns.startswith("code-")

    monkeypatch.setattr(source_ops, "is_trunk_namespace", _trunk)
    monkeypatch.setattr(source_sync, "_repo_tag", lambda src: "repo:abc")

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield True

    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        w.lease = SimpleNamespace(held=True)  # each turn's own lease
        yield w.lease

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(leases, "cluster_lease", _cluster)

    async def _token(account: Any) -> str:
        return "tok"

    monkeypatch.setattr(gdrive, "access_token", _token)
    client = httpx.AsyncClient(transport=httpx.MockTransport(w.answer))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        d = w.docs.get(uri)
        return (
            None
            if d is None
            else documents.DocumentRow(
                org=org,
                namespace=ns,
                source_uri=uri,
                doc_id="d",
                content=d.get("content", ""),
                version=1,
                ingested_at="2026-01-01T00:00:00",
                actor=d["actor"],
                tags=d["tags"],
            )
        )

    monkeypatch.setattr(documents, "get_one", _get_one)

    async def _upsert(inp: Any) -> Any:
        w.upserts.append((inp, pipeline_actor_var.get()))
        w.docs[inp.source_uri] = {
            "actor": pipeline_actor_var.get(),
            "tags": list(inp.tags),
            "metadata": dict(inp.metadata),
            "content": inp.content,
        }

    async def _delete(inp: Any) -> Any:
        gone = w.docs.pop(inp.source_uri, None)
        return writes.KnowledgeDeleteOutput(deleted=gone is not None, chunks_tombstoned=1)

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _upsert)
    monkeypatch.setattr(writes, "handle_knowledge_delete", _delete)

    async def _emit(event: str, **kw: Any) -> str:
        w.audits.append({"event": event, **kw})
        return "h"

    monkeypatch.setattr(audit, "emit", _emit)

    def _endpoint(fmt: str, media_type: str = "") -> str:
        return f"http://extractor-{fmt}" if fmt in w.routes else ""

    async def _capabilities(url: str) -> extractors.Capabilities:
        w.capability_calls += 1
        if w.extractor_down:
            raise extractors.ExtractorUnavailable("the extractor is unreachable")
        return extractors.Capabilities(name="x", formats=frozenset(w.declared), max_bytes=10**9)

    async def _extract(data: bytes, **kw: Any) -> Artifact:
        if w.extract_error is not None:
            raise w.extract_error
        file_id = kw["name"].removesuffix(" title")
        if file_id in w.extract_errors:
            raise w.extract_errors[file_id]
        return Artifact(
            format=kw["declared_format"],
            parser="test",
            parser_version="1",
            blocks=(
                Block(id="b0", type="heading", text="Decision", depth=1),
                Block(id="b1", type="paragraph", text="Keep the hash join."),
            ),
            coverage=Coverage(unit="pages", total=1, extracted=1),
        )

    monkeypatch.setattr(extractors, "endpoint_for", _endpoint)
    monkeypatch.setattr(extractors, "capabilities", _capabilities)
    monkeypatch.setattr(extractors, "extract", _extract)
    _mock_mining(monkeypatch, w)
    w.sources.append(_source())
    return w


def _mock_mining(monkeypatch: pytest.MonkeyPatch, w: _World) -> None:
    from server.llm import customer
    from server.memory.app import decision_extract
    from server.observability import budget, cost_accumulator, events
    from server.workers import cost_event

    async def _extract_document(
        org: str,
        ns: str,
        uri: str,
        *,
        dry_run: bool,
        read_deadline: float,
        may_write: Any,
        writes: Any,
    ) -> Any:
        assert (org, ns, dry_run) == (ORG, NS, False) and read_deadline > 0
        assert may_write()
        w.mined.append((uri, pipeline_actor_var.get(), customer.attribution()))
        cost_accumulator.report_llm("m", 10, 5)
        answer = w.mine_answer
        if isinstance(answer, Exception):
            raise answer
        report = answer(uri) if callable(answer) else None
        return report or decision_extract.ExtractReport(uri, dry_run)

    async def _switched_off() -> frozenset[str]:
        if isinstance(w.switched_off, Exception):
            raise w.switched_off
        return w.switched_off

    async def _over(org: str, ns: str, **kw: Any) -> bool:
        w.cap_reads.append(kw)
        return (org, ns) in w.over_cap

    class _Emitter:
        async def flush_now(self) -> None:
            return None

    def _cost(**kw: Any) -> None:
        acc = cost_accumulator.current()
        spent = acc.drain().llm_input_tokens if acc is not None else 0
        w.cost_rows.append({**kw, "llm_input_tokens": spent})

    monkeypatch.setattr(decision_extract, "no_model_error", lambda: "")
    monkeypatch.setattr(decision_extract, "extract_document", _extract_document)
    monkeypatch.setattr(customer, "switched_off_orgs", _switched_off)
    monkeypatch.setattr(budget, "namespace_over_cap", _over)
    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", _cost)


def _source(name: str = "decks", **kw: Any) -> KnowledgeSource:
    return KnowledgeSource(
        org=ORG,
        namespace=kw.pop("namespace", NS),
        name=name,
        kind="gdrive",
        source_id=kw.pop("source_id", f"id-{name}"),
        config=kw.pop(
            "config", {"folder_ids": [FOLDER], "include": ["docs", "slides", "pdf", "pptx"]}
        ),
        credential_enc=kw.pop("credential_enc", seal("knowledge_sources.credential", KEY)),
        **kw,
    )


def _detail(world: _World, name: str = "decks") -> dict[str, Any]:
    return json.loads(world.states[name]["detail"])


def _listings(world: _World) -> list[str]:
    return [r.url.params["q"] for r in world.requests if r.url.path == "/drive/v3/files"]


def test_a_first_turn_lists_everything_and_writes_each_kind(world: _World) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("deck1", SLIDES, "2026-01-02T00:00:00.000Z"),
        _file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="14"),
    ]
    result = asyncio.run(source_sync.run_sync())
    assert (result.synced, result.changed, result.failed) == (1, 3, 0)
    assert "modifiedTime >" not in _listings(world)[0]
    by_uri = {inp.source_uri: (inp, actor) for inp, actor in world.upserts}
    deck, actor = by_uri["https://docs.example.com/presentation/d/deck1/edit"]
    assert actor == SYNC_WORKER_ACTOR
    assert deck.content.startswith("# Slide 1: Why a planner pass")
    assert deck.tags == ["source-kind:design_doc", "repo:abc", "source:decks"]
    assert deck.source_class == "imported"
    assert deck.metadata["source_revision"] == "2026-01-02T00:00:00.000Z"
    assert deck.original_bytes_b64 is None
    pdf, _ = by_uri["https://docs.example.com/file/d/pdf1/edit"]
    assert pdf.content == "# Decision\n\nKeep the hash join."
    assert pdf.original_bytes_b64 and pdf.blob_content_type == PDF
    assert world.states["decks"]["state_value"] == "2026-01-03T00:00:00.000Z"
    detail = _detail(world)
    assert (detail["status"], detail["documents_seen"], detail["changed"]) == ("ok", 3, 3)
    assert detail["full_listed_at"]


def test_a_later_turn_lists_only_changes_and_skips_unchanged_files(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.files.append(_file("doc2", DOCS, "2026-01-05T00:00:00.000Z"))
    world.upserts.clear()
    asyncio.run(source_sync.run_sync())
    assert "modifiedTime > '2026-01-01T00:00:00.000Z'" in _listings(world)[-1]
    assert [inp.source_uri for inp, _ in world.upserts] == [
        "https://docs.example.com/document/d/doc2/edit"
    ]
    # A full listing that returns a stored file at its stored revision fetches nothing.
    world.upserts.clear()
    world.requests.clear()
    asyncio.run(source_sync.run_sync(full=True))
    assert world.upserts == []
    assert [r.url.path for r in world.requests] == ["/drive/v3/files", f"/drive/v3/files/{FOLDER}"]


OLD_EXPORT = "# Design\n\n![][image1]\n\n[image1]: <data:image/png;base64,AAAA>\n"


def _stored(world: _World, file_id: str, mime: str, content: str) -> str:
    """A copy of `file_id` the sync stored at REV1."""
    uri = source_sync.source_uri_of(_file(file_id, mime, REV1))
    world.docs[uri] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source-kind:design_doc", "source:decks"],
        "metadata": {"ingested_from": file_id, "source_revision": REV1},
        "content": content,
    }
    return uri


def test_a_document_stored_with_its_pictures_bytes_is_converted_and_mined_again(
    world: _World,
) -> None:
    world.files = [_file("doc1", DOCS, REV1)]
    uri = _stored(world, "doc1", DOCS, OLD_EXPORT)
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"mined": {uri: REV1}})}
    asyncio.run(source_sync.run_sync())
    assert [inp.source_uri for inp, _ in world.upserts] == [uri]
    assert "data:" not in world.docs[uri]["content"]
    assert [u for u, _, _ in world.mined] == [uri]
    world.upserts.clear()
    asyncio.run(source_sync.run_sync(full=True))
    assert world.upserts == []


def test_a_definition_shown_in_code_is_fetched_but_neither_written_nor_mined_again(
    world: _World,
) -> None:
    sample = "# Export\n\n```md\n[image1]: <data:image/png;base64,AAAA>\n```\n"
    world.files = [_file("doc1", DOCS, REV1)]
    world.file_answers["doc1"] = httpx.Response(200, text=sample)
    uri = _stored(world, "doc1", DOCS, sample)
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"mined": {uri: REV1}})}
    for _ in range(2):
        asyncio.run(source_sync.run_sync(full=True))
    assert world.upserts == [] and world.mined == []
    assert _detail(world)["mined"] == {uri: REV1}


def test_a_flagged_copy_whose_address_moved_is_written_at_the_new_one(world: _World) -> None:
    old = _stored(world, "doc1", DOCS, OLD_EXPORT)
    moved = _file("doc1", DOCS, REV1, webViewLink="https://docs.example.com/document/d/doc1/v2")
    world.files = [moved]
    asyncio.run(source_sync.run_sync(full=True))
    new = source_sync.source_uri_of(moved)
    assert new != old and old not in world.docs
    assert "data:" not in world.docs[new]["content"]


def test_unchanged_flagged_copies_do_not_hold_a_full_listing_at_the_pass_bound(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    sample = "# Export\n\n```md\n[image1]: <data:image/png;base64,AAAA>\n```\n"
    world.files = [_file(f"doc{i}", DOCS, REV1) for i in (1, 2)]
    world.files.append(_file("doc3", DOCS, "2026-01-02T00:00:00.000Z"))
    for i in (1, 2):
        world.file_answers[f"doc{i}"] = httpx.Response(200, text=sample)
        _stored(world, f"doc{i}", DOCS, sample)
    for _ in range(3):
        asyncio.run(source_sync.run_sync(full=True))
    # The two were exported once each at their revision; the file after them was written.
    exports = [r for r in world.requests if r.url.path.endswith("/export")]
    assert sorted(r.url.path for r in exports if "doc3" not in r.url.path) == [
        "/drive/v3/files/doc1/export",
        "/drive/v3/files/doc2/export",
    ]
    assert _uri("doc3") in world.docs and _detail(world)["status"] == "ok"
    # Once a file is listed at another revision, its record goes.
    world.files[0] = _file("doc1", DOCS, "2026-03-01T00:00:00.000Z")
    asyncio.run(source_sync.run_sync(full=True))
    assert "doc1" not in _detail(world)["pictures_checked"]


def test_a_document_stored_without_pictures_bytes_is_not_fetched_again(world: _World) -> None:
    world.files = [_file("doc1", DOCS, REV1)]
    _stored(world, "doc1", DOCS, "# Design\n\nText only.\n")
    asyncio.run(source_sync.run_sync(full=True))
    assert world.upserts == []


def test_a_turn_cut_after_a_reconverting_write_still_mines_the_new_text(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, REV1)]
    uri = _stored(world, "doc1", DOCS, OLD_EXPORT)
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"mined": {uri: REV1}})}
    real = source_sync._write_state

    async def _cut(*a: Any, **kw: Any) -> None:
        raise RuntimeError("cut before the turn's state")

    monkeypatch.setattr(source_sync, "_write_state", _cut)
    asyncio.run(source_sync.run_sync())
    monkeypatch.setattr(source_sync, "_write_state", real)
    asyncio.run(source_sync.run_sync())
    assert uri in [u for u, _, _ in world.mined]


def test_a_failed_reconverting_write_leaves_the_document_to_be_mined_again(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, REV1)]
    uri = _stored(world, "doc1", DOCS, OLD_EXPORT)
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"mined": {uri: REV1}})}
    real = writes.handle_knowledge_upsert

    async def _down(inp: Any) -> Any:
        raise RuntimeError("store down")

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _down)
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["file_waits"]["doc1"][2] == "write_failed:RuntimeError"
    assert "data:" in world.docs[uri]["content"]
    monkeypatch.setattr(writes, "handle_knowledge_upsert", real)
    asyncio.run(source_sync.run_sync(full=True))
    assert "data:" not in world.docs[uri]["content"]
    # The failed write left the old copy and its mined key: only the new text is mined.
    assert [u for u, _, _ in world.mined] == [uri]


def test_a_document_given_up_on_is_not_fetched_again_for_its_pictures(world: _World) -> None:
    world.files = [_file("doc1", DOCS, REV1)]
    _stored(world, "doc1", DOCS, OLD_EXPORT)
    waits = {"doc1": [REV1, 3, "http_500"]}
    # A periodic full listing whose one more try was already granted.
    listed = "2026-01-02T00:00:00+00:00"
    detail = {"file_waits": waits, "full_listed_at": listed, "retry_granted_for": listed}
    world.states["decks"] = {"state_value": REV1, "detail": json.dumps(detail)}
    asyncio.run(source_sync.run_sync())
    assert world.upserts == []
    detail = _detail(world)
    assert (detail["file_waits"], detail["unconverted"]) == (waits, 0)


@pytest.mark.parametrize(("file_id", "mime"), [("deck1", SLIDES), ("pdf1", PDF)])
def test_a_deck_or_uploaded_file_is_not_fetched_again_for_pictures_bytes(
    world: _World, file_id: str, mime: str
) -> None:
    world.files = [_file(file_id, mime, REV1, size="14")]
    _stored(world, file_id, mime, OLD_EXPORT)
    asyncio.run(source_sync.run_sync(full=True))
    assert world.upserts == []


def test_a_document_export_keeps_its_text_and_marks_its_inlined_pictures() -> None:
    data = "data:image/png;base64," + "iVBORw0KGgo" * 40
    exported = (
        "# Design\n\nThe planner runs first.\n\n![][image1]\n\n![][image2]\n\n"
        "* ![][image3] the operator version shown\n* ![][image2]\n> ![Flow][image1]\n\n"
        "![Flow][image4]*Figure 2: flow*\n\n```md\n![][image1]\n\n\n  indented\n```\n\n"
        "See [the spec][spec].\n\n"
        f"[image1]: <{data}>\n\n[image2]: <{data}>\n\n[image3]: <{data}>\n\n"
        f"[image4]: <{data}>\n\n[spec]: https://example.com/spec\n"
    )
    assert convert.exported_markdown(exported) == (
        "# Design\n\nThe planner runs first.\n\n"
        "> [image-only: 2 pictures, rationale not extracted]\n\n"
        "* the operator version shown\n"
        "* [image-only: 1 picture, rationale not extracted]\n"
        "> [image-only: 1 picture, rationale not extracted] Flow\n\n"
        "Flow *Figure 2: flow*\n\n```md\n![][image1]\n\n\n  indented\n```\n\n"
        "See [the spec][spec].\n\n[spec]: https://example.com/spec\n"
    )
    nested = "1. Step\n\n   ![][image1]\n\n2. Next\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(nested) == (
        "1. Step\n\n   > [image-only: 1 picture, rationale not extracted]\n\n2. Next\n"
    )
    heading = "# ![][image1]\n\nText.\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(heading) == (
        "# [image-only: 1 picture, rationale not extracted]\n\nText.\n"
    )
    joined = "one![][image1]two\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(joined) == "one two\n"
    inline = "![](data:image/png;base64,AAAA)\n\nA ![Graph](data:image/png;base64,AAAA) here.\n"
    assert convert.exported_markdown(inline) == (
        "> [image-only: 1 picture, rationale not extracted]\n\nA Graph here.\n"
    )
    tilde = "~~~\n![][image1]\n~~~\n``` x ` ```\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(tilde) == "~~~\n![][image1]\n~~~\n``` x ` ```\n"
    prose = "[Step 1]: data: the raw input\n\n[image1]: <data:a>\n"
    assert convert.exported_markdown(prose) == "[Step 1]: data: the raw input\n"
    emphasis = "*![][image1]*\n\nx **![Flow][image1]** y\n\n[image1]: <data:a>\n"
    assert convert.exported_markdown(emphasis) == (
        "> [image-only: 1 picture, rationale not extracted]\n\nx **Flow** y\n"
    )
    private = "a \ue000 ![][image1] b\n\n[image1]: <data:a>\n"
    assert convert.exported_markdown(private) == "a \ue000 b\n"
    bracketed = "see (![Fig][image1]) here\n\n[image1]: <data:a>\n"
    assert convert.exported_markdown(bracketed) == "see (Fig) here\n"
    ended = "The flow is ![][image1]. Then ![Flow][image1], done.\n\n[image1]: <data:a>\n"
    assert convert.exported_markdown(ended) == "The flow is. Then Flow, done.\n"
    spaced = "see![][image1] next\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(spaced) == "see next\n"
    captioned = "see![Flow][image1] next\n\n[image1]: <data:image/png;base64,AAAA>\n"
    assert convert.exported_markdown(captioned) == "see Flow next\n"
    plain = "# Plain\n\nNo pictures.\n\n\n```\n  kept\n```"
    assert convert.exported_markdown(plain) == plain


def test_a_full_listing_retires_what_the_folder_no_longer_has(world: _World) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    asyncio.run(source_sync.run_sync())
    world.files = world.files[1:]
    # An incremental turn sees no absence: nothing is retired.
    asyncio.run(source_sync.run_sync())
    assert len(world.docs) == 2
    result = asyncio.run(source_sync.run_sync(full=True))
    assert result.removed == 1
    assert list(world.docs) == ["https://docs.example.com/document/d/doc2/edit"]
    assert world.audits == [
        {
            "event": "knowledge.delete",
            "org": ORG,
            "namespace": NS,
            "actor": SYNC_WORKER_ACTOR,
            "subject": {
                "source_uri": "https://docs.example.com/document/d/doc1/edit",
                "source_name": "decks",
            },
        }
    ]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, {}, "rate_limited"),
        (403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, "rate_limited"),
        (403, {}, "credential_invalid"),
        (500, {}, "failed"),
    ],
)
def test_a_failed_listing_keeps_the_stamp_and_retires_nothing(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, Any],
    expected: str,
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.requests.clear()

    def _fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_fail))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    result = asyncio.run(source_sync.run_sync(full=True))
    assert result.failed == 1
    detail = _detail(world)
    assert detail["status"] == expected
    # The state names the status, never the store's answer.
    assert "Exceeded" not in detail["error"]
    assert world.states["decks"]["state_value"] == "2026-01-01T00:00:00.000Z"
    assert len(world.docs) == 1


def test_a_credential_that_no_longer_opens_fetches_nothing(world: _World) -> None:
    world.sources = [_source(credential_enc="not-a-sealed-value")]
    asyncio.run(source_sync.run_sync())
    assert world.requests == []
    assert _detail(world)["status"] == "credential_invalid"


def test_the_document_bound_cuts_the_turn_and_holds_the_stamp(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    world.files = [_file(f"doc{i}", DOCS, f"2026-01-0{i}T00:00:00.000Z") for i in range(1, 6)]
    world.states["decks"] = {
        "state_value": "",
        "detail": json.dumps({"requested_at": "2026-01-09T00:00:00+00:00", "full": True}),
    }
    first = asyncio.run(source_sync.run_sync())
    assert first.changed == 2
    assert world.states["decks"]["state_value"] == ""
    # The request stays until a turn finishes it.
    assert _detail(world)["requested_at"]
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync())
    assert len(world.docs) == 5
    assert world.states["decks"]["state_value"] == "2026-01-05T00:00:00.000Z"
    assert "requested_at" not in _detail(world)


def test_a_file_no_extractor_serves_is_unconverted(world: _World) -> None:
    world.routes = set()
    world.files = [_file("pdf1", PDF, "2026-01-03T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    assert _detail(world)["unconverted"] == 1
    assert not [r for r in world.requests if r.url.params.get("alt") == "media"]


def test_a_document_another_writer_holds_is_never_overwritten(world: _World) -> None:
    uri = "https://docs.example.com/document/d/doc1/edit"
    world.docs[uri] = {"actor": "human:ann", "tags": ["source:decks"], "metadata": {}}
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert world.upserts == []
    assert _detail(world)["skipped"] == 1
    # Nothing was fetched for it, and it cost none of the pass's bound.
    assert not [r for r in world.requests if r.url.path.endswith("/export")]


def test_sources_outside_a_live_trunk_are_not_synced_and_requests_go_first(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [
        _source("aaa"),
        _source("zzz"),
        _source("gone", namespace="code-gone"),
        _source("plain", namespace="notes"),
    ]
    world.inactive = {(ORG, "code-gone")}
    world.states["zzz"] = {"state_value": "", "detail": json.dumps({"requested_at": "t"})}
    order: list[str] = []

    async def _turn(src: KnowledgeSource, *a: Any) -> Any:
        order.append(src.name)
        return source_sync.TurnResult()

    monkeypatch.setattr(source_sync, "_turn", _turn)
    asyncio.run(source_sync.run_sync())
    assert order == ["zzz", "aaa"]


def test_a_removed_source_s_documents_are_retired_then_the_request_cleared(
    world: _World,
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]
    result = asyncio.run(source_sync.run_sync())
    assert result.retired == 1
    assert world.docs == {}
    assert world.audits[-1]["subject"]["source_name"] == "decks"
    assert not world.sources[-1].pending_delete


def test_a_source_replaced_during_its_turn_writes_and_records_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    replaced = _source(source_id="another")

    async def _get(org: str, ns: str, name: str) -> KnowledgeSource:
        return replaced

    monkeypatch.setattr(source_sync.store, "get_source", _get)
    result = asyncio.run(source_sync.run_sync())
    assert (result.synced, result.failed) == (0, 0)
    assert world.upserts == []
    assert "decks" not in world.states


def test_a_remove_during_the_turn_stops_it_before_the_next_write(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    real = writes.handle_knowledge_upsert

    async def _upsert_then_remove(inp: Any) -> Any:
        await real(inp)
        # The maintainer removes the source while the turn runs.
        world.sources = [dataclasses.replace(world.sources[0], active=False)]

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _upsert_then_remove)
    asyncio.run(source_sync.run_sync())
    assert [inp.source_uri for inp, _ in world.upserts] == [
        "https://docs.example.com/document/d/doc1/edit"
    ]
    assert "decks" not in world.states


def test_a_synced_document_s_address_grounds_as_a_citation() -> None:
    uri = source_sync.source_uri_of(_file("deck1", SLIDES, "2026-01-01T00:00:00.000Z"))
    assert uri == "https://docs.example.com/presentation/d/deck1/edit"
    answer, kept, removed = ground_citations(f"The pass moves [{uri}#2].", {(uri, 2)})
    assert (answer, kept, removed) == (f"The pass moves [{uri}#2].", [f"{uri}#2"], [])
    answer, kept, removed = ground_citations(f"The pass moves [{uri}#3].", {(uri, 2)})
    assert kept == [] and removed == [f"{uri}#3"]


def test_slides_become_one_section_each() -> None:
    slide = {
        "pageElements": [
            {"shape": {"placeholder": {"type": "TITLE"}, "text": _text("Join order\n")}},
            {"elementGroup": {"children": [{"shape": {"text": _text("Grouped text\n")}}]}},
            {
                "table": {
                    "tableRows": [
                        {"tableCells": [{"text": _text("a")}, {"text": _text("b")}]},
                    ]
                }
            },
            {"shape": {"text": _text(" ".join(str(n) for n in range(10)) + " end\n")}},
        ],
        "slideProperties": {
            "notesPage": {
                "notesProperties": {"speakerNotesObjectId": "n1"},
                "pageElements": [{"objectId": "n1", "shape": {"text": _text("Say why.\n")}}],
            }
        },
    }
    assert convert.slide_markdown(2, slide) == (
        "# Slide 2: Join order\n\nGrouped text\n\na | b\n\nend\n\n> Notes: Say why."
    )


def test_a_slide_that_is_a_picture_says_so() -> None:
    slide = {"pageElements": [{"image": {}}, {"image": {}}, {"shape": {"text": _text("Fig\n")}}]}
    assert convert.slide_markdown(1, slide).endswith(
        "> [image-only: 2 pictures, rationale not extracted]"
    )


def test_short_number_runs_stay() -> None:
    assert convert.drop_number_runs(["1 2 3", "4 5 6 7 8 x"]) == ["x"]
    assert convert.drop_number_runs(["v1 2 3 4"]) == ["v1 2 3 4"]


def _text(content: str) -> dict[str, Any]:
    return {"textElements": [{"textRun": {"content": content}}, {"paragraphMarker": {}}]}


def test_the_listing_pages_through_and_refuses_a_stamp_it_did_not_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "pageToken" in request.url.params:
            return httpx.Response(200, json={"files": [{"id": "b"}]})
        return httpx.Response(200, json={"files": [{"id": "a"}], "nextPageToken": "p2"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    files, whole = asyncio.run(gdrive.list_all("tok", [FOLDER], [DOCS], pace_s=0))
    assert [f["id"] for f in files] == ["a", "b"] and whole
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    with pytest.raises(ValueError):
        asyncio.run(gdrive.list_all("tok", [FOLDER], [DOCS], modified_after="x' or '1"))


def test_a_download_past_the_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    )
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    with pytest.raises(gdrive.TooLarge):
        asyncio.run(gdrive.download("tok", "abc", max_bytes=1000))
    with pytest.raises(ValueError):
        asyncio.run(gdrive.download("tok", "../x", max_bytes=1000))


def test_the_worker_row_reads_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.workers import registry

    monkeypatch.setattr(settings, "knowledge_source_sync_enabled", True)
    monkeypatch.setattr(settings, "knowledge_source_sync_interval_seconds", 600)
    monkeypatch.setattr(settings, "knowledge_source_sync_idle_interval_seconds", 1200)
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 300)
    [spec] = [s for s in registry.enabled_workers() if s.name == "knowledge_source_sync"]
    assert (spec.interval_seconds, spec.idle_interval_seconds) == (600, 1200)
    assert spec.deadline_seconds() == 300.0


def test_a_turn_lists_everything_again_once_the_full_interval_passed(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync())
    assert "modifiedTime >" in _listings(world)[-1]
    monkeypatch.setattr(settings, "knowledge_source_sync_full_interval_seconds", 0)
    asyncio.run(source_sync.run_sync())
    assert "modifiedTime >" not in _listings(world)[-1]


def test_a_file_over_the_size_bound_is_not_downloaded(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 10)
    world.files = [_file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="11")]
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    assert _detail(world)["unconverted"] == 1
    assert not [r for r in world.requests if r.url.params.get("alt") == "media"]


def _uri(file_id: str, kind: str = "document") -> str:
    return f"https://docs.example.com/{kind}/d/{file_id}/edit"


def test_a_file_the_store_refuses_is_unconverted_and_the_turn_goes_on(world: _World) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
        _file("doc3", DOCS, "2026-01-03T00:00:00.000Z"),
    ]
    world.file_answers["doc2"] = httpx.Response(403, json={"error": {"status": "denied"}})
    asyncio.run(source_sync.run_sync())
    assert sorted(world.docs) == [_uri("doc1"), _uri("doc3")]
    detail = _detail(world)
    assert (detail["status"], detail["unconverted"]) == ("ok", 1)
    assert detail["unconverted_reasons"] == {"http_403": 1}
    assert world.states["decks"]["state_value"] == "2026-01-03T00:00:00.000Z"


def test_a_full_listing_of_a_folder_the_account_lost_retires_nothing(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    # Unshared: the store lists the folder empty, and the folder itself answers 404.
    world.files = []
    world.lost_folders = {FOLDER}
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == [_uri("doc1")]
    assert world.audits == []
    assert _detail(world)["status"] == "failed"


def test_a_write_cut_before_its_chunks_is_done_again(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    real = writes.handle_knowledge_upsert

    async def _cut(inp: Any) -> Any:
        await real(inp)
        world.docs[inp.source_uri]["chunks"] = False  # the document row landed, no chunk
        raise RuntimeError("cut")

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _cut)
    asyncio.run(source_sync.run_sync())
    # The write failed in a way that may pass: the file waits and the turn ends
    # failed, by the error's class only.
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == ("failed", "RuntimeError")
    monkeypatch.setattr(writes, "handle_knowledge_upsert", real)
    world.upserts.clear()
    asyncio.run(source_sync.run_sync())
    assert [inp.source_uri for inp, _ in world.upserts] == [_uri("doc1")]
    assert world.docs[_uri("doc1")].get("chunks", True)


def test_an_extractor_whose_capabilities_cannot_be_read_holds_the_turn(world: _World) -> None:
    world.extractor_down = True
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="14"),
    ]
    for _ in range(4):
        asyncio.run(source_sync.run_sync())
        detail = _detail(world)
        assert detail["status"] == "failed" and "extractor_unavailable" in detail["error"]
        # The whole format is down, which is the turn's: no file is given up on.
        assert world.states["decks"]["state_value"] == ""
        assert detail["unconverted"] == 0
    assert list(world.docs) == [_uri("doc1")]
    assert not [r for r in world.requests if r.url.params.get("alt") == "media"]


def test_one_file_s_extraction_outage_waits_for_that_file_alone(world: _World) -> None:
    world.extract_errors["pdf1"] = extractors.ExtractorUnavailable("timed out")
    world.files = [
        _file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="14"),
        _file("pdf2", PDF, "2026-01-02T00:00:00.000Z", size="14"),
    ]
    asyncio.run(source_sync.run_sync())
    # The format's other file is served the same pass.
    assert list(world.docs) == [_uri("pdf2", "file")]
    assert _detail(world)["status"] == "failed"
    assert world.states["decks"]["state_value"] == ""
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["status"] == "ok"
    assert detail["unconverted_reasons"] == {"extractor_unavailable": 1}
    assert world.states["decks"]["state_value"] == "2026-01-02T00:00:00.000Z"
    # Given up on its revision: an incremental turn does not try it again...
    del world.extract_errors["pdf1"]
    world.upserts.clear()
    asyncio.run(source_sync.run_sync())
    assert world.upserts == []
    # ...and a new revision is tried again.
    world.files[0] = _file("pdf1", PDF, "2026-01-05T00:00:00.000Z", size="14")
    asyncio.run(source_sync.run_sync())
    assert _uri("pdf1", "file") in world.docs


def test_a_file_the_extractor_refuses_is_unconverted(world: _World) -> None:
    world.extract_error = extractors.ExtractionFailed("corrupt", "the file is damaged")
    world.files = [_file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    detail = _detail(world)
    assert detail["unconverted_reasons"] == {"extraction_failed:corrupt": 1}
    assert world.states["decks"]["state_value"] == "2026-01-03T00:00:00.000Z"


@pytest.mark.parametrize(("mime", "fmt"), [(PPTX, "pptx"), (DOCX, "docx")])
def test_an_uploaded_deck_or_document_goes_to_the_extractor_with_its_bytes_kept(
    world: _World, mime: str, fmt: str
) -> None:
    world.sources = [_source(config={"folder_ids": [FOLDER], "include": [fmt]})]
    world.files = [_file("file2", mime, "2026-01-03T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    [(inp, _)] = world.upserts
    assert inp.metadata["ingested_format"] == fmt
    assert inp.metadata["extraction_coverage"]["complete"] is True
    assert inp.blob_content_type == mime and inp.original_bytes_b64


def test_a_changed_address_retires_the_old_one(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.files = [
        _file(
            "doc1",
            DOCS,
            "2026-01-02T00:00:00.000Z",
            webViewLink="https://docs.example.com/document/d/doc1/view",
        )
    ]
    result = asyncio.run(source_sync.run_sync())
    assert list(world.docs) == ["https://docs.example.com/document/d/doc1/view"]
    assert result.removed == 1


def test_two_sources_sharing_a_folder_do_not_take_each_other_s_documents(
    world: _World,
) -> None:
    world.sources = [_source("aaa"), _source("bbb")]
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync(full=True))
    assert world.docs[_uri("doc1")]["tags"][-1] == "source:aaa"
    assert len(world.upserts) == 1
    assert _detail(world, "bbb")["skipped"] == 1
    assert world.audits == []


def test_a_second_source_with_only_docx_adds_a_folder_s_docx_files_and_nothing_else(
    world: _World,
) -> None:
    world.sources = [
        _source("aaa"),
        _source("bbb", config={"folder_ids": [FOLDER], "include": ["docx"]}),
    ]
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("docx1", DOCX, "2026-01-02T00:00:00.000Z", size="14"),
    ]
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync(full=True))
    tags = {uri: d["tags"][-1] for uri, d in world.docs.items()}
    assert tags == {_uri("doc1"): "source:aaa", _uri("docx1", "file"): "source:bbb"}
    assert len(world.upserts) == 2
    assert world.audits == []


def test_a_source_another_copy_holds_is_skipped(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb")]
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]

    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        if name.endswith("/aaa"):
            raise leases.LeaseBusy(name)
        yield world.lease

    monkeypatch.setattr(leases, "cluster_lease", _cluster)
    result = asyncio.run(source_sync.run_sync())
    assert (result.synced, result.failed) == (1, 0)
    assert "aaa" not in world.states and "bbb" in world.states


def test_a_cut_pass_resumes_at_the_source_it_did_not_start(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb"), _source("ccc")]
    cursor = {"at": ""}
    monkeypatch.setattr(source_sync.pass_context, "resume_cursor", lambda: cursor["at"])
    monkeypatch.setattr(
        source_sync.pass_context, "set_cursor", lambda value: cursor.update(at=value)
    )
    order: list[str] = []

    async def _turn(src: KnowledgeSource, state: Any, run: Any) -> Any:
        order.append(src.name)
        run.left -= 1
        return source_sync.TurnResult()

    monkeypatch.setattr(source_sync, "_turn", _turn)
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    first = asyncio.run(source_sync.run_sync())
    assert first.cut and order == ["aaa", "bbb"]
    assert cursor["at"] == f"{ORG}/{NS}/ccc"
    asyncio.run(source_sync.run_sync())
    assert order[2:] == ["ccc", "aaa"]


def test_a_pass_past_its_time_budget_starts_no_source(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The smallest pass the setting allows, with no share of it left to start a source.
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 180)
    monkeypatch.setattr(source_sync, "_BUDGET_FRACTION", 0.0)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    result = asyncio.run(source_sync.run_sync())
    assert result.cut and result.synced == 0
    assert world.requests == []


def test_a_failed_retirement_of_a_removed_source_counts_as_a_failure(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]

    async def _fail(inp: Any) -> Any:
        raise RuntimeError("store down")

    monkeypatch.setattr(writes, "handle_knowledge_delete", _fail)
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 1 and result.retired == 0
    assert world.sources[-1].pending_delete


def test_a_deck_over_the_size_bound_is_unconverted(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 10)
    world.files = [_file("deck1", SLIDES, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    assert _detail(world)["unconverted_reasons"] == {"too_large": 1}


def test_an_artifact_fences_its_code_and_reports_its_coverage() -> None:
    artifact = Artifact(
        format="pdf",
        parser="test",
        parser_version="1",
        blocks=(
            Block(id="b0", type="heading", text="Query", depth=2),
            Block(id="b1", type="sql_statement", text="SELECT 1"),
            Block(id="b2", type="code", text="x = ```y```"),
        ),
        coverage=Coverage(unit="pages", total=4, extracted=3),
    )
    assert convert.artifact_markdown(artifact) == (
        "## Query\n\n```sql\nSELECT 1\n```\n\n````\nx = ```y```\n````"
    )
    assert convert.coverage_of(artifact) == {
        "complete": False,
        "unit": "pages",
        "total": 4,
        "extracted": 3,
        "lost": [],
    }


def _waits(world: _World) -> dict[str, Any]:
    return dict(_detail(world).get("file_waits") or {})


def test_two_files_that_start_waiting_on_different_passes_each_wait_their_own(
    world: _World,
) -> None:
    world.extract_errors["pdf1"] = extractors.ExtractorUnavailable("timed out")
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    world.files.append(_file("pdf2", PDF, "2026-01-02T00:00:00.000Z", size="14"))
    world.extract_errors["pdf2"] = extractors.ExtractorUnavailable("timed out")
    asyncio.run(source_sync.run_sync())
    assert {k: v[1] for k, v in _waits(world).items()} == {"pdf1": 2, "pdf2": 1}
    asyncio.run(source_sync.run_sync())
    # pdf1 gave up on its third pass; pdf2 still holds the turn.
    detail = _detail(world)
    assert detail["unconverted_reasons"] == {"extractor_unavailable": 1}
    assert detail["status"] == "failed"
    assert {k: v[1] for k, v in _waits(world).items()} == {"pdf1": 3, "pdf2": 2}
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["status"] == "ok"
    assert world.states["decks"]["state_value"] == "2026-01-02T00:00:00.000Z"


@pytest.mark.parametrize("ending", ["rate_limited", "cut"])
def test_a_turn_that_ends_early_keeps_the_stored_waits(
    world: _World, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    world.extract_errors["pdf1"] = extractors.ExtractorUnavailable("timed out")
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    assert _waits(world)["pdf1"][1] == 1
    if ending == "rate_limited":
        world.fail_listing = 429
    else:
        # An older file, listed first, uses the whole bound.
        monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
        world.files.insert(0, _file("doc0", DOCS, "2025-12-01T00:00:00.000Z"))
    asyncio.run(source_sync.run_sync())
    assert _waits(world)["pdf1"][1] == 1


def test_an_address_another_writer_holds_reads_no_extractor(world: _World) -> None:
    uri = _uri("pdf1", "file")
    world.docs[uri] = {"actor": "human:ann", "tags": [], "metadata": {}}
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    assert world.capability_calls == 0
    assert _detail(world)["skipped"] == 1


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("mime", "answer", "reason"),
    [
        (DOCS, httpx.Response(503), "http_503"),
        (DOCS, _timeout, "unreachable"),
        (SLIDES, httpx.Response(500), "http_500"),
        (SLIDES, _timeout, "unreachable"),
    ],
)
def test_one_file_s_store_failure_that_may_pass_waits_for_that_file(
    world: _World, mime: str, answer: Any, reason: str
) -> None:
    world.files = [
        _file("bad1", mime, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    world.file_answers["bad1"] = answer
    asyncio.run(source_sync.run_sync())
    assert list(world.docs) == [_uri("doc2")]
    assert _detail(world)["status"] == "failed"
    assert world.states["decks"]["state_value"] == ""
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert (detail["status"], detail["unconverted_reasons"]) == ("ok", {reason: 1})
    assert world.states["decks"]["state_value"] == "2026-01-02T00:00:00.000Z"


def test_a_401_on_one_file_is_the_turn_s_credential(world: _World) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    world.file_answers["doc1"] = httpx.Response(401)
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    assert _detail(world)["status"] == "credential_invalid"


def test_the_stamp_never_passes_the_listing_s_start(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2999-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    stamp = world.states["decks"]["state_value"]
    assert gdrive.valid_stamp(stamp) and stamp < "2999"
    # So the file is listed again, and skipped by its revision.
    world.upserts.clear()
    asyncio.run(source_sync.run_sync())
    assert world.upserts == []


def test_a_lost_folder_writes_the_visible_ones_and_retires_nothing(world: _World) -> None:
    config = {"folder_ids": [FOLDER, OTHER], "include": ["docs"]}
    world.sources = [_source(config=config)]
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.lost_folders = {OTHER}
    world.files = [_file("doc2", DOCS, "2026-01-02T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync(full=True))
    assert sorted(world.docs) == [_uri("doc1"), _uri("doc2")]
    assert world.audits == []
    assert _detail(world)["status"] == "failed"


def test_a_maintainer_s_delete_request_goes_before_the_changed_files(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.docs[_uri("old1")] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:gone"],
        "metadata": {"ingested_from": "old1", "source_revision": "r"},
    }
    world.sources.append(_source("gone", active=False, pending_delete=True, credential_enc=""))
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    result = asyncio.run(source_sync.run_sync())
    assert result.retired == 1 and result.changed == 0
    assert list(world.docs) == []


def test_a_write_the_store_refuses_is_unconverted_at_once(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _refuse(inp: Any) -> Any:
        raise ValidationError("the original is too large", code="TOO_LARGE")

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _refuse)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert (detail["status"], detail["unconverted_reasons"]) == (
        "ok",
        {"write_refused:TOO_LARGE": 1},
    )


def test_a_full_sync_request_with_a_failing_file_completes_and_clears(world: _World) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("bad1", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    world.file_answers["bad1"] = httpx.Response(503)
    asyncio.run(source_sync.run_sync())
    world.states["decks"]["detail"] = json.dumps(
        {**_detail(world), "requested_at": "2026-11-01T00:00:00+00:00", "full": True}
    )
    for _ in range(3):
        asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    # The request reset the waits once; the file then waited its passes and gave up.
    assert "requested_at" not in detail and "full" not in detail
    assert (detail["status"], detail["unconverted_reasons"]) == ("ok", {"http_503": 1})


def test_a_periodic_full_listing_gives_a_given_up_file_one_more_try(world: _World) -> None:
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers["bad1"] = httpx.Response(503)
    for _ in range(3):
        asyncio.run(source_sync.run_sync())
    assert _waits(world)["bad1"][1] == 3
    # One more failure on the next full listing gives it up again at once.
    world.states["decks"]["detail"] = json.dumps(_listed_long_ago(world))
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["status"] == "ok" and _waits(world)["bad1"][1] == 3
    # Once the store answers, the next full listing writes it.
    del world.file_answers["bad1"]
    world.states["decks"]["detail"] = json.dumps(_listed_long_ago(world, days=3))
    asyncio.run(source_sync.run_sync())
    assert _uri("bad1") in world.docs and "bad1" not in _waits(world)


def test_an_incomplete_listing_retires_nothing(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.files = []
    world.incomplete_search = True
    world.files = [_file("doc2", DOCS, "2026-01-02T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync(full=True))
    # What it listed is written; nothing is retired and the stamp stays.
    assert sorted(world.docs) == [_uri("doc1"), _uri("doc2")]
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == ("ok", "listing: incomplete search")
    assert world.states["decks"]["state_value"] == "2026-01-01T00:00:00.000Z"


def test_a_file_missing_from_the_listing_but_still_in_the_folder_stays(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.files = []
    world.file_answers["doc1"] = httpx.Response(200, json={"trashed": False, "parents": [FOLDER]})
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == [_uri("doc1")]
    world.file_answers["doc1"] = httpx.Response(200, json={"trashed": True, "parents": [FOLDER]})
    asyncio.run(source_sync.run_sync(full=True))
    assert world.docs == {}


def test_a_document_left_at_a_file_s_old_address_is_retired(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    moved = "https://docs.example.com/document/d/doc1/view"
    # The new address is another writer's, so the file is not written there.
    world.docs[moved] = {"actor": "human:ann", "tags": [], "metadata": {}}
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", webViewLink=moved)]
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == [moved]


def test_a_401_on_the_folder_check_ends_the_turn(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers[FOLDER] = httpx.Response(401)
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    assert _detail(world)["status"] == "credential_invalid"


def test_any_other_conversion_failure_waits_for_that_file(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(deck: Any) -> str:
        raise KeyError("pageElements")

    monkeypatch.setattr(source_sync.convert, "presentation_markdown", _broken)
    world.files = [
        _file("deck1", SLIDES, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    asyncio.run(source_sync.run_sync())
    assert list(world.docs) == [_uri("doc2")]
    assert _waits(world)["deck1"][2] == "fetch_failed:KeyError"


def test_a_turn_that_raises_records_a_failed_state_and_keeps_the_waits(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers["bad1"] = httpx.Response(503)
    asyncio.run(source_sync.run_sync())

    async def _down(*a: Any, **kw: Any) -> Any:
        raise ConnectionError("store down")

    monkeypatch.setattr(source_sync.store, "source_revisions", _down)
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == ("failed", "ConnectionError")
    assert _waits(world)["bad1"][1] == 1


def _requests_for(world: _World, file_id: str) -> int:
    """Requests the store got about one file: its export, deck read, download or
    lookup (every path that names the file's id)."""
    return sum(1 for r in world.requests if f"/{file_id}" in r.url.path)


@pytest.mark.parametrize("held_by", ["waiting_file", "lost_folder", "cut"])
def test_a_full_turn_that_stays_incomplete_retries_a_given_up_file_once(
    world: _World, monkeypatch: pytest.MonkeyPatch, held_by: str
) -> None:
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers["bad1"] = httpx.Response(503)
    for _ in range(3):
        asyncio.run(source_sync.run_sync())
    assert _waits(world)["bad1"][1] == 3
    if held_by == "waiting_file":
        world.files.append(_file("bad2", DOCS, "2026-01-02T00:00:00.000Z"))
        world.file_answers["bad2"] = httpx.Response(503)
    elif held_by == "lost_folder":
        world.lost_folders = {FOLDER}
    else:
        monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
        world.files.append(_file("doc2", DOCS, "2026-01-02T00:00:00.000Z"))
        world.files.append(_file("doc3", DOCS, "2026-01-03T00:00:00.000Z"))
    world.states["decks"]["detail"] = json.dumps(_listed_long_ago(world))
    before = _requests_for(world, "bad1")
    asyncio.run(source_sync.run_sync())
    assert _requests_for(world, "bad1") == before + 1
    # The full listing did not complete, so the next full turn grants no new retry.
    asyncio.run(source_sync.run_sync())
    assert _requests_for(world, "bad1") == before + 1


@pytest.mark.parametrize(
    ("answer", "status"),
    [(httpx.Response(401), "credential_invalid"), (httpx.Response(429), "rate_limited")],
)
def test_a_lookup_refused_for_the_credential_or_rate_limited_stops_retiring(
    world: _World, answer: httpx.Response, status: str
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    asyncio.run(source_sync.run_sync())
    world.files = []
    world.file_answers["doc1"] = answer
    world.file_answers["doc2"] = answer
    asyncio.run(source_sync.run_sync(full=True))
    assert sorted(world.docs) == [_uri("doc1"), _uri("doc2")]
    assert _detail(world)["status"] == status
    lookups = [r for r in world.requests if r.url.params.get("fields") == "trashed,parents"]
    assert len(lookups) == 1


def _listed_long_ago(world: _World, days: int = 2) -> dict[str, Any]:
    """The state as days past its last complete full listing: the next turn is full.
    Each call in one test passes its own `days`, as each real full listing has its
    own time."""
    detail = _detail(world)
    stamp = datetime.fromisoformat(detail["full_listed_at"]) - timedelta(days=days)
    return {**detail, "full_listed_at": stamp.isoformat()}


def test_a_listed_file_with_no_usable_address_keeps_its_document(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", webViewLink="")]
    # The lookup finds the file still in the folder.
    world.file_answers["doc1"] = httpx.Response(200, json={"trashed": False, "parents": [FOLDER]})
    asyncio.run(source_sync.run_sync(full=True))
    assert list(world.docs) == [_uri("doc1")]
    assert world.audits == []
    assert _detail(world)["unconverted_reasons"] == {"no_address": 1}


@pytest.mark.parametrize("why", ["incomplete_search", "lost_folder"])
def test_waits_survive_a_full_listing_that_is_not_whole(world: _World, why: str) -> None:
    config = {"folder_ids": [FOLDER, OTHER], "include": ["docs"]}
    world.sources = [_source(config=config)]
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers["bad1"] = httpx.Response(503)
    asyncio.run(source_sync.run_sync())
    assert _waits(world)["bad1"][1] == 1
    # The file is not listed this time, but the listing cannot say it is gone.
    world.files = []
    if why == "incomplete_search":
        world.incomplete_search = True
    else:
        world.lost_folders = {OTHER}
    # A periodic full listing (an explicit full sync would reset the waits).
    world.states["decks"]["detail"] = json.dumps(
        {**_detail(world), "full_listed_at": "2026-01-01T00:00:00+00:00"}
    )
    asyncio.run(source_sync.run_sync())
    assert _waits(world)["bad1"][1] == 1


def test_a_folder_error_is_not_hidden_by_an_incomplete_listing(world: _World) -> None:
    config = {"folder_ids": [FOLDER, OTHER], "include": ["docs"]}
    world.sources = [_source(config=config)]
    world.lost_folders = {OTHER}
    world.incomplete_search = True
    asyncio.run(source_sync.run_sync(full=True))
    detail = _detail(world)
    assert detail["status"] == "failed" and OTHER in detail["error"]


def test_the_first_error_of_a_turn_is_the_one_it_reports(world: _World) -> None:
    config = {"folder_ids": [FOLDER, OTHER], "include": ["docs"]}
    world.sources = [_source(config=config)]
    world.lost_folders = {OTHER}
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.file_answers["bad1"] = httpx.Response(503)
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["status"] == "failed" and OTHER in detail["error"]


def test_more_refused_files_than_the_bound_do_not_hold_the_stamp(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    world.files = [_file(f"bad{i}", DOCS, f"2026-01-0{i}T00:00:00.000Z") for i in range(1, 6)]
    for i in range(1, 6):
        world.file_answers[f"bad{i}"] = httpx.Response(403)
    asyncio.run(source_sync.run_sync())
    # Cut by the bound: not a failure, but it says so.
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == (
        "ok",
        "pass bound reached; the next pass goes on",
    )
    for _ in range(2):
        asyncio.run(source_sync.run_sync())
    assert world.states["decks"]["state_value"] == "2026-01-05T00:00:00.000Z"
    # Each file was fetched once: a given-up file costs nothing on later passes.
    assert all(_requests_for(world, f"bad{i}") == 1 for i in range(1, 6))
    assert _detail(world)["unconverted_reasons"] == {"http_403": 5}


def test_a_file_over_the_listed_size_is_given_up_without_any_of_the_bound(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 100)
    world.files = [
        _file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="101"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    asyncio.run(source_sync.run_sync())
    assert list(world.docs) == [_uri("doc2")]
    assert _waits(world)["pdf1"][2] == "too_large"


def test_a_credential_refusal_after_a_waiting_file_is_the_turn_s_status(world: _World) -> None:
    world.files = [
        _file("bad1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    world.file_answers["bad1"] = httpx.Response(503)
    world.file_answers["doc2"] = httpx.Response(401)
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["status"] == "credential_invalid" and "http_401" in detail["error"]


def test_repeated_full_runs_get_past_more_refused_files_than_the_bound(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    world.files = [_file(f"bad{i}", DOCS, f"2026-01-0{i}T00:00:00.000Z") for i in range(1, 6)]
    for i in range(1, 6):
        world.file_answers[f"bad{i}"] = httpx.Response(403)
    first = asyncio.run(source_sync.run_sync(full=True))
    assert first.cut
    # Each run goes on with the request the first recorded, so the waits are reset once.
    for _ in range(2):
        asyncio.run(source_sync.run_sync(full=True))
    assert world.states["decks"]["state_value"] == "2026-01-05T00:00:00.000Z"
    assert "requested_at" not in _detail(world)
    assert all(_requests_for(world, f"bad{i}") == 1 for i in range(1, 6))


@pytest.mark.parametrize("refusal", ["http_403", "extraction_failed", "write_refused"])
def test_a_file_refused_at_once_is_tried_again_when_it_changes_and_on_a_full_listing(
    world: _World, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    if refusal == "http_403":
        world.files = [_file("f1", DOCS, "2026-01-01T00:00:00.000Z")]
        world.file_answers["f1"] = httpx.Response(403)
    elif refusal == "extraction_failed":
        world.files = [_file("f1", PDF, "2026-01-01T00:00:00.000Z", size="14")]
        world.extract_errors["f1"] = extractors.ExtractionFailed("corrupt", "damaged")
    else:

        async def _refuse(inp: Any) -> Any:
            raise ValidationError("refused", code="TOO_LARGE")

        monkeypatch.setattr(writes, "handle_knowledge_upsert", _refuse)
        world.files = [_file("f1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    tried = _requests_for(world, "f1")
    assert tried >= 1
    asyncio.run(source_sync.run_sync())
    # Given up on its revision (its entry exhausted), so the next turn does not fetch it.
    assert _waits(world)["f1"][1] == 3
    assert _requests_for(world, "f1") == tried
    # A periodic full listing tries it exactly once more...
    world.states["decks"]["detail"] = json.dumps(_listed_long_ago(world))
    asyncio.run(source_sync.run_sync())
    assert _requests_for(world, "f1") == tried * 2
    assert _waits(world)["f1"][1] == 3
    # ...a full listing that grants no new retry skips it without a fetch...
    detail = _detail(world)
    world.states["decks"]["detail"] = json.dumps(
        {**detail, "full_listed_at": detail["retry_granted_for"]}
    )
    asyncio.run(source_sync.run_sync())
    assert "modifiedTime >" not in _listings(world)[-1]
    assert _requests_for(world, "f1") == tried * 2
    # ...and a new revision is tried again.
    world.files[0] = {**world.files[0], "modifiedTime": "2026-02-01T00:00:00.000Z"}
    asyncio.run(source_sync.run_sync())
    assert _requests_for(world, "f1") == tried * 3


def test_a_turn_cut_by_the_bound_marks_the_pass_cut(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    result = asyncio.run(source_sync.run_sync())
    assert result.cut and result.changed == 1
    from server.knowledge.workers.source_sync import outcome

    assert outcome(result)["cut"] is True


def test_a_size_that_is_not_a_number_is_left_to_the_download_bound(world: _World) -> None:
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="big")]
    asyncio.run(source_sync.run_sync())
    assert _uri("pdf1", "file") in world.docs


def test_a_delete_request_cut_by_the_bound_marks_the_pass_cut(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("gone", active=False, pending_delete=True, credential_enc="")]
    for n in ("old1", "old2"):
        world.docs[_uri(n)] = {
            "actor": SYNC_WORKER_ACTOR,
            "tags": ["source:gone"],
            "metadata": {"ingested_from": n, "source_revision": "r"},
        }
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    result = asyncio.run(source_sync.run_sync())
    assert (result.retired, result.cut) == (1, True)


def test_a_delete_request_past_the_bound_waits_and_marks_the_pass_cut(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is spent before the second removed source is reached.
    world.sources = [
        _source(n, active=False, pending_delete=True, credential_enc="") for n in ("aaa", "bbb")
    ]
    for n in ("aaa", "bbb"):
        world.docs[_uri(n)] = {
            "actor": SYNC_WORKER_ACTOR,
            "tags": [f"source:{n}"],
            "metadata": {"ingested_from": n, "source_revision": "r"},
        }
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    result = asyncio.run(source_sync.run_sync())
    assert (result.retired, result.cut) == (1, True)
    assert list(world.docs) == [_uri("bbb")]


def test_a_full_run_turns_a_waiting_plain_request_into_a_full_one(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    asked = "2026-11-01T00:00:00+00:00"
    world.states["decks"]["detail"] = json.dumps({**_detail(world), "requested_at": asked})
    asyncio.run(source_sync.source_ops.request_full_sync(world.sources[0]))
    detail = _detail(world)
    assert detail["full"] is True and detail["requested_at"] != asked
    # The stamp is kept.
    assert world.states["decks"]["state_value"] == "2026-01-01T00:00:00.000Z"


# The extraction leg.

DOC1 = "https://docs.example.com/document/d/doc1/edit"
DOC2 = "https://docs.example.com/document/d/doc2/edit"
REV1 = "2026-01-01T00:00:00.000Z"
REV2 = "2026-01-02T00:00:00.000Z"


def _two_docs(world: _World) -> None:
    world.files = [_file("doc1", DOCS, REV1), _file("doc2", DOCS, REV2)]


def test_a_written_document_is_mined_in_the_same_pass_as_the_worker(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import cost_accumulator

    monkeypatch.setattr(settings, "knowledge_source_sync_cap_share", 0.4)
    world.files = [_file("doc1", DOCS, REV1)]
    outer = cost_accumulator.begin_op()
    result = asyncio.run(source_sync.run_sync())
    assert [(uri, actor) for uri, actor, _ in world.mined] == [(DOC1, SYNC_WORKER_ACTOR)]
    attribution = world.mined[0][2]
    assert (attribution.org, attribution.namespace) == (ORG, NS)
    assert world.cap_reads == [
        {"operation": "worker.knowledge_source_sync", "share": 0.4, "fail_open": False}
    ]
    # The document's spend bills its namespace, in a row of its own.
    assert [
        (r["org"], r["namespace"], r["operation"], r["llm_input_tokens"]) for r in world.cost_rows
    ] == [(ORG, NS, "worker.knowledge_source_sync", 10)]
    # The pass's own accumulator stays bound, and holds none of it.
    assert cost_accumulator.current() is outer and outer.drain().llm_input_tokens == 0
    assert (result.extracted, result.failed, result.extract_deferred) == (1, 0, 0)
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"], detail["pending_extract"]) == (
        {DOC1: REV1},
        {},
        0,
    )
    assert (detail["extracted"], detail["extract_error"]) == (1, "")
    assert detail["last_extracted_at"]
    # Unchanged on the next pass: nothing is mined again. Changed, it is.
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1
    world.files = [_file("doc1", DOCS, "2026-03-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 2


@pytest.mark.parametrize("stop", ["raises", "stopped"])
def test_a_document_written_by_a_turn_that_did_not_finish_is_mined_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    from server.workers import pass_context

    write_state = source_sync._write_state
    calls: list[int] = []

    async def _first_fails(*a: Any, **kw: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise (
                RuntimeError("state store down") if stop == "raises" else pass_context.PassStopped()
            )
        await write_state(*a, **kw)

    # The turn writes the file, then fails or is stopped before its state is written:
    # nothing records the write but the document itself.
    monkeypatch.setattr(source_sync, "_write_state", _first_fails)
    world.files = [_file("doc1", DOCS, REV1)]
    with contextlib.suppress(pass_context.PassStopped):
        asyncio.run(source_sync.run_sync())
    assert DOC1 in world.docs
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][-1:] == [DOC1]
    assert _detail(world)["mined"] == {DOC1: REV1}


def test_pinned_rows_a_run_tags_are_counted_and_kept_through_a_held_turn(
    world: _World,
) -> None:
    from server.core.errors import ModelCallRefused
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, flagged=["decision:a", "decision:b"])
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.flagged_pinned == 2 and _detail(world)["flagged_pinned"] == 2
    world.mine_answer = lambda uri: ExtractReport(
        uri, False, held=ModelCallRefused("over budget", hold_s=600.0)
    )
    world.files.append(_file("doc2", DOCS, REV2))
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    # The held turn tried nothing to completion: the last mining turn's counts stand.
    assert (detail["flagged_pinned"], detail["extracted"]) == (2, 1)
    assert (detail["pending_extract"], detail["extract_error"]) == (1, "held:MODEL_CALL_REFUSED")


@pytest.mark.parametrize("why", ["over_cap", "switched_off", "switch_unread"])
def test_a_capped_or_switched_off_namespace_keeps_its_documents_pending(
    world: _World, why: str
) -> None:
    from server.llm import customer

    if why == "over_cap":
        world.over_cap = {(ORG, NS)}
    elif why == "switched_off":
        world.switched_off = frozenset({ORG})
    else:
        world.switched_off = customer.SwitchUnread("the switch is too old")
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert world.mined == [] and result.extract_deferred == 1 and result.failed == 0
    assert _detail(world)["pending_extract"] == 1
    # Once it may spend again, the next pass mines it with no change at the source.
    world.over_cap, world.switched_off = set(), frozenset()
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert _detail(world)["pending_extract"] == 0


def test_the_cap_is_read_again_before_each_document(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    def _spend(uri: str) -> ExtractReport:
        world.over_cap = {(ORG, NS)}  # this document's spend reached the share
        return ExtractReport(uri, False)

    world.mine_answer = _spend
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.extract_deferred == 1 and _detail(world)["pending_extract"] == 1


def test_a_held_namespace_waits_out_its_hold_with_no_try_counted(world: _World) -> None:
    from server.core.errors import ModelCallRefused
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(
        uri, False, held=ModelCallRefused("over budget", hold_s=600.0)
    )
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    # The hold stops the namespace at its first document.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert (result.failed, result.extract_deferred) == (0, 1)
    assert result.errors == [f"{ORG}/{NS}/decks: extraction held:MODEL_CALL_REFUSED"]
    detail = _detail(world)
    assert (detail["pending_extract"], detail["extract_waits"], detail["mined"]) == (2, {}, {})
    # While it is held, a later pass makes no call for it.
    result = asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1 and result.extract_deferred == 1


def test_a_hold_on_every_org_stops_the_leg(world: _World) -> None:
    from server.llm import customer
    from server.memory.app.decision_extract import ExtractReport

    world.sources.append(_source("notes", source_id="id-notes"))
    world.docs[DOC2] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:notes"],
        "metadata": {"ingested_from": "doc2", "source_revision": REV2},
    }
    world.mine_answer = lambda uri: ExtractReport(uri, False, held=customer.SwitchUnread("old"))
    # Each source lists both files and writes only its own.
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    # The first source's hold ends the leg: the second's document is not tried.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.extract_deferred == 2
    assert _detail(world, "notes")["pending_extract"] == 1


def test_a_failing_extraction_is_given_up_after_its_tries_until_rewritten(
    world: _World,
) -> None:
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, error="no chunks")
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 1 and result.errors == [f"{ORG}/{NS}/decks: extraction not_run"]
    detail = _detail(world)
    assert detail["extract_waits"] == {DOC1: [REV1, 1, "not_run"]}
    assert (detail["extract_error"], detail["pending_extract"]) == ("not_run", 1)
    asyncio.run(source_sync.run_sync())
    result = asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES and result.unextracted == 1
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"], detail["pending_extract"]) == (
        {DOC1: REV1},
        {},
        0,
    )
    # Given up on that revision: a later pass does not try it again.
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES
    # Written again, it is tried afresh, and mined.
    world.mine_answer = None
    world.files = [_file("doc1", DOCS, "2026-02-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES + 1
    assert (_detail(world)["pending_extract"], _detail(world)["extracted"]) == (0, 1)


@pytest.mark.parametrize("answer", ["refused", "raises", "out_of_time"])
def test_each_failed_document_counts_one_try(
    world: _World, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    from server.memory.app.decision_extract import Dropped, ExtractReport

    if answer == "refused":
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, dropped=[Dropped("s", "decision:x: refused: BUSY", refused=True)]
        )
        reason = "refused"
    elif answer == "raises":
        world.mine_answer = RuntimeError("store down")
        reason = "failed:RuntimeError"
    else:
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, error="time ran out", out_of_time=True
        )
        reason = "out_of_time"
        # Started with less than a whole pass's read time: it may fit in a later pass.
        monkeypatch.setattr(source_sync, "_full_read_s", lambda: 10.0**6)
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 2
    assert _detail(world)["extract_waits"] == {DOC1: [REV1, 1, reason], DOC2: [REV2, 1, reason]}


def test_a_document_no_longer_the_source_s_is_not_mined_and_leaves_the_map(
    world: _World,
) -> None:
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    assert set(_detail(world)["mined"]) == {DOC1, DOC2}
    world.docs[DOC1]["actor"] = "agent:someone"
    world.files = [_file("doc2", DOCS, "2026-03-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][2:] == [DOC2]
    assert set(_detail(world)["mined"]) == {DOC2}


def test_mining_shares_the_pass_bound_and_goes_on_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    # The one fetch spent the bound: the document waits for the next pass.
    assert world.mined == [] and result.cut and _detail(world)["pending_extract"] == 1
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]


def test_a_document_is_not_started_with_too_little_time_left(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 180)
    # More than the 150 s of read time the smallest pass leaves: no try is spent.
    monkeypatch.setattr(source_sync, "_MIN_MINE_S", 151.0)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert world.mined == [] and result.cut
    assert (_detail(world)["pending_extract"], _detail(world).get("extract_waits")) == (1, None)


def test_a_sync_listing_shows_the_extraction_counts() -> None:
    from server.knowledge.domain.sources import sync_of

    detail = json.dumps(
        {
            "pending_extract": 2,
            "mined": {DOC1: REV1},
            "extracted": 3,
            "flagged_pinned": 1,
            "extract_error": "",
        }
    )
    assert sync_of(detail) == {
        "pending_extract": 2,
        "extracted": 3,
        "flagged_pinned": 1,
        "extract_error": "",
    }


def test_a_document_out_of_time_with_a_whole_pass_is_given_up_at_once(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, error="late", out_of_time=True)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.unextracted == 1
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"]) == ({DOC1: REV1}, {})
    assert (detail["unextracted"], detail["extract_error"]) == (1, "too_large_for_pass")
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1


def test_a_document_that_ran_out_of_time_goes_first_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.decision_extract import ExtractReport

    monkeypatch.setattr(source_sync, "_full_read_s", lambda: 10.0**6)
    world.mine_answer = lambda uri: ExtractReport(uri, False, error="late", out_of_time=uri == DOC2)
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # doc1 failed for another reason, doc2 ran out of time: doc2 goes first.
    world.mined.clear()
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC2, DOC1]


def test_a_permanent_refusal_settles_the_document(world: _World) -> None:
    from server.memory.app.decision_extract import Dropped, ExtractReport

    world.mine_answer = lambda uri: ExtractReport(
        uri,
        False,
        dropped=[Dropped("s", "decision:x: refused: BLACKLISTED", refused=True, permanent=True)],
    )
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert (result.extracted, result.failed) == (1, 0)
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"]) == ({DOC1: REV1}, {})
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1


def test_a_run_stopped_mid_write_records_nothing_and_is_mined_next_pass(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    def _lost(uri: str) -> ExtractReport:
        world.lease.held = False  # the lease went while the run wrote
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _lost
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # One try, then the source's turn stops: no state write under a lost lease, nothing settled.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert "mined" not in _detail(world)
    world.lease.held, world.mine_answer = True, None
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][1:] == [DOC1, DOC2]


@pytest.mark.parametrize(
    "why", ["no_model", "switched_off", "over_cap", "switch_unread", "held:MODEL_CALL_REFUSED"]
)
def test_a_deferred_source_shows_why_it_waits(
    world: _World, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    from server.core.errors import ModelCallRefused
    from server.llm import customer
    from server.memory.app import decision_extract
    from server.memory.app.decision_extract import ExtractReport

    if why == "no_model":
        monkeypatch.setattr(decision_extract, "no_model_error", lambda: "no key")
    elif why == "switched_off":
        world.switched_off = frozenset({ORG})
    elif why == "over_cap":
        world.over_cap = {(ORG, NS)}
    elif why == "switch_unread":
        world.switched_off = customer.SwitchUnread("old")
    else:
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, held=ModelCallRefused("budget", hold_s=60.0)
        )
    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())
    assert (_detail(world)["extract_error"], _detail(world)["pending_extract"]) == (why, 1)


def test_the_pass_bound_inside_a_turn_is_why_the_rest_wait(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [True, True, False]  # the leg's check, the first document's, the second's
    monkeypatch.setattr(source_sync, "_time_for_one", lambda run: answers.pop(0))
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.cut and result.extract_deferred == 1
    detail = _detail(world)
    assert (detail["pending_extract"], detail["extract_error"]) == (1, "pass_bound")


def test_the_reason_clears_once_nothing_waits(world: _World) -> None:
    world.over_cap = {(ORG, NS)}
    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["extract_error"] == "over_cap"
    world.docs[DOC1]["actor"] = "agent:someone"  # no longer the source's: nothing waits
    asyncio.run(source_sync.run_sync())
    assert (_detail(world)["extract_error"], _detail(world)["pending_extract"]) == ("", 0)


def _second_source(world: _World) -> None:
    world.sources.append(_source("notes", source_id="id-notes"))
    world.docs[DOC2] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:notes"],
        "metadata": {"ingested_from": "doc2", "source_revision": REV2},
    }


def test_a_lease_lost_mid_write_ends_that_source_and_the_leg_goes_on(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    _second_source(world)

    def _lost(uri: str) -> ExtractReport | None:
        if uri != DOC1:
            return None
        world.lease.held = False
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _lost
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # The first source records nothing; the second, under its own lease, is mined.
    assert [uri for uri, _, _ in world.mined] == [DOC1, DOC2]
    assert "mined" not in _detail(world) and _detail(world, "notes")["mined"] == {DOC2: REV2}


def test_a_stopped_pass_ends_the_leg_with_nothing_recorded(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.decision_extract import ExtractReport
    from server.workers import pass_context

    _second_source(world)
    stopped: list[bool] = []
    monkeypatch.setattr(pass_context, "stop_requested", lambda: bool(stopped))

    def _stop(uri: str) -> ExtractReport:
        stopped.append(True)
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _stop
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert "mined" not in _detail(world) and "mined" not in _detail(world, "notes")


@pytest.mark.parametrize("how", ["lease_lost", "check_stop", "cancelled"])
def test_a_turn_ended_from_outside_writes_no_state_and_is_mined_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    from server.memory.app import decision_extract
    from server.workers import pass_context

    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())  # synced, then mined
    mined_before = _detail(world)["mined"]
    world.files = [_file("doc1", DOCS, "2026-03-01T00:00:00.000Z")]
    started = asyncio.Event()
    checks = (pass_context.check_stop, source_sync._extract_all)
    mined = decision_extract.extract_document

    async def _ends(*a: Any, **kw: Any) -> Any:
        started.set()
        if how == "lease_lost":
            raise asyncio.CancelledError  # as the lease's loss cancels the turn's body
        await asyncio.sleep(5)

    if how == "lease_lost":

        @contextlib.asynccontextmanager
        async def _losing(name: str, **kw: Any) -> AsyncIterator[Any]:
            try:
                yield world.lease
            except asyncio.CancelledError:
                raise leases.LeaseLost(name) from None

        monkeypatch.setattr(leases, "cluster_lease", _losing)
    if how == "check_stop":
        # The supervisor stops the pass once the leg has started: the leg's own check
        # before the document raises.
        leg: list[bool] = []
        real_check, real_all = checks

        def _check() -> None:
            if leg:
                raise pass_context.PassStopped
            real_check()

        async def _all(*a: Any, **kw: Any) -> None:
            leg.append(True)
            await real_all(*a, **kw)

        monkeypatch.setattr(pass_context, "check_stop", _check)
        monkeypatch.setattr(source_sync, "_extract_all", _all)
    else:
        monkeypatch.setattr(decision_extract, "extract_document", _ends)

    async def _run() -> None:
        task = asyncio.create_task(source_sync.run_sync())
        if how == "cancelled":
            await started.wait()
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, pass_context.PassStopped):
            await task

    asyncio.run(_run())
    assert _detail(world)["mined"] == mined_before
    # The next pass, with nothing ending it.
    monkeypatch.setattr(pass_context, "check_stop", checks[0])
    monkeypatch.setattr(source_sync, "_extract_all", checks[1])
    monkeypatch.setattr(decision_extract, "extract_document", mined)
    monkeypatch.setattr(leases, "cluster_lease", _plain_lease(world))
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["mined"] == {DOC1: "2026-03-01T00:00:00.000Z"}


def _plain_lease(world: _World) -> Any:
    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        yield world.lease

    return _cluster


def test_a_write_unit_a_cancel_leaves_running_is_billed(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import decision_extract
    from server.observability import cost_accumulator

    started = asyncio.Event()

    async def _unit() -> None:
        started.set()
        await asyncio.sleep(0.05)
        cost_accumulator.report_llm("m", 7, 1)  # the write's embedding, say

    async def _extract(*a: Any, **kw: Any) -> Any:
        # As extract_document holds its units in the run's own set.
        await decision_extract._whole(kw["writes"], _unit())

    monkeypatch.setattr(decision_extract, "extract_document", _extract)
    world.files = [_file("doc1", DOCS, REV1)]

    async def _run() -> None:
        task = asyncio.create_task(source_sync.run_sync())
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert [(r["org"], r["llm_input_tokens"]) for r in world.cost_rows] == [(ORG, 7)]


def test_a_failed_state_write_of_a_waiting_source_does_not_stop_the_leg(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import OverloadedError

    _second_source(world)
    # The first source has nothing left to mine but a stale count to clear.
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"pending_extract": 3})}
    write_mined = source_sync._write_mined

    async def _busy_once(src: Any, mined: Any, *a: Any, **kw: Any) -> None:
        if mined is None and src.name == "decks":
            raise OverloadedError("the sources lease is busy")
        await write_mined(src, mined, *a, **kw)

    monkeypatch.setattr(source_sync, "_write_mined", _busy_once)
    world.files = [_file("doc2", DOCS, REV2)]  # notes' file: decks lists it and skips it
    result = asyncio.run(source_sync.run_sync())
    assert result.errors == [f"{ORG}/{NS}/decks: extraction OverloadedError"]
    assert result.failed == 1
    assert [uri for uri, _, _ in world.mined] == [DOC2]
    assert _detail(world, "notes")["mined"] == {DOC2: REV2}


def test_a_failed_read_of_a_source_s_documents_is_that_source_s_error(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.workers import pass_context

    _second_source(world)
    world.files = [_file("doc2", DOCS, REV2)]
    revisions = source_sync.store.source_revisions
    noted: list[str] = []

    async def _decks_down(org: str, ns: str, name: str, **kw: Any) -> Any:
        if name == "decks" and kw.get("searchable_only", True) and noted == ["sync-done"]:
            raise ConnectionError("store down")
        return await revisions(org, ns, name, **kw)

    write_state = source_sync._write_state

    async def _then(*a: Any, **kw: Any) -> None:
        await write_state(*a, **kw)
        if a[0].name == "notes":
            noted.append("sync-done")  # both turns done: the leg's read of decks fails

    monkeypatch.setattr(source_sync.store, "source_revisions", _decks_down)
    monkeypatch.setattr(source_sync, "_write_state", _then)
    failures: list[str] = []
    monkeypatch.setattr(pass_context, "note_failure", lambda e, what="": failures.append(what))
    result = asyncio.run(source_sync.run_sync())
    assert result.errors == [f"{ORG}/{NS}/decks: extraction ConnectionError"]
    assert result.failed == 1 and failures == [f"{ORG}/{NS}/decks"]
    # The leg goes on: the other source's document is mined.
    assert [uri for uri, _, _ in world.mined] == [DOC2]
