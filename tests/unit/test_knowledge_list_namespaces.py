"""knowledge.list over several namespaces: the cursor of each form, and the read check."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.auth import authz
from server.core.cursors import encode_cursor
from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app import document_reads
from server.knowledge.store import documents as documents_repo

AT = datetime(2026, 5, 17, 12, 0, 0)


def _row(namespace: str, uri: str) -> Any:
    return {"ingested_at": AT, "namespace": namespace, "source_uri": uri}


def test_a_one_namespace_cursor_keeps_its_wire_format() -> None:
    cursor = document_reads._list_cursor(_row("ns", "doc://a"), "acme", ["ns"])
    assert cursor == encode_cursor(AT, "doc://a")
    assert document_reads._list_position(cursor, "acme", ["ns"]) == (AT, "ns", "doc://a")


def test_several_namespaces_read_back_their_own_cursor() -> None:
    cursor = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    # The namespaces' order does not matter, only which they are.
    assert document_reads._list_position(cursor, "acme", ["b", "a"]) == (AT, "b", "doc://a")


@pytest.mark.parametrize(
    ("org", "asked"),
    [("acme", ["a", "c"]), ("other", ["a", "b"]), ("acme", ["a"])],
)
def test_a_several_namespace_cursor_is_refused_elsewhere(org: str, asked: list[str]) -> None:
    cursor = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, org, asked)


def test_a_one_namespace_cursor_is_refused_for_several() -> None:
    cursor = document_reads._list_cursor(_row("a", "doc://a"), "acme", ["a"])
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, "acme", ["a", "b"])


@pytest.mark.parametrize("namespaces", [["a"], ["a", "b"]])
@pytest.mark.parametrize("cursor", ["not base64!", "w6k="])
def test_a_cursor_it_did_not_issue_is_refused(namespaces: list[str], cursor: str) -> None:
    with pytest.raises(ValidationError):
        document_reads._list_position(cursor, "acme", namespaces)


def test_a_several_namespace_cursor_with_a_byte_changed_is_refused() -> None:
    good = document_reads._list_cursor(_row("b", "doc://a"), "acme", ["a", "b"])
    tampered = good[:-3] + ("A" if good[-3] != "A" else "B") + good[-2:]
    with pytest.raises(ValidationError):
        document_reads._list_position(tampered, "acme", ["a", "b"])


async def test_knowledge_list_refuses_a_namespace_the_caller_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def check(auth: Any, role: Any, *, org: str, namespace: str, **_: Any) -> None:
        raise PermissionError(f"cannot access the collection {org}/{namespace}")

    monkeypatch.setattr(authz, "check_authorized", check)
    with pytest.raises(AuthInsufficientError):
        await document_reads.handle_knowledge_list(
            KnowledgeListInput(org="acme", namespace="a", namespaces=["col-secret"])
        )


async def test_each_row_names_its_namespace_whatever_fields_are_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def page(org: str, namespaces: list[str], **_: Any) -> list[dict[str, Any]]:
        return [
            {
                "namespace": "b",
                "source_uri": "doc://a",
                "title": "A",
                "tags": [],
                "ingested_at": AT,
                "chunk_count": 1,
                "superseded_at": None,
            }
        ]

    async def named(org: str, namespace: str, names: list[str]) -> list[str]:
        return [namespace, *names]

    monkeypatch.setattr(documents_repo, "list_with_chunk_counts", page)
    monkeypatch.setattr(document_reads, "named_namespaces", named)
    out = await document_reads.handle_knowledge_list(
        KnowledgeListInput(org="acme", namespace="a", namespaces=["b"], fields=["title"])
    )
    assert out.model_dump(mode="json")["rows"] == [
        {"namespace": "b", "source_uri": "doc://a", "title": "A"}
    ]
