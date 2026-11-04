"""A page store source (`kind: notion`): its client and probe over a mock transport,
the block to Markdown conversion, and its turn of the sync pass over an in-memory
registry and document store: what a first, an incremental and a full turn list,
fetch, write and retire, and how each failure is told apart. Every response is
written here; none is a recording of real content."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from server.clients import notion
from server.core import leases
from server.core.hashing import sha256_text
from server.core.secrets_at_rest import seal
from server.knowledge.app import source_probes, source_sync, source_sync_notion, writes
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import convert
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, KnowledgeSource, parse_config
from server.knowledge.store import documents
from server.memory.domain.provenance import pipeline_actor_var
from server.observability import audit
from server.settings import settings

ORG, NS = "acme", "code-acme-app"
TOKEN = "ntn_integration_token_for_tests"
PAGE = "11111111-1111-4111-8111-111111111111"
DB = "22222222-2222-4222-8222-222222222222"
DS = "33333333-3333-4333-8333-333333333333"
ROW1 = "44444444-4444-4444-8444-444444444444"
ROW2 = "55555555-5555-4555-8555-555555555555"
CHILD = "99999999-9999-4999-8999-999999999999"
GRANDCHILD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notion, "PAGE_PACE_S", 0.0)


def _rt(text: str, **marks: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text, "annotations": marks, "href": None}]


def _para(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": _rt(text)}, **kw}


def _page(page_id: str, edited: str, *, title: str = "", parent: str = "", **kw: Any) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://pages.example.com/{(title or page_id).replace(' ', '-')}-{page_id[:8]}",
        "last_edited_time": edited,
        "in_trash": False,
        "parent": {"type": "data_source_id", "data_source_id": DS, "database_id": parent}
        if parent
        else kw.pop("parent_of", {"type": "workspace", "workspace": True}),
        "properties": {"Name": {"type": "title", "title": _rt(title or f"{page_id[:4]} title")}},
        **kw,
    }


class _Store:
    """The registry, sync state and documents, and what the page store answers."""

    def __init__(self) -> None:
        self.sources: list[KnowledgeSource] = []
        self.states: dict[str, dict[str, str]] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.audits: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.pages: dict[str, dict[str, Any]] = {}
        self.database: dict[str, Any] = {"object": "database", "data_sources": [{"id": DS}]}
        self.rows: list[str] = []  # page ids of the data source
        self.blocks: dict[str, list[dict[str, Any]]] = {}
        # A response, or a callable that answers, by request path.
        self.answers: dict[str, Any] = {}
        self.incomplete = False
        self.upserts = 0
        # The clock a turn reads; later than every revision here unless a test moves it.
        self.now = datetime(2026, 3, 1, tzinfo=UTC)

    async def all_active_sources(self) -> list[KnowledgeSource]:
        return [s for s in self.sources if s.active]

    async def pending_delete_sources(self) -> list[KnowledgeSource]:
        return []

    async def inactive_namespaces(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        return set()

    async def read_states(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        return {n: self.states[n] for n in names if n in self.states}

    async def write_state(self, org: str, ns: str, name: str, *, state_value: str, detail: Any):
        self.states[name] = {"state_value": state_value, "detail": json.dumps(detail)}

    async def get_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return next((s for s in self.sources if s.name == name and s.active), None)

    async def source_revisions(
        self, org: str, ns: str, name: str, *, searchable_only: bool = True
    ) -> list[dict[str, str]]:
        return [
            {
                "source_uri": uri,
                "file_id": d["metadata"]["ingested_from"],
                "source_revision": d["metadata"]["source_revision"],
                "read_at": d["metadata"].get("read_at", ""),
                "text_sha256": d["metadata"].get("text_sha256", ""),
            }
            for uri, d in self.docs.items()
            if d["actor"] == SYNC_WORKER_ACTOR and f"source:{name}" in d["tags"]
        ]

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if path in self.answers:
            answer = self.answers[path]
            return answer(request) if callable(answer) else answer
        if path.startswith("/pages/"):
            page = self.pages.get(path.split("/")[2])
            return httpx.Response(200, json=page) if page else httpx.Response(404)
        if path == f"/databases/{DB}":
            return httpx.Response(200, json=self.database)
        if path == f"/data_sources/{DS}/query":
            body = json.loads(request.content)
            after = (body.get("filter") or {}).get("last_edited_time", {}).get("after", "")
            found = [self.pages[r] for r in self.rows if self.pages[r]["last_edited_time"] > after]
            status = {"type": "incomplete" if self.incomplete else "complete"}
            return httpx.Response(
                200, json={"results": found, "has_more": False, "request_status": status}
            )
        if path.startswith("/blocks/"):
            block_id = path.split("/")[2]
            blocks = self.blocks.get(block_id, [_para(f"Text of {block_id[:4]}.")])
            return httpx.Response(200, json={"results": blocks, "has_more": False})
        return httpx.Response(404)


def _source(**kw: Any) -> KnowledgeSource:
    return KnowledgeSource(
        org=ORG,
        namespace=NS,
        name="wiki",
        kind="notion",
        source_id="id-wiki",
        config=kw.pop("config", {"page_ids": [PAGE], "database_ids": [DB]}),
        credential_enc=seal("knowledge_sources.credential", TOKEN),
        **kw,
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _Store:
    w = _Store()
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 40)
    for name in (
        "all_active_sources",
        "pending_delete_sources",
        "inactive_namespaces",
        "read_states",
        "write_state",
        "get_source",
        "source_revisions",
    ):
        monkeypatch.setattr(source_sync.store, name, getattr(w, name))

    async def _trunk(org: str, ns: str) -> bool:
        return True

    monkeypatch.setattr(source_ops, "is_trunk_namespace", _trunk)
    monkeypatch.setattr(source_sync, "_repo_tag", lambda src: "repo:abc")

    @contextlib.asynccontextmanager
    async def _held(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield True

    monkeypatch.setattr(leases, "holding", _held)
    monkeypatch.setattr(leases, "cluster_lease", _held)
    monkeypatch.setattr(source_sync_notion, "_now", lambda: w.now)
    client = httpx.AsyncClient(transport=httpx.MockTransport(w.answer))
    monkeypatch.setattr(notion, "get_client", lambda: client)

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        d = w.docs.get(uri)
        if d is None:
            return None
        return documents.DocumentRow(
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

    monkeypatch.setattr(documents, "get_one", _get_one)

    async def _upsert(inp: Any) -> Any:
        w.upserts += 1
        w.docs[inp.source_uri] = {
            "actor": pipeline_actor_var.get(),
            "tags": list(inp.tags),
            "metadata": dict(inp.metadata),
            "input": inp,
        }

    async def _delete(inp: Any) -> Any:
        gone = w.docs.pop(inp.source_uri, None)
        return writes.KnowledgeDeleteOutput(deleted=gone is not None, chunks_tombstoned=1)

    async def _emit(event: str, **kw: Any) -> str:
        w.audits.append({"event": event, **kw})
        return "h"

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _upsert)
    monkeypatch.setattr(writes, "handle_knowledge_delete", _delete)
    monkeypatch.setattr(audit, "emit", _emit)
    w.sources.append(_source())
    w.pages[PAGE] = _page(PAGE, "2026-01-01T00:00:00.000Z", title="Design notes")
    for row, edited in ((ROW1, "2026-01-02T00:00:00.000Z"), (ROW2, "2026-01-03T00:00:00.000Z")):
        w.pages[row] = _page(row, edited, parent=DB)
        w.rows.append(row)
    return w


def _run(**kw: Any) -> source_sync.PassResult:
    return asyncio.run(source_sync.run_sync(**kw))


def _detail(world: _Store) -> dict[str, Any]:
    return json.loads(world.states["wiki"]["detail"])


def _paths(world: _Store) -> list[str]:
    return [r.url.path.removeprefix("/v1") for r in world.requests]


def _uri(world: _Store, page_id: str) -> str:
    return str(world.pages[page_id]["url"])


def test_a_first_turn_writes_every_listed_page_and_database_page(world: _Store) -> None:
    out = _run()
    assert (out.synced, out.changed, out.failed) == (1, 3, 0)
    doc = world.docs[_uri(world, PAGE)]
    inp = doc["input"]
    assert doc["actor"] == SYNC_WORKER_ACTOR and inp.source_class == "imported"
    assert inp.tags == ["source-kind:design_doc", "repo:abc", "source:wiki"]
    assert inp.title == "Design notes" and inp.content == "Text of 1111."
    assert doc["metadata"] == {
        "source_kind": "notion",
        "source_name": "wiki",
        "source_revision": "2026-01-01T00:00:00.000Z",
        "ingested_from": PAGE,
        "ingested_format": "blocks",
        "read_at": "2026-03-01T00:00:00.000Z",
        # Exactly what was written: title, Markdown and the blocks left out.
        "text_sha256": sha256_text("Design notes\nText of 1111.\n{}"),
    }
    # The token goes in the header only, with the API version the shapes follow.
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in world.requests)
    assert all(r.headers["Notion-Version"] == notion.API_VERSION for r in world.requests)
    assert all(TOKEN not in str(r.url) for r in world.requests)
    # The first turn lists the whole database.
    query = next(r for r in world.requests if r.url.path.endswith("/query"))
    assert "filter" not in json.loads(query.content)
    assert world.states["wiki"]["state_value"] == "2026-01-03T00:00:00.000Z"
    assert _detail(world)["status"] == "ok" and _detail(world)["documents_seen"] == 3


def test_a_later_turn_asks_for_edits_after_the_stamp_and_skips_unchanged_pages(
    world: _Store,
) -> None:
    _run()
    world.requests.clear()
    world.pages[ROW1] = _page(ROW1, "2026-01-05T00:00:00.000Z", parent=DB)
    world.blocks[ROW1] = [_para("Edited.")]
    out = _run()
    assert out.changed == 1 and world.docs[_uri(world, ROW1)]["input"].content == "Edited."
    query = next(r for r in world.requests if r.url.path.endswith("/query"))
    after = json.loads(query.content)["filter"]["last_edited_time"]["after"]
    assert after == "2026-01-03T00:00:00.000Z"
    # The listed page is read again but, unchanged, never fetched.
    assert [p for p in _paths(world) if p.startswith("/blocks/")] == [f"/blocks/{ROW1}/children"]


def test_a_full_listing_retires_a_page_the_store_reports_gone(world: _Store) -> None:
    _run()
    gone_uri, kept_uri = _uri(world, ROW1), _uri(world, ROW2)
    world.pages[ROW1] = {**world.pages[ROW1], "in_trash": True}
    # ROW2 is missing from the listing but the lookup finds it still in the database.
    world.rows = []
    world.pages[ROW2] = {**world.pages[ROW2]}
    _run(full=True)
    assert gone_uri not in world.docs and kept_uri in world.docs
    assert [a["subject"]["source_uri"] for a in world.audits] == [gone_uri]
    assert world.audits[0]["actor"] == SYNC_WORKER_ACTOR
    # A page moved out of the listed database is gone too.
    world.pages[ROW2] = _page(ROW2, "2026-01-03T00:00:00.000Z", parent="")
    _run(full=True)
    assert kept_uri not in world.docs


@pytest.mark.parametrize("lost", ["database", "page"])
def test_a_lost_page_or_database_writes_the_rest_and_retires_nothing(
    world: _Store, lost: str
) -> None:
    _run()
    world.rows = [ROW2]
    world.pages[ROW1] = {**world.pages[ROW1], "in_trash": True}
    world.answers[f"/databases/{DB}" if lost == "database" else f"/pages/{PAGE}"] = httpx.Response(
        404
    )
    stamp = world.states["wiki"]["state_value"]
    _run(full=True)
    assert len(world.docs) == 3 and world.audits == []
    assert _detail(world)["status"] == "failed" and "http_404" in _detail(world)["error"]
    assert world.states["wiki"]["state_value"] == stamp


def test_a_database_in_the_trash_retires_nothing(world: _Store) -> None:
    _run()
    world.database = {**world.database, "in_trash": True}
    world.rows = []
    _run(full=True)
    assert len(world.docs) == 3 and _detail(world)["error"] == f"database {DB}: in the trash"


@pytest.mark.parametrize(("status", "state"), [(401, "credential_invalid"), (429, "rate_limited")])
def test_a_refused_token_or_a_rate_limit_ends_the_turn(
    world: _Store, status: int, state: str
) -> None:
    world.answers[f"/data_sources/{DS}/query"] = httpx.Response(status, text="page names")
    _run()
    detail = _detail(world)
    assert detail["status"] == state and "page names" not in detail["error"]
    assert world.docs == {} and world.states["wiki"]["state_value"] == ""


def test_an_incomplete_result_writes_what_it_listed_and_retires_nothing(world: _Store) -> None:
    _run()
    world.rows = [ROW2]
    world.incomplete = True
    _run(full=True)
    assert len(world.docs) == 3 and _detail(world)["error"] == "listing: incomplete result"


def test_one_page_s_trouble_waits_or_gives_up_on_that_page_alone(world: _Store) -> None:
    world.answers[f"/blocks/{ROW1}/children"] = httpx.Response(503)
    world.answers[f"/blocks/{ROW2}/children"] = httpx.Response(403)
    _run()
    detail = _detail(world)
    assert _uri(world, PAGE) in world.docs and len(world.docs) == 1
    assert detail["status"] == "failed" and detail["unconverted_reasons"] == {"http_403": 1}
    waits = detail["file_waits"]
    assert waits[ROW1][1:] == [1, "http_503"] and waits[ROW2][2] == "http_403"
    # The stamp waits for the page that may still come through.
    assert world.states["wiki"]["state_value"] == ""


def test_a_renamed_page_moves_to_its_new_address(world: _Store) -> None:
    _run()
    old = _uri(world, PAGE)
    world.pages[PAGE] = _page(PAGE, "2026-02-01T00:00:00.000Z", title="Renamed notes")
    _run()
    assert old not in world.docs and _uri(world, PAGE) in world.docs
    assert [a["subject"]["source_uri"] for a in world.audits] == [old]


def test_a_page_past_its_request_bound_is_given_up_as_too_large(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_sync_notion, "_MAX_BLOCK_REQUESTS", 1)
    world.blocks[PAGE] = [_para("Has children.", id=ROW1, has_children=True)]
    world.sources[0] = _source(config={"page_ids": [PAGE], "database_ids": []})
    _run()
    assert world.docs == {} and _detail(world)["unconverted_reasons"] == {"too_large": 1}


def test_an_empty_page_is_given_up_until_it_changes(world: _Store) -> None:
    world.sources[0] = _source(config={"page_ids": [PAGE], "database_ids": []})
    world.blocks[PAGE] = []
    _run()
    assert world.docs == {} and _detail(world)["unconverted_reasons"] == {"no_text": 1}


def test_nested_blocks_are_read_through_and_pages_follow_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = "66666666-6666-4666-8666-666666666666"
    original = "77777777-7777-4777-8777-777777777777"

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/v1/blocks/{PAGE}/children" and "start_cursor" not in request.url.params:
            first = {"type": "toggle", "toggle": {"rich_text": _rt("More")}}
            return httpx.Response(
                200,
                json={
                    "results": [{**first, "id": child, "has_children": True}],
                    "has_more": True,
                    "next_cursor": "c2",
                },
            )
        if path == f"/v1/blocks/{PAGE}/children":
            synced = {
                "type": "synced_block",
                "id": "88888888-8888-4888-8888-888888888888",
                "has_children": False,
                "synced_block": {"synced_from": {"block_id": original}},
            }
            sub = {
                "type": "child_page",
                "id": CHILD,
                "child_page": {"title": "Sub"},
                "has_children": True,
            }
            return httpx.Response(200, json={"results": [synced, sub], "has_more": False})
        if path == f"/v1/blocks/{child}/children":
            return httpx.Response(200, json={"results": [_para("Inside.")], "has_more": False})
        if path == f"/v1/blocks/{original}/children":
            return httpx.Response(200, json={"results": [_para("Shared.")], "has_more": False})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(notion, "get_client", lambda: client)
    blocks = asyncio.run(notion.page_blocks(TOKEN, PAGE, max_requests=10))
    md, skipped = convert.page_markdown(blocks)
    assert md == "More\n\nInside.\n\nShared."
    # A child page is a document of its own: neither read into its parent nor counted.
    assert skipped == {} and convert.child_page_ids(blocks) == [CHILD]


def test_blocks_become_markdown_one_to_one() -> None:
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": _rt("Why")}},
        {
            "type": "paragraph",
            "paragraph": {"rich_text": _rt("We chose ") + _rt("hash", bold=True)},
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt("one")},
            "children": [
                {"type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt("a")}},
                {"type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt("b")}},
            ],
        },
        {"type": "to_do", "to_do": {"rich_text": _rt("done"), "checked": True}},
        {"type": "to_do", "to_do": {"rich_text": _rt("open")}},
        {"type": "quote", "quote": {"rich_text": _rt("Said")}},
        {
            "type": "callout",
            "callout": {"rich_text": _rt("Note"), "icon": {"type": "emoji", "emoji": "!"}},
        },
        {"type": "code", "code": {"rich_text": _rt("select 1\n"), "language": "sql"}},
        {
            "type": "table",
            "table": {"has_column_header": True},
            "children": [
                {"type": "table_row", "table_row": {"cells": [_rt("k"), _rt("a|b")]}},
                {"type": "table_row", "table_row": {"cells": [_rt("1"), _rt("2")]}},
            ],
        },
        {"type": "image", "image": {"caption": _rt("The plan")}},
        {"type": "image", "image": {"caption": []}},
        {"type": "pdf", "pdf": {}},
        {"type": "table_of_contents", "table_of_contents": {}},
        {"type": "unsupported", "unsupported": {}},
    ]
    md, skipped = convert.page_markdown(blocks)
    assert md == (
        "# Why\n\nWe chose **hash**\n\n- one\n  1. a\n  2. b\n- [x] done\n- [ ] open\n\n"
        "> Said\n\n> ! Note\n\n```sql\nselect 1\n```\n\n"
        "| k | a\\|b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "> [image-only: 2 pictures, rationale not extracted]\n\nThe plan"
    )
    assert skipped == {"pdf": 1, "unsupported": 1}
    assert convert.description_of(md) == "We chose **hash**"


def test_a_config_takes_ids_with_or_without_dashes() -> None:
    raw = {"page_ids": [PAGE.replace("-", "").upper()], "database_ids": [DB, DB]}
    assert parse_config("notion", raw) == {"page_ids": [PAGE], "database_ids": [DB]}


def _probe_world(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(notion, "get_client", lambda: client)
    return seen


def _probe_store(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/v1/pages/{PAGE}":
        return httpx.Response(200, json=_page(PAGE, "2026-01-01T00:00:00.000Z", title="Old"))
    if path == f"/v1/databases/{DB}":
        return httpx.Response(200, json={"data_sources": [{"id": DS}]})
    if path == f"/v1/data_sources/{DS}/query":
        rows = [
            _page(ROW2, "2026-03-01T00:00:00.000Z", title="Newest", parent=DB),
            _page(PAGE, "2026-01-01T00:00:00.000Z", title="Old"),
        ]
        return httpx.Response(200, json={"results": rows, "has_more": True})
    return httpx.Response(404)


def test_the_probe_reads_every_listed_id_and_samples_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _probe_world(monkeypatch, _probe_store)
    config = {"page_ids": [PAGE], "database_ids": [DB]}
    found = asyncio.run(source_probes.probe("notion", config, TOKEN))
    assert (found.documents, found.sample, found.more) == (2, ["Newest", "Old"], True)
    assert [r.url.path for r in seen] == [
        f"/v1/pages/{PAGE}",
        f"/v1/databases/{DB}",
        f"/v1/data_sources/{DS}/query",
    ]
    assert json.loads(seen[-1].content)["sorts"][0]["direction"] == "descending"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, source_probes.CredentialRefused),
        (404, source_probes.SourceUnreachable),
        (429, source_probes.SourceUnreachable),
        (500, source_probes.SourceUnreachable),
    ],
)
def test_probe_failures_are_told_apart_and_carry_no_body(
    monkeypatch: pytest.MonkeyPatch, status: int, error: type[Exception]
) -> None:
    _probe_world(monkeypatch, lambda r: httpx.Response(status, text="secret page title"))
    with pytest.raises(error) as raised:
        asyncio.run(source_probes.probe("notion", {"page_ids": [PAGE], "database_ids": []}, TOKEN))
    assert "secret page title" not in str(raised.value)


def test_a_credential_that_is_no_token_is_refused_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _probe_world(monkeypatch, _probe_store)
    for bad in ("", "two words here", "short"):
        with pytest.raises(source_probes.CredentialRefused):
            asyncio.run(
                source_probes.probe("notion", {"page_ids": [PAGE], "database_ids": []}, bad)
            )
    assert seen == []


def _child_page(block_id: str) -> dict[str, Any]:
    return {
        "type": "child_page",
        "id": block_id,
        "child_page": {"title": "x"},
        "has_children": True,
    }


def _under(page_id: str) -> dict[str, Any]:
    return {"type": "page_id", "page_id": page_id}


def _only_the_page(world: _Store) -> None:
    world.sources[0] = _source(config={"page_ids": [PAGE], "database_ids": []})


def test_an_edit_within_90_s_of_a_read_is_read_again(world: _Store) -> None:
    _only_the_page(world)
    world.now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)  # 30 s after the revision
    _run()
    world.blocks[PAGE] = [_para("Edited 20 s after the read.")]
    world.now = datetime(2026, 1, 1, 0, 0, 50, tzinfo=UTC)
    _run()
    doc = world.docs[_uri(world, PAGE)]
    assert doc["input"].content == "Edited 20 s after the read."
    # Read 90 s or more after its revision, the copy is final and skipped.
    world.now = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    _run()
    world.requests.clear()
    _run()
    assert not [r for r in world.requests if r.url.path.startswith("/v1/blocks/")]


def test_a_child_page_is_a_document_of_its_own(world: _Store) -> None:
    _only_the_page(world)
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
    world.pages[CHILD] = _page(
        CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=_under(PAGE)
    )
    world.blocks[CHILD] = [_para("Child text."), _child_page(GRANDCHILD)]
    world.pages[GRANDCHILD] = _page(
        GRANDCHILD, "2026-01-04T00:00:00.000Z", title="Grandchild", parent_of=_under(CHILD)
    )
    out = _run()
    # Found while syncing, the child and its child are synced in the same turn.
    assert out.changed == 3 and world.docs[_uri(world, CHILD)]["input"].content == "Child text."
    assert _detail(world)["child_pages"] == {CHILD: PAGE, GRANDCHILD: CHILD}
    assert "skipped_blocks" not in world.docs[_uri(world, PAGE)]["metadata"]
    # A later turn reads each known child once for its revision and fetches nothing.
    world.requests.clear()
    _run()
    assert sorted(_paths(world)) == sorted(f"/pages/{p}" for p in (PAGE, CHILD, GRANDCHILD))
    # A child moved out from under the page is retired by the full turn, and the
    # grandchild under it too.
    world.pages[CHILD] = {**world.pages[CHILD], "parent": {"type": "workspace", "workspace": True}}
    _run(full=True)
    assert set(world.docs) == {_uri(world, PAGE)}
    assert _detail(world)["child_pages"] == {}


def test_a_trashed_child_page_is_retired_by_the_full_turn(world: _Store) -> None:
    _only_the_page(world)
    world.blocks[PAGE] = [_child_page(CHILD), _para("Parent.")]
    world.pages[CHILD] = _page(
        CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=_under(PAGE)
    )
    _run()
    child_uri = _uri(world, CHILD)
    world.pages[CHILD] = {**world.pages[CHILD], "in_trash": True}
    _run()
    assert child_uri in world.docs  # an incremental turn retires nothing
    _run(full=True)
    assert child_uri not in world.docs and _detail(world)["status"] == "ok"


def test_child_pages_past_the_cap_are_counted_in_their_parent(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_sync_notion, "_MAX_CHILD_PAGES", 1)
    _only_the_page(world)
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD), _child_page(GRANDCHILD)]
    for child in (CHILD, GRANDCHILD):
        world.pages[child] = _page(child, "2026-01-04T00:00:00.000Z", parent_of=_under(PAGE))
    _run()
    assert _detail(world)["child_pages"] == {CHILD: PAGE}
    assert world.docs[_uri(world, PAGE)]["metadata"]["skipped_blocks"] == {"child_page": 1}
    assert _uri(world, GRANDCHILD) not in world.docs


@pytest.mark.parametrize("status", [403, 404])
def test_a_synced_copy_of_an_unshared_original_is_left_out(world: _Store, status: int) -> None:
    _only_the_page(world)
    original = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    copy = {
        "type": "synced_block",
        "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "synced_block": {"synced_from": {"block_id": original}},
    }
    world.blocks[PAGE] = [_para("Own text."), copy]
    world.answers[f"/blocks/{original}/children"] = httpx.Response(status)
    _run()
    doc = world.docs[_uri(world, PAGE)]
    assert doc["input"].content == "Own text."
    assert doc["metadata"]["skipped_blocks"] == {"synced_block": 1}


def test_a_trashed_listed_page_is_retired_by_the_full_turn(world: _Store) -> None:
    _run()
    page_uri = _uri(world, PAGE)
    world.pages[PAGE] = {**world.pages[PAGE], "in_trash": True}
    _run()
    assert page_uri in world.docs and _detail(world)["status"] == "ok"
    _run(full=True)
    assert page_uri not in world.docs and _detail(world)["status"] == "ok"


def test_a_page_the_lookup_cannot_find_is_retired(world: _Store) -> None:
    _run()
    gone_uri = _uri(world, ROW1)
    del world.pages[ROW1]
    world.rows = [ROW2]
    _run(full=True)
    assert gone_uri not in world.docs and len(world.docs) == 2


@pytest.mark.parametrize(("status", "state"), [(401, "credential_invalid"), (429, "rate_limited")])
def test_a_refused_or_rate_limited_lookup_stops_the_retiring(
    world: _Store, status: int, state: str
) -> None:
    _run()
    world.rows = []
    world.answers[f"/pages/{ROW1}"] = httpx.Response(status)
    world.answers[f"/pages/{ROW2}"] = httpx.Response(404)
    _run(full=True)
    assert len(world.docs) == 3 and _detail(world)["status"] == state


def test_lists_tables_and_links_keep_their_shape() -> None:
    blocks = [
        {
            "type": "to_do",
            "to_do": {"rich_text": _rt("parent")},
            "children": [{"type": "to_do", "to_do": {"rich_text": _rt("child"), "checked": True}}],
        },
        {
            "type": "table",
            "table": {"table_width": 3, "has_column_header": False},
            "children": [
                {"type": "table_row", "table_row": {"cells": [_rt("a"), _rt("b")]}},
                {"type": "table_row", "table_row": {"cells": [_rt("1"), _rt("2"), _rt("3")]}},
            ],
        },
        {"type": "bookmark", "bookmark": {"url": "notion://internal"}},
        {"type": "embed", "embed": {"url": "https://example.com/x", "caption": []}},
    ]
    md, skipped = convert.page_markdown(blocks)
    assert md == (
        "- [ ] parent\n  - [x] child\n\n"
        "| a | b |  |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n\n"
        "[https://example.com/x](https://example.com/x)"
    )
    assert skipped == {"bookmark": 1}


@pytest.mark.parametrize("trashed", ["page", "database"])
def test_the_probe_refuses_a_trashed_page_or_database(
    monkeypatch: pytest.MonkeyPatch, trashed: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/pages/{PAGE}":
            return httpx.Response(
                200, json={**_page(PAGE, "2026-01-01T00:00:00.000Z"), "in_trash": trashed == "page"}
            )
        if request.url.path == f"/v1/databases/{DB}":
            return httpx.Response(200, json={"data_sources": [], "in_trash": trashed == "database"})
        return httpx.Response(404)

    _probe_world(monkeypatch, handler)
    with pytest.raises(source_probes.SourceUnreachable, match="in the trash"):
        asyncio.run(
            source_probes.probe("notion", {"page_ids": [PAGE], "database_ids": [DB]}, TOKEN)
        )


def _under_block(block_id: str) -> dict[str, Any]:
    return {"type": "block_id", "block_id": block_id}


COLUMNS = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
COLUMN = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
ORIGINAL = "ffffffff-ffff-4fff-8fff-ffffffffffff"


@pytest.mark.parametrize("held_in", ["column", "synced copy"])
def test_a_child_page_under_a_block_survives_a_full_turn(world: _Store, held_in: str) -> None:
    _only_the_page(world)
    if held_in == "column":
        world.blocks[PAGE] = [
            _para("Parent."),
            {"type": "column_list", "id": COLUMNS, "column_list": {}, "has_children": True},
        ]
        world.blocks[COLUMNS] = [
            {"type": "column", "id": COLUMN, "column": {}, "has_children": True}
        ]
        world.blocks[COLUMN] = [_child_page(CHILD)]
        parent = _under_block(COLUMN)
    else:
        copy = {
            "type": "synced_block",
            "id": COLUMNS,
            "synced_block": {"synced_from": {"block_id": ORIGINAL}},
        }
        world.blocks[PAGE] = [_para("Parent."), copy]
        world.blocks[ORIGINAL] = [_child_page(CHILD)]
        parent = _under_block(ORIGINAL)
    world.pages[CHILD] = _page(CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=parent)
    _run()
    _run(full=True)
    assert _uri(world, CHILD) in world.docs and world.audits == []
    assert _detail(world)["child_pages"] == {CHILD: PAGE}


def test_a_child_missing_while_the_turn_fails_is_kept_and_read_again(world: _Store) -> None:
    _only_the_page(world)
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
    world.pages[CHILD] = _page(
        CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=_under(PAGE)
    )
    _run()
    child = world.pages.pop(CHILD)
    world.answers[f"/pages/{PAGE}"] = httpx.Response(500)
    _run(full=True)
    assert _detail(world)["status"] == "failed"
    assert _detail(world)["child_pages"] == {CHILD: PAGE}  # not forgotten by a failed turn
    del world.answers[f"/pages/{PAGE}"]
    world.pages[CHILD] = {**child, "last_edited_time": "2026-02-01T00:00:00.000Z"}
    world.blocks[CHILD] = [_para("Back again.")]
    _run()
    assert world.docs[_uri(world, CHILD)]["input"].content == "Back again."


def test_a_probe_of_twenty_databases_fits_its_time_bound_at_the_store_s_pace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    databases = [f"{n:08x}-0000-4000-8000-000000000000" for n in range(1, 21)]
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/databases/"):
            n = request.url.path.split("/")[-1][:8]
            sources = [
                {"id": f"{n}-1111-4111-8111-111111111111"},
                {"id": f"{n}-2222-4222-8222-222222222222"},
            ]
            return httpx.Response(200, json={"data_sources": sources})
        return httpx.Response(200, json={"results": [], "has_more": False})

    monkeypatch.setattr(notion, "PAGE_PACE_S", 0.35)
    # Another integration's queue is long; this probe does not wait on it.
    monkeypatch.setattr(notion, "_next_request_at", {"0" * 16: notion.time.monotonic() + 600.0})
    monkeypatch.setattr(notion.asyncio, "sleep", _sleep)
    seen = _probe_world(monkeypatch, handler)
    found = asyncio.run(
        source_probes.probe("notion", {"page_ids": [], "database_ids": databases}, TOKEN)
    )
    # Every database is read; only three first data sources are queried.
    assert len(seen) == 23 and found.more
    # The longest wait is the probe's length at the real pace, well inside its bound.
    assert max(waits) < float(settings.knowledge_source_probe_timeout_s) / 2


@pytest.mark.parametrize("held_in", ["page", "column"])
def test_a_child_missing_during_a_cut_turn_is_kept_and_still_known(
    world: _Store, monkeypatch: pytest.MonkeyPatch, held_in: str
) -> None:
    world.sources[0] = _source(config={"page_ids": [PAGE, ROW1, ROW2], "database_ids": []})
    for row in (ROW1, ROW2):
        world.pages[row] = _page(row, "2026-01-02T00:00:00.000Z")
    if held_in == "page":
        world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
        parent = _under(PAGE)
    else:
        world.blocks[PAGE] = [
            _para("Parent."),
            {"type": "column_list", "id": COLUMNS, "column_list": {}, "has_children": True},
        ]
        world.blocks[COLUMNS] = [
            {"type": "column", "id": COLUMN, "column": {}, "has_children": True}
        ]
        world.blocks[COLUMN] = [_child_page(CHILD)]
        parent = _under_block(COLUMN)
    world.pages[CHILD] = _page(CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=parent)
    _run()
    child_uri = _uri(world, CHILD)
    # The child does not answer while a full turn is cut by the pass bound.
    child = world.pages.pop(CHILD)
    for row in (ROW1, ROW2):
        world.pages[row] = _page(row, "2026-02-01T00:00:00.000Z")
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    out = _run(full=True)
    assert out.cut and _detail(world)["child_pages"] == {CHILD: PAGE}
    # A later full turn finds it live again: kept, still known, nothing retired.
    world.pages[CHILD] = child
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 40)
    _run(full=True)
    assert child_uri in world.docs and world.audits == []
    assert _detail(world)["child_pages"] == {CHILD: PAGE}


def test_a_known_child_with_no_document_is_forgotten_once_a_full_turn_reads_it_gone(
    world: _Store,
) -> None:
    _only_the_page(world)
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
    world.pages[CHILD] = _page(
        CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=_under(PAGE)
    )
    world.blocks[CHILD] = []  # no text: given up, so it holds no document
    _run()
    assert _uri(world, CHILD) not in world.docs and _detail(world)["child_pages"] == {CHILD: PAGE}
    del world.pages[CHILD]
    _run()
    assert _detail(world)["child_pages"] == {CHILD: PAGE}  # an incremental turn keeps it
    _run(full=True)
    assert _detail(world)["status"] == "ok" and _detail(world)["child_pages"] == {}


def test_a_new_child_whose_read_fails_stays_known_and_is_synced_next_turn(world: _Store) -> None:
    _only_the_page(world)
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
    world.pages[CHILD] = _page(
        CHILD, "2026-01-04T00:00:00.000Z", title="Child", parent_of=_under(PAGE)
    )
    world.answers[f"/pages/{CHILD}"] = httpx.Response(503)
    _run(full=True)
    detail = _detail(world)
    assert detail["status"] == "failed" and "http_503" in detail["error"]
    assert detail["child_pages"] == {CHILD: PAGE} and _uri(world, CHILD) not in world.docs
    del world.answers[f"/pages/{CHILD}"]
    _run()
    assert _uri(world, CHILD) in world.docs


def test_a_rename_s_old_address_is_retired_when_a_new_child_read_is_rate_limited(
    world: _Store,
) -> None:
    _only_the_page(world)
    _run()
    old = _uri(world, PAGE)
    world.pages[PAGE] = _page(PAGE, "2026-02-01T00:00:00.000Z", title="Renamed notes")
    world.blocks[PAGE] = [_para("Parent."), _child_page(CHILD)]
    world.answers[f"/pages/{CHILD}"] = httpx.Response(429)
    _run()
    assert old not in world.docs and _uri(world, PAGE) in world.docs
    assert [a["subject"]["source_uri"] for a in world.audits] == [old]
    assert _detail(world)["status"] == "rate_limited"
    assert _detail(world)["child_pages"] == {CHILD: PAGE}


def test_another_integration_s_busy_pacer_does_not_slow_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(notion, "PAGE_PACE_S", 0.35)
    # Another integration has a long queue of requests ahead.
    busy = {"0" * 16: notion.time.monotonic() + 600.0}
    monkeypatch.setattr(notion, "_next_request_at", busy)
    monkeypatch.setattr(notion.asyncio, "sleep", _sleep)
    _probe_world(monkeypatch, _probe_store)
    asyncio.run(source_probes.probe("notion", {"page_ids": [PAGE], "database_ids": [DB]}, TOKEN))
    assert waits == pytest.approx([0.35, 0.70], abs=0.05)


def _mock_mining(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The extraction leg's outside calls answered here; the addresses it mined."""
    from server.llm import customer
    from server.memory.app import decision_extract
    from server.observability import budget, events
    from server.workers import cost_event

    mined: list[str] = []

    async def _extract_document(org: str, ns: str, uri: str, *, dry_run: bool, **kw: Any) -> Any:
        mined.append(uri)
        return decision_extract.ExtractReport(uri, dry_run)

    async def _none() -> frozenset[str]:
        return frozenset()

    async def _under_cap(*a: Any, **kw: Any) -> bool:
        return False

    class _Emitter:
        async def flush_now(self) -> None:
            return None

    monkeypatch.setattr(decision_extract, "no_model_error", lambda: "")
    monkeypatch.setattr(decision_extract, "extract_document", _extract_document)
    monkeypatch.setattr(customer, "switched_off_orgs", _none)
    monkeypatch.setattr(budget, "namespace_over_cap", _under_cap)
    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", lambda **kw: None)
    return mined


