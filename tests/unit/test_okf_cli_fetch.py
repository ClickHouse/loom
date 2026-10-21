"""`loom export-okf` reads the rows the bundle needs: superseded rows stay in
the export so the supersedes chain has both
ends; the typed edges are paged through memory.export_evidence kind=edges."""

from __future__ import annotations

from typing import Any

import pytest

from server.cli import okf as cli_okf

_DELETED = "2026-09-01T00:00:00"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, body))
        if op == "memory.keys":
            keys = [
                {"agent": "a", "user_id": "-", "session_id": "-", "memory_key": k}
                for k in ("new", "old")
            ]
            return {"keys": keys, "truncated": False}
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
    rows, unreadable, why, _ = await cli_okf._fetch_namespace(
        client, "o", "ns", include_tombstoned=False
    )  # type: ignore[arg-type]
    assert unreadable == [] and why == ""
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
        complete=True,
    )
    newer = okf.parse_concept("semantic/new.md", files["semantic/new.md"])
    assert newer.relates_to == [{"path": "semantic/old.md", "kind": "supersedes"}]
    assert orphans == [] and skipped == 0
    assert datetime.now(UTC).date().isoformat() in files["log.md"]


def _entries(keys: list[str], **over: Any) -> list[dict[str, Any]]:
    return [
        {"memory_key": k, "agent": "x", "user_id": "-", "session_id": "-", **over} for k in keys
    ]


def _keys(keys: list[str]) -> dict[str, Any]:
    return {"keys": _entries(keys), "truncated": False}


class _ShiftingClient:
    """Pages of one row. On the first `shifts` listings "a" is deleted after the first page
    is read, so the next offset serves "c" and skips "b"; memory.keys then reads b and c."""

    def __init__(self, shifts: int) -> None:
        self.shifts = shifts
        self.listings = 0
        self.live = ["a", "b", "c"]

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.bulk_get":
            return {"results": {k: {"value": k, "sensitivity": ""} for k in body["memory_keys"]}}
        if op == "memory.keys":
            return _keys(self.live)
        assert op == "memory.list"
        if body["offset"] == 0:
            self.listings += 1
            self.live = ["a", "b", "c"]
        elif self.listings <= self.shifts:
            self.live = ["b", "c"]
        page = self.live[body["offset"] : body["offset"] + 1]
        return {"entries": _entries(page), "truncated": body["offset"] + 1 < len(self.live)}


