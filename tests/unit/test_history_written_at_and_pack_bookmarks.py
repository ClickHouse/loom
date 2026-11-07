"""A history row says when its version was written; an
intended edit stamps `updated_at`; an MCP context.pack serves no project's
session bookmark unless it names its own."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from server.memory.app import curation as cur
from server.memory.domain import context_layout as context_layout


def test_a_version_is_read_back_as_its_write_time() -> None:
    # 1790308951974427015 ns = 2026-09-25 04:02:31.974 UTC (a real row's version)
    t = cur._version_time(1790308951974427015)
    assert t == datetime(2026, 9, 25, 4, 2, 31, 974427)
    assert cur._version_time("1790308951974427015") == t
    for bad in (None, "x", 0, 12345, 10**20):
        assert cur._version_time(bad) is None


def test_history_rows_carry_written_at() -> None:
    from server.memory.api.models_temporal import _MemoryHistoryRow

    assert "written_at" in _MemoryHistoryRow.model_fields
    src = inspect.getsource(cur.handle_memory_history)
    assert "written_at=_version_time(" in src


def test_history_rows_carry_their_version_exactly_as_text() -> None:
    # A version is above 2^53, where a JavaScript caller's JSON.parse rounds the number;
    # memory.redact takes the string back.
    from server.memory.api.models_temporal import _MemoryHistoryRow

    version = 2**60 + 1
    row = _MemoryHistoryRow(
        memory_id="m", version=version, confidence=1.0, created_at=datetime(2000, 1, 1)
    )
    assert row.version_text == str(version)
    assert float(version) != version


def test_written_now_is_utc_naive_now() -> None:
    from server.memory.store import written_now

    now = datetime.now(UTC).replace(tzinfo=None)
    assert abs(written_now() - now) < timedelta(seconds=5)
    assert written_now().tzinfo is None


def test_every_intended_edit_stamps_updated_at() -> None:
    """The builder carries updated_at forward (right for maintenance); each
    write a caller meant — update, lock, unlock, tombstone, restore, apply
    label/demote — overrides it."""
    import server.memory.app.apply as app
    import server.memory.app.handlers as mem
    import server.memory.app.lock as lock
    import server.memory.app.restore as restore
    import server.memory.app.update as update
    import server.memory.app.write as write

    stamp = "updated_at=memory_repo.written_now()"
    assert inspect.getsource(lock).count(stamp) >= 2  # lock + unlock rows
    msrc = (
        inspect.getsource(mem)
        + inspect.getsource(write)
        + inspect.getsource(update)
        + inspect.getsource(restore)
    )
    restored = 'status=status,\n        superseded_by="",'
    for marker in ("tombstoned_at=now,", restored, "run_id=revision_run_id,"):
        i = msrc.index(marker)
        assert stamp in msrc[max(0, i - 200) : i], marker
    assert inspect.getsource(app).count("updated_at=memory_repo.written_now()") == 2


def _blocks(*keys: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(block_key=k) for k in keys]


def test_a_dash_session_block_serves_no_bookmark() -> None:
    keys = ["session:loom", "session:example-private", "loomcheck"]
    assert [b.block_key for b in _blocks(*keys) if context_layout._own_bookmark(b, "-")] == [
        "loomcheck"
    ]
    assert [
        b.block_key for b in _blocks(*keys) if context_layout._own_bookmark(b, "session:loom")
    ] == [
        "session:loom",
        "loomcheck",
    ]
    assert [b.block_key for b in _blocks(*keys) if context_layout._own_bookmark(b, "")] == keys


def test_mcp_context_pack_defaults_to_no_bookmark_but_keeps_a_named_one() -> None:
    import server.transport.mcp_call as mcp_call

    args: dict = {}
    mcp_call._scope_arguments("context.pack", args, {"agent": "claude-code"}, False)
    assert args["session_block"] == "-"
    named = {"session_block": "session:loom"}
    mcp_call._scope_arguments("context.pack", named, {"agent": "claude-code"}, False)
    assert named["session_block"] == "session:loom"
    other: dict = {}
    mcp_call._scope_arguments("memory.get", other, {"agent": "claude-code"}, False)
    assert "session_block" not in other
    assert re.search(r'op_name == "context.pack"', inspect.getsource(mcp_call._scope_arguments))
