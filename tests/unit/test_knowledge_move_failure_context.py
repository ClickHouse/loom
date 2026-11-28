"""Catalog-copy failures report send metadata without changing their outcome."""

from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core.ch import CHClient
from server.core.errors import BackendUnavailableError
from tests.integration import test_knowledge_move as native

CASE = native.test_a_retry_onto_a_copy_with_its_chunks_and_tables_gone_copies_it_again


class UnreadableFailure(Exception):
    def __str__(self) -> str:
        raise ValueError("private formatter")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "stored", "kind", "code"),
    [
        (
            DatabaseError("Code: 395. private-query"),
            frozenset({"org", "private-column"}),
            "DatabaseError",
            395,
        ),
        (TimeoutError("private-host"), frozenset(), "TimeoutError", None),
        (ValueError("private-content"), None, "other", None),
        (UnreadableFailure(), frozenset({"org"}), "other", None),
        (
            DatabaseError("Code: 999999999. private-query"),
            frozenset({"org"}),
            "DatabaseError",
            None,
        ),
    ],
)
@pytest.mark.parametrize("translated", [False, True])
async def test_catalog_failure_keeps_exception_identity_and_only_fixed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    stored: frozenset[str] | None,
    kind: str,
    code: int | None,
    translated: bool,
) -> None:
    outward = BackendUnavailableError("public refusal") if translated else error
    state = _install(monkeypatch, error=error, outward=outward, stored=stored)
    with pytest.raises(type(outward)) as caught:
        await CASE("http://fixture.test", state.client, state.move, monkeypatch)
    assert caught.value is outward
    (note,) = caught.value.__notes__
    assert note.startswith("catalog send failure: ")
    assert json.loads(note.removeprefix("catalog send failure: ")) == {
        "kind": kind,
        "code": code,
        "stored_count": None if stored is None else len(stored),
        "stored_fingerprint": None
        if stored is None
        else hashlib.sha256("\n".join(sorted(stored)).encode()).hexdigest()[:16],
    }
    assert len(note) < 200
    assert "private" not in note
    assert state.calls == [
        "agent-enter",
        "add",
        "agent-exit",
        "write-table",
        "live",
        "move",
        "send",
    ]
    assert CHClient._fenced_insert is state.original_send


@pytest.mark.asyncio
async def test_success_keeps_the_cutoff_and_retry_checks_without_extra_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch)
    await CASE("http://fixture.test", state.client, state.move, monkeypatch)
    assert state.calls == [
        "agent-enter",
        "add",
        "agent-exit",
        "write-table",
        "live",
        "move",
        "send",
        "delete",
        "delete",
        "delete",
        "live-rows",
        "move",
        "send",
        "live",
        "live-rows",
        "rows",
    ]
    assert CHClient._fenced_insert is state.original_send


@pytest.mark.asyncio
async def test_a_failure_before_the_catalog_send_gains_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ValueError("private pre-send failure")
    state = _install(monkeypatch, error=error, before_send=True)
    with pytest.raises(ValueError) as caught:
        await CASE("http://fixture.test", state.client, state.move, monkeypatch)
    assert caught.value is error
    assert not getattr(error, "__notes__", [])
    assert state.calls[-1] == "move"


@pytest.mark.asyncio
async def test_another_tables_failure_gains_no_catalog_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ValueError("private other-table failure")
    state = _install(monkeypatch, error=error, table="loom.documents")
    with pytest.raises(ValueError) as caught:
        await CASE("http://fixture.test", state.client, state.move, monkeypatch)
    assert caught.value is error
    assert not getattr(error, "__notes__", [])


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
    outward: Exception | None = None,
    stored: frozenset[str] | None = frozenset({"org"}),
    before_send: bool = False,
    table: str = "loom.datasets",
) -> SimpleNamespace:
    calls: list[str] = []
    src, dst, uri = "fixture-source", "fixture-target", "doc://fixture"
    sender, data, columns, types, settings = (
        object(),
        [["private-data"]],
        ["private-column"],
        ["String"],
        {"private-token": "secret"},
    )
    args = (table, data, columns, types, stored, settings, 123.5)
    result = object()

    async def send(self: Any, *forwarded: Any) -> Any:
        assert self is sender
        assert len(forwarded) == len(args)
        assert all(actual is expected for actual, expected in zip(forwarded, args, strict=True))
        calls.append("send")
        if error is not None:
            raise error
        return result

    async def moved(move: Any, source: str, target: str, source_uri: str) -> list[dict[str, Any]]:
        assert (source, target, source_uri) == (src, dst, uri)
        calls.append("move")
        if before_send:
            assert error is not None
            raise error
        try:
            got = await CHClient._fenced_insert(sender, *args)
        except Exception as caught:  # Check identity before simulating translation.
            assert caught is error
            if outward is not None:
                raise outward from None
            raise
        assert got is result
        if calls.count("move") == 1:
            await move._delete_source(None)
        return [{"source_uri": uri, "moved": True, "reason": ""}]

    class Agent:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs == {
                "org": native.ORG,
                "namespace": src,
                "agent": "w",
                "base_url": "http://fixture.test",
            }
            self.knowledge = SimpleNamespace(add=self.add)

        async def add(self, **kwargs: Any) -> None:
            assert kwargs == {
                "content": "region,total\nnorth,3",
                "source_uri": uri,
                "title": "sales",
            }
            calls.append("add")

        async def __aenter__(self) -> Agent:
            calls.append("agent-enter")
            return self

        async def __aexit__(self, *_: Any) -> None:
            calls.append("agent-exit")

    class Client:
        def command(self, sql: str, *, parameters: dict[str, str]) -> None:
            assert sql == (
                f"DELETE FROM {('loom.chunks', 'loom.datasets', 'loom.dataset_rows')[calls.count('delete')]} "
                "WHERE org = {org:String} AND namespace = {ns:String} SETTINGS lightweight_deletes_sync = 2"
            )
            assert parameters == {"org": native.ORG, "ns": dst}
            calls.append("delete")

        def query(self, sql: str, *, parameters: dict[str, str]) -> Any:
            assert sql == (
                "SELECT s1 FROM loom.dataset_rows WHERE org = {org:String} AND namespace = {ns:String} ORDER BY row_id"
            )
            assert parameters == {"org": native.ORG, "ns": dst}
            calls.append("rows")
            return SimpleNamespace(result_rows=[("north",), ("south",)])

    def live(client: Any, namespace: str, source_uri: str) -> list[str]:
        assert client is instance and source_uri == uri
        assert namespace == (src if calls.count("move") == 0 else dst)
        calls.append("live")
        return ["fixture-chunk"]

    def live_rows(client: Any, namespace: str, source_uri: str) -> int:
        assert client is instance and source_uri == uri
        calls.append("live-rows")
        return 1 if namespace == dst else 0

    def write(client: Any, namespace: str, source_uri: str) -> None:
        assert client is instance and (namespace, source_uri) == (src, uri)
        calls.append("write-table")

    instance = Client()
    move = SimpleNamespace(_delete_source=None)
    monkeypatch.setitem(sys.modules, "loom", SimpleNamespace(Agent=Agent))
    monkeypatch.setattr(CHClient, "_fenced_insert", send)
    monkeypatch.setattr(native, "_places", lambda: (src, dst, uri))
    monkeypatch.setattr(native, "_write_table", write)
    monkeypatch.setattr(native, "_live", live)
    monkeypatch.setattr(native, "_live_rows", live_rows)
    monkeypatch.setattr(native, "_move_here", moved)
    return SimpleNamespace(calls=calls, client=instance, move=move, original_send=send)