def test_a_page_store_s_pages_are_mined_by_the_same_leg(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mined = _mock_mining(monkeypatch)
    _only_the_page(world)
    result = _run()
    uri = _uri(world, PAGE)
    assert mined == [uri] and result.extracted == 1
    text = sha256_text("Design notes\nText of 1111.\n{}")
    revision = f"2026-01-01T00:00:00.000Z@{text}"  # revision@text_sha256
    assert _detail(world)["mined"] == {uri: revision}
    _run()
    assert mined == [uri]  # mined once per revision and text


def test_the_pacer_forgets_only_integrations_no_longer_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    now = notion.time.monotonic()
    waiting = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
    queue = {f"expired{n:09d}": now - 1.0 for n in range(40)}
    queue |= {f"later{n:011d}": now + 60.0 for n in range(24)}
    queue[waiting] = now + 10.0
    monkeypatch.setattr(notion, "PAGE_PACE_S", 0.35)
    monkeypatch.setattr(notion, "_next_request_at", queue)
    monkeypatch.setattr(notion.asyncio, "sleep", _sleep)
    asyncio.run(notion._pace(TOKEN))
    assert not [k for k in queue if k.startswith("expired")]
    assert len([k for k in queue if k.startswith("later")]) == 24
    # The waiting integration still waits its turn, then queues one pace later.
    assert waits == pytest.approx([10.0], abs=0.05)
    assert queue[waiting] == pytest.approx(now + 10.35, abs=0.05)


def test_an_edited_page_goes_before_re_reads_that_only_settle(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources[0] = _source(config={"page_ids": [PAGE, ROW2], "database_ids": [DB]})
    world.rows = [ROW1]
    for page_id in (PAGE, ROW1, ROW2):
        world.pages[page_id] = _page(
            page_id, "2026-03-01T00:00:00.000Z", parent=DB if page_id == ROW1 else ""
        )
    world.now = datetime(2026, 3, 1, 0, 0, 30, tzinfo=UTC)
    _run(full=True)  # all three written 30 s after their revision: none settled
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    world.pages[ROW1] = _page(ROW1, "2026-03-01T00:04:00.000Z", parent=DB)
    world.blocks[ROW1] = [_para("Edited in the database.")]
    world.now = datetime(2026, 3, 1, 0, 5, tzinfo=UTC)
    out = _run()
    assert out.cut
    assert world.docs[_uri(world, ROW1)]["input"].content == "Edited in the database."


def test_a_same_text_read_before_the_margin_is_written_once_and_not_mined_again(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mined = _mock_mining(monkeypatch)
    _only_the_page(world)
    world.now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)  # 30 s after the revision
    _run()
    assert world.upserts == 1 and len(mined) == 1
    world.now = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    _run()  # read again, same text: written once with the new read time, not mined
    uri = _uri(world, PAGE)
    assert world.upserts == 2 and len(mined) == 1
    assert world.docs[uri]["metadata"]["read_at"] == "2026-01-01T00:05:00.000Z"
    world.requests.clear()
    _run()  # past the margin now: skipped with no fetch
    assert not [p for p in _paths(world) if p.startswith("/blocks/")]
    assert world.upserts == 2


@pytest.mark.parametrize("text", ["Edited in the same minute.", "Text  of 1111."])
def test_other_text_at_the_same_revision_is_written_and_mined(
    world: _Store, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    mined = _mock_mining(monkeypatch)
    _only_the_page(world)
    world.now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    _run()
    world.blocks[PAGE] = [_para(text)]  # another text, or only other whitespace
    world.now = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    _run()
    uri = _uri(world, PAGE)
    assert world.docs[uri]["input"].content == text
    assert world.upserts == 2 and mined == [uri, uri]


def test_more_unsettled_pages_than_the_pass_bound_settle_and_the_turn_completes(
    world: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [f"{n:08x}-1111-4111-8111-111111111111" for n in range(1, 6)]
    world.sources[0] = _source(config={"page_ids": [], "database_ids": [DB]})
    world.rows = rows
    for row in rows:
        world.pages[row] = _page(row, "2026-03-01T00:00:00.000Z", parent=DB)
    world.now = datetime(2026, 3, 1, 0, 0, 30, tzinfo=UTC)
    _run(full=True)  # all five written 30 s after their revision: none settled
    stamp = world.states["wiki"]["state_value"]
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    world.now = datetime(2026, 3, 1, 0, 5, tzinfo=UTC)
    for _ in range(2):  # ceil(5 / 2) = 3 passes: the first two are cut
        assert _run().cut
        assert world.states["wiki"]["state_value"] == stamp
    out = _run()
    assert not out.cut and _detail(world)["status"] == "ok"
    assert world.states["wiki"]["state_value"] == "2026-03-01T00:00:00.000Z"
    assert world.upserts == 10
    world.requests.clear()
    _run()
    assert not [p for p in _paths(world) if p.startswith("/blocks/")]


def test_a_database_page_read_within_the_margin_is_listed_again_by_an_incremental_turn(
    world: _Store,
) -> None:
    world.sources[0] = _source(config={"page_ids": [], "database_ids": [DB]})
    world.rows = [ROW1]
    world.pages[ROW1] = _page(ROW1, "2026-03-01T00:00:00.000Z", parent=DB)
    world.now = datetime(2026, 3, 1, 0, 1, 15, tzinfo=UTC)  # first read 75 s after its edit
    _run(full=True)
    world.blocks[ROW1] = [_para("Edited in the same minute.")]
    world.now = datetime(2026, 3, 1, 0, 20, tzinfo=UTC)
    world.requests.clear()
    _run()  # incremental: listed again and rewritten
    assert f"/blocks/{ROW1}/children" in _paths(world)
    assert world.docs[_uri(world, ROW1)]["input"].content == "Edited in the same minute."
    assert world.docs[_uri(world, ROW1)]["metadata"]["read_at"] == "2026-03-01T00:20:00.000Z"
