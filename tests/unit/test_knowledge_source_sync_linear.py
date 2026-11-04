"""The sync turn of a tracker project's documents
(server/knowledge/app/source_sync_linear.py) over the in-memory registry and
document store of test_knowledge_source_sync and a GraphQL API answered by a mock
transport: what a turn lists, fetches, writes and retires, and how each failure is
told apart. Every response is written here; none is a recording of real content."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import httpx
import pytest

from server.clients import linear
from server.core.secrets_at_rest import seal
from server.knowledge.app import source_sync
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.settings import settings
from tests.unit.test_knowledge_source_sync import _detail, _source, _World
from tests.unit.test_knowledge_source_sync import world as world  # the fixture

PROJECT = "0b6d7a52-1c1e-4c2b-9a4e-5d0f3b1e2a01"
OTHER = "7e3f9c10-8a2b-4d5e-b6f7-0123456789ab"
KEY = "lin_api_unit-test-key"


def _doc_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _node(n: int, updated: str, *, title: str = "") -> dict[str, Any]:
    title = title or f"Design {n}"
    slug = title.lower().replace(" ", "-")
    return {
        "id": _doc_id(n),
        "title": title,
        "updatedAt": updated,
        "url": f"https://tracker.example.com/acme/document/{slug}-{n}?ref=x",
    }


class _Tracker:
    """A project's documents as the GraphQL API answers them, two to a page."""

    page = 2

    def __init__(self) -> None:
        self.projects: dict[str, list[dict[str, Any]]] = {PROJECT: [], OTHER: []}
        self.requests: list[dict[str, Any]] = []
        self.headers: list[str] = []
        # Answers that override the store's, by operation name or document id.
        self.answers: dict[str, Any] = {}
        # Document id -> its lookup answer (trashed, archivedAt, project).
        self.states: dict[str, dict[str, Any]] = {}

    def fetches(self, document_id: str = "") -> int:
        return sum(
            1
            for r in self.requests
            if r["op"] == "Document" and (not document_id or r["variables"]["id"] == document_id)
        )

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        op = body["query"].split("query ", 1)[1].split("(", 1)[0]
        variables = body["variables"]
        self.requests.append({"op": op, "variables": variables})
        self.headers.append(request.headers["authorization"])
        for key in (op, variables.get("id")):
            if key in self.answers:
                answer = self.answers[key]
                return answer(request) if callable(answer) else answer
        if op == "ProjectDocuments":
            nodes = self.projects.get(variables["id"])
            if nodes is None:
                return _not_found("Project")
            start = int(variables.get("after") or 0)
            page = nodes[start : start + self.page]
            more = start + self.page < len(nodes)
            info = {"hasNextPage": more, "endCursor": str(start + self.page) if more else None}
            data = {"id": variables["id"], "documents": {"nodes": page, "pageInfo": info}}
            return httpx.Response(200, json={"data": {"project": data}})
        if op == "Document":
            node = self._find(variables["id"])
            if node is None:
                return _not_found("Document")
            content = f"# {node['title']}\n\nWe keep the hash join.\n"
            return httpx.Response(200, json={"data": {"document": {**node, "content": content}}})
        if op == "DocumentState":
            if variables["id"] in self.states:
                state = self.states[variables["id"]]
                return httpx.Response(200, json={"data": {"document": state}})
            return _not_found("Document")
        return httpx.Response(400)

    def _find(self, document_id: str) -> dict[str, Any] | None:
        for nodes in self.projects.values():
            for node in nodes:
                if node["id"] == document_id:
                    return node
        return None


def _not_found(entity: str) -> httpx.Response:
    error = {
        "message": "Entity not found",
        "extensions": {
            "type": "invalid input",
            "userPresentableMessage": f"Could not find referenced {entity}.",
        },
    }
    return httpx.Response(400, json={"errors": [error], "data": None})


