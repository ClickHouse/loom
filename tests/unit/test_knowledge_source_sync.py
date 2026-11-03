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
from typing import Any

import httpx
import pytest

from server.clients import gdrive
from server.core import leases
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
DOCS = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
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
        self.routes: set[str] = {"pdf", "pptx"}
        self.declared: set[str] = {"pdf", "pptx"}
        self.extractor_down = False
        self.extract_error: Exception | None = None
        # Folders the account no longer sees; per-file answers that override the store's.
        self.lost_folders: set[str] = set()
        self.file_answers: dict[str, httpx.Response] = {}

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
        self, org: str, ns: str, name: str, *, searchable_only: bool = True
    ) -> list[dict[str, str]]:
        return [
            {
                "source_uri": uri,
                "file_id": d["metadata"]["ingested_from"],
                "source_revision": d["metadata"]["source_revision"],
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
            return self.file_answers[file_id]
        if path == f"/drive/v3/files/{FOLDER}":
            if FOLDER in self.lost_folders:
                return httpx.Response(404)
            return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.folder"})
        if path == "/drive/v3/files":
            if self.fail_listing:
                return httpx.Response(self.fail_listing, json={"error": {"status": "x"}})
            q = request.url.params["q"]
            after = q.split("modifiedTime > '")[1].rstrip("'") if "modifiedTime >" in q else ""
            files = [f for f in self.files if f["modifiedTime"] > after]
            return httpx.Response(200, json={"files": files})
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
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[None]:
        yield None

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
                content="",
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
        if w.extractor_down:
            raise extractors.ExtractorUnavailable("the extractor is unreachable")
        return extractors.Capabilities(name="x", formats=frozenset(w.declared), max_bytes=10**9)

    async def _extract(data: bytes, **kw: Any) -> Artifact:
        if w.extract_error is not None:
            raise w.extract_error
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
    w.sources.append(_source())
    return w


def _source(name: str = "decks", **kw: Any) -> KnowledgeSource:
    return KnowledgeSource(
        org=ORG,
        namespace=kw.pop("namespace", NS),
        name=name,
        kind="gdrive",
        source_id=kw.pop("source_id", f"id-{name}"),
        config={"folder_ids": [FOLDER], "include": ["docs", "slides", "pdf", "pptx"]},
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
    files = asyncio.run(gdrive.list_all("tok", [FOLDER], [DOCS], pace_s=0))
    assert [f["id"] for f in files] == ["a", "b"]
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
    # The turn that raised still records itself as failed, by error class only.
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == ("failed", "RuntimeError")
    monkeypatch.setattr(writes, "handle_knowledge_upsert", real)
    world.upserts.clear()
    asyncio.run(source_sync.run_sync())
    assert [inp.source_uri for inp, _ in world.upserts] == [_uri("doc1")]
    assert world.docs[_uri("doc1")].get("chunks", True)


def test_an_extractor_that_cannot_be_read_holds_the_turn_then_gives_up(world: _World) -> None:
    world.extractor_down = True
    world.files = [_file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["status"] == "failed" and "extractor_unavailable" in detail["error"]
    assert world.states["decks"]["state_value"] == ""
    asyncio.run(source_sync.run_sync())
    assert world.states["decks"]["state_value"] == ""
    # The third pass on the same revision counts it unconverted, and the stamp moves.
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert detail["unconverted_reasons"] == {"extractor_unavailable": 1}
    assert detail["status"] == "ok"
    assert world.states["decks"]["state_value"] == "2026-01-03T00:00:00.000Z"
    assert not [r for r in world.requests if r.url.params.get("alt") == "media"]


def test_a_file_the_extractor_refuses_is_unconverted(world: _World) -> None:
    world.extract_error = extractors.ExtractionFailed("corrupt", "the file is damaged")
    world.files = [_file("pdf1", PDF, "2026-01-03T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    assert world.docs == {}
    detail = _detail(world)
    assert detail["unconverted_reasons"] == {"extraction_failed:corrupt": 1}
    assert world.states["decks"]["state_value"] == "2026-01-03T00:00:00.000Z"


def test_a_pptx_file_goes_to_the_extractor_with_its_bytes_kept(world: _World) -> None:
    world.files = [_file("deck2", PPTX, "2026-01-03T00:00:00.000Z", size="14")]
    asyncio.run(source_sync.run_sync())
    [(inp, _)] = world.upserts
    assert inp.metadata["ingested_format"] == "pptx"
    assert inp.metadata["extraction_coverage"]["complete"] is True
    assert inp.blob_content_type == PPTX and inp.original_bytes_b64


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


def test_a_source_another_copy_holds_is_skipped(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb")]
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]

    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[None]:
        if name.endswith("/aaa"):
            raise leases.LeaseBusy(name)
        yield None

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

    async def _turn(src: KnowledgeSource, state: Any, run: Any, full: bool) -> Any:
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
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 0)
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
