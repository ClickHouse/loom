"""knowledge.list over several places: the cursor of each form, and the place check."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.core.cursors import encode_cursor
from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.app import document_reads, places

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
    assert await places.named_namespaces("acme", "a", ["b", "a"]) == ["a", "b"]
    with pytest.raises(AuthInsufficientError):
        await places.named_namespaces("acme", "a", ["b", "col-secret"])
