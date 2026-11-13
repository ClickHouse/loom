"""knowledge.list over several namespaces: the cursor of each form, and the read check."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.core.cursors import encode_cursor
from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.app import document_reads, readable

AT = datetime(2026, 5, 17, 12, 0, 0)


def _row(namespace: str, uri: str) -> Any:
    return {"ingested_at": AT, "namespace": namespace, "source_uri": uri}


def test_one_namespace_keeps_the_cursor_a_build_a_release_behind_reads() -> None:
    cursor = document_reads._list_cursor(_row("ns", "doc://a"), "acme", ["ns"])
    assert cursor == encode_cursor(AT, "doc://a")
    assert document_reads._list_position(cursor, "acme", ["ns"]) == (AT, "ns", "doc://a")


def test_several_namespaces_read_back_their_own_cursor() -> None:
    cursor = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    # The places' order does not matter, only which they are.
    assert document_reads._list_position(cursor, "acme", ["b", "a"]) == (AT, "b", "doc://a")


@pytest.mark.parametrize(
    ("org", "asked"),
    [("acme", ["a", "c"]), ("other", ["a", "b"]), ("acme", ["a"])],
)
def test_a_several_place_cursor_is_refused_elsewhere(org: str, asked: list[str]) -> None:
    cursor = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, org, asked)


def test_a_one_place_cursor_is_refused_for_several() -> None:
    cursor = document_reads._list_cursor(_row("a", "doc://a"), "acme", ["a"])
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, "acme", ["a", "b"])


async def test_a_place_the_caller_cannot_read_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import authz

    async def check(auth: Any, role: Any, *, org: str, namespace: str, **_: Any) -> None:
        if namespace == "col-secret":
            raise PermissionError("cannot access the collection acme/col-secret")

    monkeypatch.setattr(authz, "check_authorized", check)
    assert await readable.named_namespaces("acme", "a", ["b", "a"]) == ["a", "b"]
    with pytest.raises(AuthInsufficientError):
        await readable.named_namespaces("acme", "a", ["b", "col-secret"])


def test_a_several_namespace_cursor_names_its_namespace_under_a_mac() -> None:
    import base64

    cursor = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    mac, ts, extra = base64.urlsafe_b64decode(cursor).decode().split("|", 2)
    assert len(mac) == 32 and ts == AT.isoformat() and extra == '["b", "doc://a"]'


@pytest.mark.parametrize("namespaces", [["a"], ["a", "b"]])
@pytest.mark.parametrize("cursor", ["not base64!", "", "w6k=", None])
def test_a_cursor_it_did_not_issue_is_refused(namespaces: list[str], cursor: str | None) -> None:
    if cursor is None:  # a several-namespace cursor with one byte changed
        good = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
        cursor = good[:-3] + ("A" if good[-3] != "A" else "B") + good[-2:]
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, "acme", namespaces)


async def test_knowledge_list_refuses_a_namespace_the_caller_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz
    from server.knowledge.api.models_knowledge import KnowledgeListInput

    async def check(auth: Any, role: Any, *, org: str, namespace: str, **_: Any) -> None:
        raise PermissionError(f"cannot access the collection {org}/{namespace}")

    monkeypatch.setattr(authz, "check_authorized", check)
    with pytest.raises(AuthInsufficientError):
        await document_reads.handle_knowledge_list(
            KnowledgeListInput(org="acme", namespace="a", namespaces=["col-secret"])
        )