def _graphql_error(status: int, code: str, message: str = "x") -> httpx.Response:
    error = {"message": message, "extensions": {"code": code}}
    return httpx.Response(status, json={"errors": [error], "data": None})


def _linear_source(**kw: Any) -> Any:
    """The harness's source, of this kind."""
    fields = {
        "kind": "linear",
        "config": {"project_ids": [PROJECT, OTHER]},
        "credential_enc": seal("knowledge_sources.credential", KEY),
        **kw,
    }
    return dataclasses.replace(_source(), **fields)


@pytest.fixture
def tracker(world: _World, monkeypatch: pytest.MonkeyPatch) -> _Tracker:
    t = _Tracker()
    client = httpx.AsyncClient(transport=httpx.MockTransport(t.answer))
    monkeypatch.setattr(linear, "get_client", lambda: client)
    monkeypatch.setattr(linear, "PAGE_PACE_S", 0.0)
    world.sources = [_linear_source()]
    return t


def _sync(**kw: Any) -> source_sync.PassResult:
    return asyncio.run(source_sync.run_sync(**kw))


def test_a_turn_writes_every_project_s_documents_across_pages(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2, 3)]
    tracker.projects[OTHER] = [_node(4, "2026-01-04T00:00:00.000Z")]
    result = _sync()
    assert result.changed == 4 and result.failed == 0
    listings = [r for r in tracker.requests if r["op"] == "ProjectDocuments"]
    assert [r["variables"].get("after") for r in listings] == [None, "2", None]
    # The key goes as it is, never as a bearer token.
    assert set(tracker.headers) == {KEY}
    inp, actor = world.upserts[0]
    assert actor == SYNC_WORKER_ACTOR and inp.source_class == "imported"
    assert inp.source_uri == "https://tracker.example.com/acme/document/design-1-1"
    assert inp.title == "Design 1" and inp.description == "We keep the hash join."
    assert inp.tags == ["source-kind:design_doc", "repo:abc", "source:decks"]
    assert inp.metadata == {
        "source_kind": "linear",
        "source_name": "decks",
        "source_revision": "2026-01-01T00:00:00.000Z",
        "ingested_from": _doc_id(1),
        "ingested_format": "markdown",
    }
    assert inp.original_bytes_b64 is None
    detail = _detail(world)
    assert detail["status"] == "ok" and detail["documents_seen"] == 4
    assert world.states["decks"]["state_value"] == "2026-01-04T00:00:00.000Z"


