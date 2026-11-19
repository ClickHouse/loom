"""Checked downloads revalidate before headers and every emitted chunk."""

from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from server.knowledge.app import download_routes as app
from server.knowledge.app import source_access, source_documents
from server.knowledge.app.source_access import Admitted
from server.knowledge.store import blobs, documents
from server.knowledge.store.documents import DocumentRow

SID = "11111111-1111-4111-8111-111111111111"
PUBLIC_URI = "gdrive:file#native"
MIRROR = "acl-" + "1" * 24
URL = "/v1/knowledge.download"


@pytest.fixture
def world(monkeypatch):
    state = {
        "allowed": True,
        "opened": 0,
        "closed": 0,
        "during_read": "",
        "data": b"First private section. Second private section. Last section.",
    }
    plan = SimpleNamespace(
        org="acme", marked=True, failed=False, unread=False, current=lambda _item: state["allowed"]
    )
    row = DocumentRow(
        org="acme",
        doc_id=SID,
        namespace=MIRROR,
        source_uri=PUBLIC_URI + "#" + SID,
        title="",
        content="Private text.",
        version=10,
        revision=0,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    checked = source_documents.CheckedDocument(
        Admitted("team", {"namespace": MIRROR, "source_uri": row.source_uri}, {}), row
    )
    state["checked"] = checked
    phases = []

    async def prepare(*_args):
        phases.append("prepare")
        return plan

    async def ordinary(*_args, **_kwargs):
        phases.append("ordinary")
        return None

    async def refresh(*_args):
        return state["checked"] if state["allowed"] else None

    @asynccontextmanager
    async def stream(org, namespace, sha, **_kwargs):
        assert (org, namespace, sha) == ("acme", MIRROR, "a" * 64)
        state["opened"] += 1

        async def chunks():
            if state["during_read"] == "cancel":
                raise asyncio.CancelledError
            if state["during_read"] == "error":
                raise RuntimeError("private backend secret")
            if state["during_read"] == "revoke":
                state["allowed"] = False
            for start in range(0, len(state["data"]), 16):
                if start and state["during_read"] == "late_io":
                    raise OSError("private storage secret")
                yield state["data"][start : start + 16]

        try:
            yield chunks()
        finally:
            state["closed"] += 1
            if state.get("close_error"):
                raise RuntimeError("private close secret")

    monkeypatch.setattr(app.settings, "auth_required", False)
    monkeypatch.setattr(app, "check_acted_for", AsyncMock())
    monkeypatch.setattr(app, "check_authorized", AsyncMock())
    monkeypatch.setattr(app, "note_restricted_access", AsyncMock())
    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(documents, "get_one", AsyncMock(side_effect=ordinary))
    monkeypatch.setattr(source_documents, "get", AsyncMock(return_value=checked))
    monkeypatch.setattr(source_documents, "refresh", AsyncMock(side_effect=refresh))
    monkeypatch.setattr(
        blobs, "locate", AsyncMock(side_effect=lambda *_args: blobs.Stored(len(state["data"]), ""))
    )
    monkeypatch.setattr(
        blobs, "read", AsyncMock(side_effect=AssertionError("Mirror blobs must stream"))
    )
    monkeypatch.setattr(blobs, "stream", stream)
    return state, plan, checked, phases


def _blob(world):
    state, _plan, checked, _phases = world
    selected = replace(checked, row=checked.row.model_copy(update={"blob_path": "a" * 64}))
    state["checked"] = selected
    source_documents.get.return_value = selected


@pytest.fixture
def client(world):
    from server.main import app as application

    return TestClient(application, base_url="http://localhost")


def params():
    return {"org": "acme", "namespace": "team", "source_uri": PUBLIC_URI}


async def response(spec="2.4"):
    scope = {
        "type": "http",
        "method": "GET",
        "path": URL,
        "headers": [],
        "asgi": {"spec_version": spec},
    }
    return scope, await app.knowledge_download(Request(scope), **params())


async def receive():
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


def test_checked_text_download_prepares_before_content_and_projects_filename(world, client):
    _state, _plan, checked, phases = world
    result = client.get(URL, params=params())
    assert result.status_code == 200 and result.text == checked.row.content
    assert result.headers["x-loom-download-source"] == "text"
    assert SID not in result.headers["content-disposition"]
    assert phases[:2] == ["prepare", "ordinary"]
    source_documents.refresh.assert_awaited()


def test_ordinary_document_remains_the_complete_download_copy(world, client):
    _state, _plan, checked, phases = world
    documents.get_one.side_effect = None
    documents.get_one.return_value = checked.row.model_copy(
        update={"namespace": "team", "source_uri": PUBLIC_URI, "content": "Ordinary text."}
    )
    result = client.get(URL, params=params())
    assert result.status_code == 200 and result.text == "Ordinary text."
    assert phases == ["prepare"]
    source_documents.get.assert_not_awaited()
    source_documents.refresh.assert_not_awaited()


def test_checked_blob_uses_admitted_namespace_and_closes(world, client):
    state, _plan, checked, _phases = world
    _blob(world)
    result = client.get(URL, params=params(), headers={"Accept-Encoding": "identity"})
    assert result.status_code == 200 and result.content == state["data"]
    assert state["opened"] == state["closed"] == 1
    assert result.headers["content-length"] == str(len(state["data"]))
    assert SID not in result.headers["content-disposition"]
    blobs.locate.assert_awaited_once_with("acme", MIRROR, "a" * 64)
    blobs.read.assert_not_awaited()


@pytest.mark.parametrize("change", ["locate", "revoke", "error"])
def test_preheader_blob_loss_has_the_complete_missing_reply_and_closes(world, client, change):
    state, _plan, checked, _phases = world
    source_documents.get.return_value = None
    missing = client.get(URL, params=params())
    source_documents.get.return_value = checked
    _blob(world)
    if change == "locate":

        async def locate(*_args):
            state["allowed"] = False
            return blobs.Stored(len(state["data"]), "")

        blobs.locate.side_effect = locate
    else:
        state["during_read"] = change
    result = client.get(URL, params=params())
    assert result.status_code == missing.status_code == 404
    assert result.json() == missing.json()
    assert "private backend" not in result.text
    blobs.locate.assert_awaited_once()
    assert state["opened"] == state["closed"] == (0 if change == "locate" else 1)


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("when", ["headers", "first_chunk"])
async def test_revocation_during_send_prevents_the_next_body_chunk(world, when, spec):
    state, _plan, checked, _phases = world
    _blob(world)
    scope, answer = await response(spec)
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start" and when == "headers":
            state["allowed"] = False
        elif (
            message["type"] == "http.response.body"
            and message.get("body")
            and when == "first_chunk"
        ):
            state["allowed"] = False

    with pytest.raises(RuntimeError, match="document read unavailable"):
        await answer(scope, receive, send)
    payload = b"".join(message.get("body", b"") for message in sent)
    assert payload == (b"" if when == "headers" else state["data"][:16])
    assert state["opened"] == state["closed"] == 1


async def test_cancellation_during_prefetch_closes_the_open_stream(world):
    state, plan, checked, _phases = world
    _blob(world)
    state["during_read"] = "cancel"
    scope, answer = await response()
    send = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await answer(scope, receive, send)
    assert state["opened"] == state["closed"] == 1
    assert not plan.failed
    send.assert_not_awaited()


async def test_failed_header_send_closes_a_prefetched_but_unstarted_body(world):
    state, _plan, checked, _phases = world
    _blob(world)
    scope, answer = await response()
    with pytest.raises(ClientDisconnect):
        await answer(scope, receive, AsyncMock(side_effect=OSError("client gone")))
    assert state["opened"] == state["closed"] == 1


def test_preheader_cleanup_failure_does_not_escape_private_details(world, client):
    state, _plan, _checked, _phases = world
    source_documents.get.return_value = None
    missing = client.get(URL, params=params())
    _blob(world)
    state.update(during_read="revoke", close_error=True)
    result = client.get(URL, params=params())
    assert result.status_code == 404 and result.json() == missing.json()
    assert state["opened"] == state["closed"] == 1


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_body_io_failure_does_not_escape_private_traceback_context(world, spec):
    state, _plan, _checked, _phases = world
    _blob(world)
    state["during_read"] = "late_io"
    scope, answer = await response(spec)
    with pytest.raises((ClientDisconnect, RuntimeError)) as error:
        await answer(scope, receive, AsyncMock())
    rendered = "".join(traceback.format_exception(error.value))
    assert "private storage secret" not in rendered
    assert state["opened"] == state["closed"] == 1
