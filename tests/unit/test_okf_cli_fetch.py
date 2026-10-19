"""`loom export-okf` reads the rows the bundle needs: superseded rows stay in
the export so the supersedes chain has both
ends; the typed edges are paged through memory.export_evidence kind=edges."""

from __future__ import annotations

from typing import Any

import pytest

from server.cli import okf as cli_okf


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, body))
        if op == "memory.list":
            return {
                "entries": [
                    {
                        "memory_key": "new",
                        "agent": "a",
                        "user_id": "-",
                        "session_id": "-",
                        "status": "active",
                    },
                    {
                        "memory_key": "old",
                        "agent": "a",
                        "user_id": "-",
                        "session_id": "-",
                        "status": "superseded",
                    },
                ],
                "truncated": False,
            }
        if op == "memory.bulk_get":
            return {
                "results": {
                    "new": {
                        "value": "v2",
                        "memory_id": "m-new",
                        "source_class": "asserted",
                        "sensitivity": "",
                    },
                    "old": {
                        "value": "v1",
                        "memory_id": "m-old",
                        "sensitivity": "",
                        "status": "superseded",
                        "superseded_by": "m-new",
                        "source_class": "extracted",
                    },
                }
            }
        if op == "memory.export_evidence" and body.get("kind") == "edges":
            if not body.get("cursor"):
                return {
                    "edges": [
                        {
                            "from_key": "new",
                            "to_key": "old",
                            "relation": "supersedes",
                            "confidence": 1.0,
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "c1",
                }
            return {
                "edges": [
                    {"from_key": "old", "to_key": "new", "relation": "precedes", "confidence": 1.0}
                ],
                "has_more": False,
                "next_cursor": "",
            }
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_export_keeps_superseded_rows_and_pages_edges() -> None:
    client = _FakeClient()
    rows, unreadable = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert unreadable == []
    list_call = next(b for op, b in client.calls if op == "memory.list")
    assert list_call["include_superseded"] is True
    assert sorted(r["memory_key"] for r in rows) == ["new", "old"]
    old = next(r for r in rows if r["memory_key"] == "old")
    assert old["superseded_by"] == "m-new" and old["source_class"] == "extracted"

    edges = await cli_okf._fetch_edges(client, "o", "ns")  # type: ignore[arg-type]
    assert [e["relation"] for e in edges] == ["supersedes", "precedes"]
    cursors = [b.get("cursor") for op, b in client.calls if op == "memory.export_evidence"]
    assert cursors == ["", "c1"]

    from datetime import UTC, datetime

    from server.memory.domain import okf_bundle as okf

    files, orphans, skipped = cli_okf._plan_export(
        rows,
        edges,
        [],
        [],
        namespace="ns",
        title=None,
        include_high=False,
    )
    newer = okf.parse_concept("semantic/new.md", files["semantic/new.md"])
    assert newer.relates_to == [{"path": "semantic/old.md", "kind": "supersedes"}]
    assert orphans == [] and skipped == 0
    assert datetime.now(UTC).date().isoformat() in files["log.md"]


class _MixedClient:
    """A namespace with a live row, a live 'high' row and three tombstoned rows, which
    bulk_get does not answer: one short, one longer than the list preview, one 'high'."""

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        def entry(key: str, **over: Any) -> dict[str, Any]:
            base = {"memory_key": key, "agent": "a", "user_id": "-", "session_id": "-"}
            return {**base, "memory_type": "semantic", "value": f"{key} text", **over}

        if op == "memory.list":
            return {
                "entries": [
                    entry("plain", sensitivity=""),
                    # The list reached a build that reads it as high, bulk_get one that does not.
                    entry("split", sensitivity="high"),
                    entry("secret", sensitivity="high"),
                    entry("gone-short", status="tombstoned", sensitivity=""),
                    entry("gone-long", status="tombstoned", sensitivity="", value_truncated=True),
                    # memory.list sends a high row without its value
                    entry(
                        "gone-high",
                        status="tombstoned",
                        sensitivity="high",
                        value=None,
                        value_truncated=True,
                    ),
                ],
                "truncated": False,
            }
        if op == "memory.bulk_get":
            return {
                "results": {
                    "plain": {"value": "plain text", "sensitivity": ""},
                    "split": {"value": "split text", "sensitivity": ""},
                    "secret": {"value": "secret text", "sensitivity": "high"},
                }
            }
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_export_keeps_high_and_unreadable_rows_home() -> None:
    rows, unreadable = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
    )
    assert unreadable == ["gone-long"]
    files, _orphans, skipped = cli_okf._plan_export(
        rows, [], [], [], namespace="ns", title=None, include_high=False
    )
    text = "\n".join(files.values())
    assert skipped == 3  # secret, split and gone-high
    assert "plain text" in text and "gone-short text" in text
    assert "secret text" not in text and "gone-high text" not in text and "split text" not in text


class _OlderEvidenceClient(_MixedClient):
    """A server that sends every chunk, without holding back those behind a 'high' row."""

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.export_evidence" and body["kind"] == "chunks":
            chunk = {"session_id": "s", "content": "", "memory_keys": []}
            return {
                "chunks": [
                    {
                        **chunk,
                        "chunk_index": 0,
                        "content": "said in passing",
                        "memory_keys": ["plain"],
                    },
                    {
                        **chunk,
                        "chunk_index": 1,
                        "content": "the vault rotates",
                        "memory_keys": ["secret"],
                    },
                ],
                "has_more": False,
            }
        if op == "memory.export_evidence":
            return {"digests": [], "edges": [], "has_more": False}
        return await super().post(op, body)

    async def close(self) -> None:
        pass


def test_export_keeps_the_evidence_of_a_held_row_home(tmp_path, monkeypatch, capsys) -> None:
    import argparse

    monkeypatch.setattr(cli_okf, "_Client", lambda *_a: _OlderEvidenceClient())
    monkeypatch.setattr(cli_okf, "_server_build", lambda _url: "")
    parser = argparse.ArgumentParser()
    cli_okf.register_okf_cli(parser.add_subparsers())
    out = tmp_path / "b"
    args = parser.parse_args(
        ["export-okf", "--token", "t", "--org", "o", "--namespace", "ns", "--out", str(out)]
    )
    assert int(args.func(args)) == 0
    evidence = "\n".join(p.read_text() for p in (out / "evidence").rglob("*.md"))
    assert "said in passing" in evidence and "the vault rotates" not in evidence
    assert "1 raw chunk(s) or session digest(s) stayed in the database" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_export_never_reads_the_value_of_a_row_the_list_calls_high() -> None:
    asked: list[str] = []

    class _Client(_MixedClient):
        async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if op == "memory.bulk_get":
                asked.extend(body["memory_keys"])
            return await super().post(op, body)

    rows, _ = await cli_okf._fetch_namespace(
        _Client(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
    )
    assert "secret" not in asked and "split" not in asked and "plain" in asked
    assert {r["memory_key"] for r in rows} >= {"secret", "split"}  # listed, kept home


@pytest.mark.asyncio
async def test_include_high_cannot_export_a_tombstoned_high_row() -> None:
    # bulk_get does not answer a tombstoned row and the list sends no high value, so even
    # --include-high has nothing to write for it: it is named, not exported empty.
    _rows, unreadable = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
        include_high=True,
    )
    assert "gone-high" in unreadable


@pytest.mark.asyncio
async def test_a_tombstoned_high_row_is_counted_as_high_not_named_without_include_high() -> None:
    rows, unreadable = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
    )
    assert "gone-high" not in unreadable
    from server.memory.domain import okf_bundle

    held = [r["memory_key"] for r in rows if not okf_bundle.is_exportable(r)]
    assert "gone-high" in held