def test_an_unchanged_document_is_not_fetched_and_a_changed_one_is(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.projects[OTHER] = [_node(2, "2026-01-02T00:00:00.000Z")]
    _sync()
    tracker.requests.clear()
    tracker.projects[OTHER] = [_node(2, "2026-02-01T00:00:00.000Z")]
    result = _sync()
    assert result.changed == 1
    assert tracker.fetches(_doc_id(1)) == 0 and tracker.fetches(_doc_id(2)) == 1


def test_a_renamed_document_moves_to_its_new_address(world: _World, tracker: _Tracker) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    _sync()
    tracker.projects[PROJECT] = [_node(1, "2026-01-05T00:00:00.000Z", title="Planner")]
    result = _sync()
    assert result.changed == 1 and result.removed == 1
    assert list(world.docs) == ["https://tracker.example.com/acme/document/planner-1"]


@pytest.mark.parametrize(
    "state, retired",
    [
        (None, True),  # the lookup finds nothing
        ({"id": "x", "trashed": True, "project": {"id": PROJECT}}, True),
        ({"id": "x", "archivedAt": "2026-01-09T00:00:00.000Z", "project": {"id": PROJECT}}, True),
        ({"id": "x", "project": {"id": "11111111-2222-4333-8444-555555555555"}}, True),
        ({"id": "x", "project": None}, True),
        # Missed by the list, but the tracker still has it in the project.
        ({"id": "x", "trashed": False, "project": {"id": OTHER}}, False),
    ],
)
def test_a_document_missing_from_a_complete_list_is_retired_only_once_the_lookup_agrees(
    world: _World, tracker: _Tracker, state: dict[str, Any] | None, retired: bool
) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.projects[OTHER] = [_node(2, "2026-01-02T00:00:00.000Z")]
    _sync()
    tracker.projects[OTHER] = []
    if state is not None:
        tracker.states[_doc_id(2)] = state
    result = _sync()
    assert result.removed == (1 if retired else 0)
    assert (len(world.docs) == 1) is retired
    deletes = [a for a in world.audits if a["event"] == "knowledge.delete"]
    assert len(deletes) == (1 if retired else 0)
    if retired:
        assert deletes[0]["actor"] == SYNC_WORKER_ACTOR


def test_a_project_the_key_cannot_see_retires_nothing(world: _World, tracker: _Tracker) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.projects[OTHER] = [_node(2, "2026-01-02T00:00:00.000Z")]
    _sync()
    stamp = world.states["decks"]["state_value"]
    del tracker.projects[OTHER]  # the key lost the project: its list is refused
    tracker.projects[PROJECT].append(_node(3, "2026-01-03T00:00:00.000Z"))
    result = _sync()
    assert result.removed == 0 and result.changed == 1 and len(world.docs) == 3
    detail = _detail(world)
    assert detail["status"] == "failed" and detail["error"] == f"project {OTHER}: not_found"
    assert world.states["decks"]["state_value"] == stamp
    assert not [r for r in tracker.requests if r["op"] == "DocumentState"]


@pytest.mark.parametrize(
    "answer",
    [
        _graphql_error(400, "RATELIMITED", "Rate limit exceeded"),
        _graphql_error(200, "RATELIMITED"),
        httpx.Response(429, text="slow down"),
    ],
)
def test_a_rate_limit_in_any_shape_ends_the_turn(
    world: _World, tracker: _Tracker, answer: httpx.Response
) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    _sync()
    tracker.projects[PROJECT] = []
    tracker.answers["ProjectDocuments"] = answer
    result = _sync()
    assert result.removed == 0 and len(world.docs) == 1
    detail = _detail(world)
    assert detail["status"] == "rate_limited" and detail["error"] == "rate_limited"


def test_a_rate_limit_on_a_fetch_stops_the_turn(world: _World, tracker: _Tracker) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2)]
    tracker.answers[_doc_id(1)] = _graphql_error(400, "RATELIMITED")
    result = _sync()
    assert result.changed == 0 and tracker.fetches(_doc_id(2)) == 0
    assert _detail(world)["status"] == "rate_limited"
    assert world.states["decks"]["state_value"] == ""


def test_a_refused_key_is_the_turn_s_credential(world: _World, tracker: _Tracker) -> None:
    tracker.answers["ProjectDocuments"] = _graphql_error(400, "AUTHENTICATION_ERROR")
    _sync()
    detail = _detail(world)
    assert detail["status"] == "credential_invalid"
    assert detail["error"] == f"project {PROJECT}: credential_refused"


def test_an_answer_with_any_error_is_no_list(world: _World, tracker: _Tracker) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.projects[OTHER] = [_node(2, "2026-01-02T00:00:00.000Z")]
    _sync()
    # Some data and an error: the list may be missing documents.
    partial = {
        "data": {"project": {"id": OTHER, "documents": {"nodes": [], "pageInfo": {}}}},
        "errors": [{"message": "x", "extensions": {"type": "internal error"}}],
    }
    tracker.answers["ProjectDocuments"] = httpx.Response(200, json=partial)
    result = _sync()
    assert result.removed == 0 and len(world.docs) == 2
    assert _detail(world)["error"] == f"project {PROJECT}: graphql_internal_error"