@pytest.mark.asyncio
async def test_a_listing_that_skipped_a_row_is_read_again() -> None:
    client = _ShiftingClient(shifts=1)
    rows, _, why, _ = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert why == "" and client.listings == 2
    assert [r["memory_key"] for r in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_namespace_that_keeps_shifting_is_not_called_complete() -> None:
    # The bundle then carries no completeness marker: it is written, and not pruned.
    client = _ShiftingClient(shifts=99)
    rows, _, why, _ = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert why == cli_okf._SHIFTED and client.listings == cli_okf._LIST_ATTEMPTS
    assert [r["memory_key"] for r in rows] == ["a", "c"]


class _LateCommitClient:
    """Two pages of two rows. A write whose version was drawn before the newest one lands
    between the pages and sorts first, so the second page repeats "b" and "n" is never
    listed; no version or total moves. memory.keys reads it."""

    def __init__(self) -> None:
        self.listings = 0

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.bulk_get":
            return {"results": {k: {"value": k, "sensitivity": ""} for k in body["memory_keys"]}}
        if op == "memory.keys":
            return _keys(["a", "b", "c", "n"])
        if body["offset"] == 0:
            self.listings += 1
        late = self.listings > 1 or body["offset"] > 0
        live = ["n", "a", "b", "c"] if late else ["a", "b", "c"]
        page = live[body["offset"] : body["offset"] + 2]
        return {"entries": _entries(page), "truncated": body["offset"] + 2 < len(live)}


@pytest.mark.asyncio
async def test_a_row_that_commits_late_below_the_newest_version_is_caught() -> None:
    client = _LateCommitClient()
    rows, _, why, _ = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert why == "" and client.listings == 2
    assert sorted(r["memory_key"] for r in rows) == ["a", "b", "c", "n"]


@pytest.mark.asyncio
async def test_a_source_without_memory_keys_is_listed_once_and_not_called_complete() -> None:
    calls: list[str] = []

    class _Older(_ShiftingClient):
        async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append(op)
            if op == "memory.keys":
                raise RuntimeError("memory.keys → HTTP 404 Not Found")
            return await super().post(op, body)

    rows, _, why, _ = await cli_okf._fetch_namespace(_Older(0), "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert "did not answer memory.keys" in why and calls.count("memory.keys") == 1
    assert [r["memory_key"] for r in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_namespace_larger_than_one_keys_answer_is_not_called_complete() -> None:
    class _Large(_ShiftingClient):
        async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
            if op == "memory.keys":
                return {**_keys(["a", "b", "c"]), "truncated": True}
            return await super().post(op, body)

    _rows, _, why, _ = await cli_okf._fetch_namespace(
        _Large(0), "o", "ns", include_tombstoned=False
    )  # type: ignore[arg-type]
    assert "more live rows than one memory.keys answer" in why


class _DeletedBeforeTheFullRead:
    """One page of rows, every key listed; `gone` was deleted before bulk_get, and
    `old` was already deleted (listed only with include_tombstoned). bulk_get never answers
    a tombstoned row."""

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.bulk_get":
            keys = [k for k in body["memory_keys"] if k not in ("gone", "old")]
            return {"results": {k: {"value": k, "sensitivity": ""} for k in keys}}
        if op == "memory.keys":
            return _keys(["kept", "gone"])
        keys = ["kept", "gone", "old"] if body.get("include_tombstoned") else ["kept", "gone"]
        entries = [
            {**e, "value": "cut…", "tombstoned_at": _DELETED if e["memory_key"] == "old" else None}
            for e in _entries(keys)
        ]
        return {"entries": entries, "truncated": False}


@pytest.mark.asyncio
async def test_a_row_deleted_before_its_full_read_is_left_out_and_the_bundle_not_complete() -> None:
    # Its list preview is a cut value, and its absence says nothing about the rows around it.
    rows, unreadable, why, _ = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _DeletedBeforeTheFullRead(), "o", "ns", include_tombstoned=False
    )
    assert [r["memory_key"] for r in rows] == ["kept"] and unreadable == ["gone"]
    assert why == "a listed row was deleted before its full read"


@pytest.mark.asyncio
async def test_include_tombstoned_keeps_a_deleted_row_but_not_one_deleted_mid_export() -> None:
    rows, unreadable, _, _ = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _DeletedBeforeTheFullRead(), "o", "ns", include_tombstoned=True
    )
    assert sorted(r["memory_key"] for r in rows) == ["kept", "old"] and unreadable == ["gone"]


@pytest.mark.asyncio
async def test_a_limit_the_namespace_never_reached_is_listed_again_like_a_whole_export() -> None:
    # --limit 100 on a three-row namespace read every row: a shifted listing is retried.
    client = _ShiftingClient(shifts=1)
    rows, _, why, capped = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        client, "o", "ns", include_tombstoned=False, limit=100
    )
    assert why == "" and not capped and client.listings == 2
    assert [r["memory_key"] for r in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_repeated_row_never_counts_toward_the_limit() -> None:
    # Three rows under --limit 4, paged two at a time: the late row "n" makes the second
    # page serve "b" again, and the repeat must not make the listing look cut.
    rows, _, why, capped = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _LateCommitClient(), "o", "ns", include_tombstoned=False, limit=4
    )
    assert not capped and why == ""
    assert sorted(r["memory_key"] for r in rows) == ["a", "b", "c", "n"]


@pytest.mark.asyncio
async def test_an_empty_namespace_is_read_whole() -> None:
    calls: list[str] = []

    class _Empty:
        async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append(op)
            if op == "memory.keys":
                return _keys([])
            return {"entries": [], "truncated": False}

    rows, unreadable, why, capped = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _Empty(), "o", "ns", include_tombstoned=False
    )
    assert (rows, unreadable, why, capped) == ([], [], "", False)
    assert calls == ["memory.list", "memory.keys"]


@pytest.mark.asyncio
async def test_an_include_tombstoned_export_lists_the_namespace_once() -> None:
    # It is never marked complete, so a shifting source is not listed again for nothing.
    client = _ShiftingClient(shifts=99)
    await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=True)  # type: ignore[arg-type]
    assert client.listings == 1


