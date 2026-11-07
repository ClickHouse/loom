"""What a request against a restricted namespace leaves behind: text-free
argument projection, one audit row per admitted request,
and nothing at all for a standard namespace."""

from __future__ import annotations

import asyncio

import pytest

import server.identity.app.namespace_classification as nc
import server.identity.app.restricted_access as ra
from server.auth.jwt import AuthInfo
from server.observability.event_args import redact_args
from server.operations.base import Role

ORG, NS = "clickhouse", "code-example-private"


@pytest.fixture(autouse=True)
def _snapshot():
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({NS}))
    yield
    nc.reset()


def test_redact_args_drops_text_keeps_identifiers() -> None:
    raw = {
        "org": ORG,
        "namespace": NS,
        "query": "secret keeper path",
        "path": "src/a.cpp",
        "memory_key": "k",
        "question": "why?",
    }
    out = redact_args(raw, ORG, NS)
    assert "query" not in out and "question" not in out
    assert out["path"] == "src/a.cpp" and out["memory_key"] == "k"
    assert out["_restricted"] == 1


def test_redact_args_is_identity_for_standard_namespace() -> None:
    raw = {"org": ORG, "namespace": "code-clickhouse", "query": "q"}
    assert redact_args(raw, ORG, "code-clickhouse") is raw
    assert redact_args(None, ORG, NS) is None
    assert redact_args(raw, "", "") is raw


def test_redact_args_redacts_when_no_directory_is_wired() -> None:
    from server.auth import directory
    from server.identity.app.directory import IdentityDirectory

    raw = {"org": ORG, "namespace": "code-clickhouse", "query": "q", "path": "a"}
    directory.use_directory(None)
    try:
        assert redact_args(raw, ORG, "code-clickhouse") == {
            "org": ORG,
            "namespace": "code-clickhouse",
            "path": "a",
            "_restricted": 1,
        }
    finally:
        directory.use_directory(IdentityDirectory())


def test_digest_text_is_stable_and_labelled() -> None:
    a, b = ra.digest_text("SELECT 1"), ra.digest_text("SELECT 1")
    assert a == b and a.startswith("sha256:") and len(a) == 7 + 64


@pytest.mark.asyncio
async def test_note_restricted_access_writes_one_row(monkeypatch) -> None:
    rows = []

    async def _emit(event_type, **kw):
        await asyncio.sleep(0.05)  # a ClickHouse insert takes time
        rows.append((event_type, kw))
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    from server.settings import settings

    monkeypatch.setattr(settings, "restricted_audit_inline", False)
    auth = AuthInfo(
        subject="sa:clickgap",
        org=ORG,
        namespaces=(NS,),
        roles=(Role.VIEWER,),
        is_service_account=True,
        token_id="jti-1",
    )
    assert await ra.note_restricted_access(
        auth, op="code.search", org=ORG, namespace=NS, transport="http"
    )
    # The request does not wait for the insert: no row yet...
    assert rows == []
    # ...and the row still lands, with the same content
    assert await ra.drain() == 1
    ((event, kw),) = rows
    assert event == "namespace.restricted_access"
    assert kw["org"] == ORG and kw["namespace"] == NS
    assert kw["actor"] == "process:sa:clickgap"
    assert kw["subject"] == {
        "op": "code.search",
        "transport": "http",
        "token_id": "jti-1",
        "roles": ["viewer"],
    }


@pytest.mark.asyncio
async def test_note_restricted_access_is_silent_for_standard_or_anonymous(monkeypatch) -> None:
    async def _boom(event_type, **kw):
        raise AssertionError("no audit row expected")

    monkeypatch.setattr("server.observability.audit.emit", _boom)
    auth = AuthInfo(
        subject="a@x",
        org=ORG,
        namespaces=("personal",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    assert not await ra.note_restricted_access(
        auth, op="memory.search", org=ORG, namespace="personal", transport="http"
    )
    assert not await ra.note_restricted_access(
        None, op="code.search", org=ORG, namespace=NS, transport="http"
    )


@pytest.mark.asyncio
async def test_note_restricted_access_never_raises(monkeypatch) -> None:
    async def _fail(event_type, **kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("server.observability.audit.emit", _fail)
    from server.settings import settings

    monkeypatch.setattr(settings, "restricted_audit_inline", False)
    auth = AuthInfo(
        subject="a@x", org=ORG, namespaces=(NS,), roles=(Role.VIEWER,), is_service_account=False
    )
    # scheduled beside the request; the write's failure stays in its task
    assert (
        await ra.note_restricted_access(
            auth, op="code.search", org=ORG, namespace=NS, transport="mcp"
        )
        is True
    )
    assert await ra.drain() == 1  # swallowed, logged; nothing raised
    # the inline mode surfaces the same failure as "no row", never a raise
    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    assert (
        await ra.note_restricted_access(
            auth, op="code.search", org=ORG, namespace=NS, transport="mcp"
        )
        is False
    )


@pytest.mark.asyncio
async def test_inline_mode_commits_the_row_before_answering(monkeypatch) -> None:
    """`restricted_audit_inline` (the default) waits for the audit insert, so a
    killed process never loses a row for a read it answered."""
    rows = []

    async def _emit(event_type, **kw):
        await asyncio.sleep(0.02)
        rows.append(event_type)
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    from server.settings import settings

    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    auth = AuthInfo(
        subject="a@x", org=ORG, namespaces=(NS,), roles=(Role.VIEWER,), is_service_account=False
    )
    assert await ra.note_restricted_access(
        auth, op="code.search", org=ORG, namespace=NS, transport="http"
    )
    assert rows == ["namespace.restricted_access"] and await ra.drain() == 0


def test_audit_rows_are_inline_by_default() -> None:
    from server.settings import Settings

    assert Settings.model_fields["restricted_audit_inline"].default is True