def test_a_next_page_without_a_cursor_is_no_list(world: _World, tracker: _Tracker) -> None:
    info = {"hasNextPage": True, "endCursor": None}
    page = {"data": {"project": {"id": PROJECT, "documents": {"nodes": [], "pageInfo": info}}}}
    tracker.answers["ProjectDocuments"] = httpx.Response(200, json=page)
    _sync()
    assert _detail(world)["error"] == f"project {PROJECT}: no_cursor"


def test_one_document_s_failure_waits_for_that_document_alone(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2)]
    tracker.answers[_doc_id(1)] = httpx.Response(502)
    first = _sync()
    assert first.changed == 1 and len(world.docs) == 1
    detail = _detail(world)
    assert detail["status"] == "failed" and detail["error"] == f"document {_doc_id(1)}: http_502"
    assert world.states["decks"]["state_value"] == ""
    _sync()
    third = _sync()
    # Three passes on the same revision: given up, and the stamp moves.
    assert third.unconverted == 1
    assert _detail(world)["unconverted_reasons"] == {"http_502": 1}
    assert world.states["decks"]["state_value"] == "2026-01-02T00:00:00.000Z"
    tracker.requests.clear()
    _sync()
    assert tracker.fetches(_doc_id(1)) == 0


def test_a_document_over_the_size_bound_is_given_up_at_once(
    world: _World, tracker: _Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 64)
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    result = _sync()
    assert result.unconverted == 1 and not world.docs
    assert _detail(world)["unconverted_reasons"] == {"too_large": 1}
    assert _detail(world)["status"] == "ok"
    tracker.requests.clear()
    _sync()
    assert tracker.fetches() == 0
    # A new revision is tried again.
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 10**6)
    tracker.projects[PROJECT] = [_node(1, "2026-01-02T00:00:00.000Z")]
    assert _sync().changed == 1


def test_a_document_another_writer_holds_is_not_fetched(world: _World, tracker: _Tracker) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    uri = "https://tracker.example.com/acme/document/design-1-1"
    world.docs[uri] = {"actor": "alice", "tags": [], "metadata": {}}
    result = _sync()
    assert result.skipped == 1 and tracker.fetches() == 0
    assert world.docs[uri]["actor"] == "alice"


def test_the_document_bound_cuts_the_turn(
    world: _World, tracker: _Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2, 3)]
    first = _sync()
    assert first.changed == 2 and first.cut
    assert world.states["decks"]["state_value"] == ""
    second = _sync()
    assert second.changed == 1 and _detail(world)["error"] == ""


def test_a_credential_that_no_longer_opens_calls_nothing(world: _World, tracker: _Tracker) -> None:
    world.sources = [_linear_source(credential_enc="not-sealed")]
    _sync()
    assert _detail(world)["status"] == "credential_invalid" and not tracker.requests


def _two_synced(tracker: _Tracker) -> None:
    """Documents 1 and 2 synced, then document 2 dropped from its project's list."""
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.projects[OTHER] = [_node(2, "2026-01-02T00:00:00.000Z")]
    _sync()
    tracker.projects[OTHER] = []


def _lookups(tracker: _Tracker) -> int:
    return sum(1 for r in tracker.requests if r["op"] == "DocumentState")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"data": {}}),
        httpx.Response(200, json={"data": {"document": None}}),
        httpx.Response(503),
        _graphql_error(400, "FORBIDDEN"),
        _graphql_error(200, "INTERNAL_ERROR"),
    ],
)
def test_a_lookup_that_does_not_say_not_found_retires_nothing(
    world: _World, tracker: _Tracker, answer: httpx.Response
) -> None:
    _two_synced(tracker)
    tracker.answers["DocumentState"] = answer
    result = _sync()
    assert _lookups(tracker) == 1
    assert result.removed == 0 and len(world.docs) == 2
    assert _detail(world)["status"] == "ok"