class _MixedClient:
    """A namespace with three live rows (plain, one both reads call high, one only the list
    calls high) and three tombstoned rows, which bulk_get does not answer: one short, one
    longer than the list preview, one 'high'."""

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
                    entry("gone-short", tombstoned_at=_DELETED, sensitivity=""),
                    entry(
                        "gone-long", tombstoned_at=_DELETED, sensitivity="", value_truncated=True
                    ),
                    # memory.list sends a high row without its value
                    entry(
                        "gone-high",
                        tombstoned_at=_DELETED,
                        sensitivity="high",
                        value=None,
                        value_truncated=True,
                    ),
                ],
                "truncated": False,
            }
        if op == "memory.keys":
            return {"keys": [entry(k) for k in ("plain", "split", "secret")], "truncated": False}
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
    rows, unreadable, _, _ = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
    )
    assert unreadable == ["gone-long"]
    files, _orphans, skipped = cli_okf._plan_export(
        rows, [], [], [], namespace="ns", title=None, include_high=False, complete=False
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

    async def __aenter__(self) -> _OlderEvidenceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
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

    rows, *_ = await cli_okf._fetch_namespace(
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
    _rows, unreadable, _, _ = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
        include_high=True,
    )
    assert "gone-high" in unreadable


@pytest.mark.asyncio
async def test_a_tombstoned_high_row_is_counted_as_high_not_named_without_include_high() -> None:
    rows, unreadable, _, _ = await cli_okf._fetch_namespace(
        _MixedClient(),  # type: ignore[arg-type]
        "o",
        "ns",
        include_tombstoned=True,
    )
    assert "gone-high" not in unreadable
    from server.memory.domain import okf_bundle

    held = [r["memory_key"] for r in rows if not okf_bundle.is_exportable(r)]
    assert "gone-high" in held


class _AliasClient:
    """A stored key shaped like a chunk id, deleted between the list and bulk_get: bulk_get
    answers it with its parent's row (`canonical_key` names the parent)."""

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        key = "chunk:memory.set:x:0"
        if op == "memory.list":
            row = {"memory_key": key, "agent": "a", "user_id": "-", "session_id": "-"}
            return {
                "entries": [{**row, "value": "cut…", "value_truncated": True}],
                "truncated": False,
            }
        if op == "memory.keys":
            row = {"memory_key": key, "agent": "a", "user_id": "-", "session_id": "-"}
            return {"keys": [row], "truncated": False}
        if op == "memory.bulk_get":
            return {
                "results": {
                    key: {"value": "x's own value", "canonical_key": "x", "sensitivity": ""}
                }
            }
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_export_never_writes_a_parents_value_under_a_chunk_shaped_key() -> None:
    rows, unreadable, _, _ = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _AliasClient(), "o", "ns", include_tombstoned=False
    )
    assert rows == [] and unreadable == ["chunk:memory.set:x:0"]


def test_a_chunk_shaped_rows_own_answer_is_told_apart_from_its_parents() -> None:
    # Another row's answer: `resolved_from`, the parent named in the id as `canonical_key`,
    # or (an older build) a canonical key with another version. `duplicate_of` marks the
    # row's own answer, and a duplicate's canonical alone (another key) says nothing.
    own = {"value": "v", "canonical_key": "notes-v2"}
    assert not cli_okf._answered_by_parent("chunk:memory.set:notes:0", own, None)
    assert cli_okf._answered_by_parent("chunk:memory.set:notes:0", {"canonical_key": "notes"}, None)
    assert cli_okf._answered_by_parent("chunk:memory.set:notes:0", {"resolved_from": "x"}, None)
    assert not cli_okf._answered_by_parent(
        "chunk:memory.set:notes", {"canonical_key": "notes"}, None
    )
    # an older build answering with a duplicate parent: its canonical, and another version
    older = {"value": "v", "canonical_key": "notes-v2", "version": 9}
    assert cli_okf._answered_by_parent("chunk:memory.set:notes:0", older, 5)
    assert not cli_okf._answered_by_parent("chunk:memory.set:notes:0", {**older, "version": 5}, 5)
    # this build's own answer for the row, rewritten since the list: kept
    assert not cli_okf._answered_by_parent("chunk:memory.set:notes:0", {"version": 9}, 5)
    # this build's own answer for a duplicate row (its canonical may even be the "parent"),
    # whether or not it was rewritten since the list: `duplicate_of` says it is the row's own
    duplicate = {"canonical_key": "notes", "duplicate_of": "m-1"}
    assert not cli_okf._answered_by_parent("chunk:memory.set:notes:0", duplicate, None)
    assert not cli_okf._answered_by_parent(
        "chunk:memory.set:notes:0", {**older, "duplicate_of": "m-1"}, 5
    )


class _HeldHighClient:
    """Two live rows: a plain one, and a high one whose retention window has passed (the
    list serves it until the expiry sweep; bulk_get no longer does)."""

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.keys":
            return _keys(["plain", "secret"])
        if op == "memory.list":
            return {
                "entries": [
                    *_entries(["plain"], sensitivity="", value="plain text"),
                    *_entries(
                        ["secret"],
                        sensitivity="high",
                        value=None,
                        value_truncated=True,
                        expires_at="2026-01-01T00:00:00",
                    ),
                ],
                "truncated": False,
            }
        if op == "memory.bulk_get":
            got = {"plain": {"value": "plain text", "sensitivity": ""}}
            return {"results": {k: got[k] for k in body["memory_keys"] if k in got}}
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_a_high_row_kept_home_leaves_the_export_complete() -> None:
    rows, unreadable, why, _ = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _HeldHighClient(), "o", "ns", include_tombstoned=False
    )
    assert why == "" and unreadable == []
    assert {r["memory_key"] for r in rows} == {"plain", "secret"}  # listed, kept home


@pytest.mark.asyncio
async def test_a_high_row_past_its_retention_leaves_an_include_high_export_complete() -> None:
    # bulk_get will never serve it again: it has left the source, which the bundle says.
    rows, unreadable, why, _ = await cli_okf._fetch_namespace(  # type: ignore[arg-type]
        _HeldHighClient(), "o", "ns", include_tombstoned=False, include_high=True
    )
    assert why == "" and unreadable == []
    assert [r["memory_key"] for r in rows] == ["plain"]
