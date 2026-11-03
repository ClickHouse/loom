"""The tracker-documents probe over a mock transport: the request it sends, what it
counts and samples, and how each failure is told apart. No error carries the
tracker's response body."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from server.clients import linear
from server.knowledge.app import source_probes
from server.knowledge.domain.sources import SourceConfigError, parse_config

PROJECT = "0b6d7a52-1c1e-4c2b-9a4e-5d0f3b1e2a01"
OTHER = "7e3f9c10-8a2b-4d5e-b6f7-0123456789ab"
CONFIG = {"project_ids": [PROJECT, OTHER]}
KEY = "lin_api_unit-test-key"
SECRET_TEXT = "Roadmap for the private launch"


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_record))
    monkeypatch.setattr(linear, "get_client", lambda: client)
    return seen


def _page(nodes: list[dict[str, Any]], more: bool = False) -> httpx.Response:
    info = {"hasNextPage": more, "endCursor": "c1" if more else None}
    project = {"id": PROJECT, "documents": {"nodes": nodes, "pageInfo": info}}
    return httpx.Response(200, json={"data": {"project": project}})


def _tracker(request: httpx.Request) -> httpx.Response:
    project = json.loads(request.content)["variables"]["id"]
    if project == PROJECT:
        return _page(
            [
                {"id": "a", "title": "Older", "updatedAt": "2026-01-01T00:00:00.000Z"},
                {"id": "b", "title": "Newer", "updatedAt": "2026-02-01T00:00:00.000Z"},
            ]
        )
    return _page([{"id": "c", "title": "Other", "updatedAt": "2026-01-15T00:00:00.000Z"}], True)


def _probe() -> source_probes.Probe:
    return asyncio.run(source_probes.probe("linear", CONFIG, KEY + "\n"))


def test_the_probe_reads_one_page_of_each_project(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, _tracker)
    found = _probe()
    assert found.documents == 3 and found.more
    assert found.sample == ["Newer", "Other", "Older"]
    assert {str(r.url) for r in seen} == {"https://api.linear.app/graphql"}
    # The key as it is (trimmed), no bearer prefix; ids only as variables.
    assert {r.headers["authorization"] for r in seen} == {KEY}
    bodies = [json.loads(r.content) for r in seen]
    assert sorted(b["variables"]["id"] for b in bodies) == sorted(CONFIG["project_ids"])
    assert all(PROJECT not in b["query"] for b in bodies)


def _error(status: int, message: str, **extensions: Any) -> httpx.Response:
    errors = [{"message": message, "extensions": extensions}]
    return httpx.Response(status, json={"errors": errors, "data": None})


@pytest.mark.parametrize(
    "answer, raised, text",
    [
        (
            _error(400, "Entity not found", type="invalid input"),
            source_probes.SourceUnreachable,
            "not_found; the key's account must see the project",
        ),
        (
            _error(400, SECRET_TEXT, type="authentication error"),
            source_probes.CredentialRefused,
            "credential_refused",
        ),
        (_error(401, SECRET_TEXT), source_probes.CredentialRefused, "credential_refused"),
        (
            _error(400, SECRET_TEXT, code="RATELIMITED"),
            source_probes.SourceUnreachable,
            "rate_limited",
        ),
        (_error(200, SECRET_TEXT, type="forbidden"), source_probes.SourceUnreachable, "forbidden"),
        (httpx.Response(503, text=SECRET_TEXT), source_probes.SourceUnreachable, "http_503"),
        (httpx.Response(200, text=SECRET_TEXT), source_probes.SourceUnreachable, "no_data"),
    ],
)
def test_failures_are_told_apart_and_carry_no_body(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response, raised: type, text: str
) -> None:
    _install(monkeypatch, lambda request: answer)
    with pytest.raises(raised) as e:
        _probe()
    assert text in str(e.value) and SECRET_TEXT not in str(e.value)


def test_a_key_that_cannot_be_a_header_is_refused_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, _tracker)
    with pytest.raises(source_probes.CredentialRefused):
        asyncio.run(source_probes.probe("linear", CONFIG, "two words"))
    assert seen == []


def test_a_document_id_that_is_not_the_tracker_s_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, _tracker)
    with pytest.raises(ValueError):
        asyncio.run(linear.document(KEY, "1 OR 1", max_bytes=10))
    assert seen == []


def test_a_rate_limited_error_is_typed_whatever_its_status() -> None:
    for status in (200, 400):
        with pytest.raises(linear.RateLimited):
            linear._raise_for(status, {"errors": [{"extensions": {"type": "ratelimited"}}]}, "x")


def test_a_config_names_projects_by_id() -> None:
    upper = OTHER.upper()
    assert parse_config("linear", {"project_ids": [PROJECT, upper, f" {PROJECT} "]}) == {
        "project_ids": [PROJECT, OTHER]
    }


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"project_ids": []},
        {"project_ids": PROJECT},
        {"project_ids": ["ENG-12"]},
        {"project_ids": [[PROJECT]]},
        {"project_ids": [PROJECT], "host": "h"},
        {"project_ids": [f"00000000-0000-4000-8000-{n:012d}" for n in range(21)]},
    ],
)
def test_a_config_the_kind_cannot_use_is_refused(config: dict[str, Any]) -> None:
    with pytest.raises(SourceConfigError):
        parse_config("linear", config)