@pytest.mark.parametrize(
    "answer, status",
    [
        (_graphql_error(400, "RATELIMITED"), "rate_limited"),
        (httpx.Response(429), "rate_limited"),
        (_graphql_error(400, "AUTHENTICATION_ERROR"), "credential_invalid"),
        (httpx.Response(401), "credential_invalid"),
    ],
)
def test_a_rate_limit_or_a_refused_key_on_a_lookup_stops_the_retiring(
    world: _World, tracker: _Tracker, answer: httpx.Response, status: str
) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2, 3)]
    _sync()
    tracker.projects[PROJECT] = []
    tracker.answers["DocumentState"] = answer
    result = _sync()
    assert _lookups(tracker) == 1  # the first lookup stops the others
    assert result.removed == 0 and len(world.docs) == 3
    assert _detail(world)["status"] == status


def test_a_stored_document_with_no_usable_id_is_never_looked_up_or_retired(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    uri = "https://tracker.example.com/acme/document/kept-9"
    world.docs[uri] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:decks"],
        "metadata": {"ingested_from": "not-an-id", "source_revision": "x"},
    }
    result = _sync()
    assert result.removed == 0 and uri in world.docs and _lookups(tracker) == 0


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"data": {}}),
        httpx.Response(200, json={"data": {"document": None}}),
    ],
)
def test_a_fetch_answered_without_the_document_waits(
    world: _World, tracker: _Tracker, answer: httpx.Response
) -> None:
    tracker.projects[PROJECT] = [_node(1, "2026-01-01T00:00:00.000Z")]
    tracker.answers["Document"] = answer
    result = _sync()
    assert result.changed == 0 and result.unconverted == 0
    detail = _detail(world)
    assert detail["status"] == "failed" and detail["error"] == f"document {_doc_id(1)}: no_document"
    assert detail["file_waits"][_doc_id(1)][1] == 1


def test_a_refused_fetch_is_given_up_at_once_and_an_internal_error_waits(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2)]
    tracker.answers[_doc_id(1)] = _graphql_error(200, "FORBIDDEN")
    tracker.answers[_doc_id(2)] = _graphql_error(200, "INTERNAL_ERROR")
    result = _sync()
    assert result.unconverted == 1
    detail = _detail(world)
    assert detail["unconverted_reasons"] == {"graphql_forbidden": 1}
    assert detail["status"] == "failed"
    assert detail["error"] == f"document {_doc_id(2)}: graphql_internal_error"
    tracker.requests.clear()
    _sync()
    assert tracker.fetches(_doc_id(1)) == 0 and tracker.fetches(_doc_id(2)) == 1


def test_the_key_and_the_tracker_s_text_never_reach_the_state(
    world: _World, tracker: _Tracker
) -> None:
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2)]
    echo = f"bad key {KEY} for Roadmap"
    tracker.answers[_doc_id(1)] = _graphql_error(200, "FORBIDDEN", echo)
    tracker.answers[_doc_id(2)] = _graphql_error(502, "INTERNAL_ERROR", echo)
    _sync()
    assert tracker.fetches(_doc_id(1)) == 1 and tracker.fetches(_doc_id(2)) == 1
    state = json.dumps(world.states["decks"])
    assert KEY not in state and "Roadmap" not in state
    # The list refused for the key, with the key in the tracker's text.
    tracker.answers[OTHER] = _graphql_error(400, "AUTHENTICATION_ERROR", echo)
    _sync()
    assert _detail(world)["status"] == "credential_invalid"
    state = json.dumps(world.states["decks"])
    assert KEY not in state and "Roadmap" not in state


def test_list_pages_are_paced(
    world: _World, tracker: _Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(linear, "PAGE_PACE_S", 0.5)
    monkeypatch.setattr(linear, "_sleep", _sleep)
    tracker.projects[PROJECT] = [_node(n, f"2026-01-0{n}T00:00:00.000Z") for n in (1, 2, 3, 4, 5)]
    _sync()
    assert slept == [0.5, 0.5]  # three pages of the first project, one of the other
