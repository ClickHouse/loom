"""memory.for_file — memories anchored to a file, rendered for a pre-edit hook.

Both stored shapes (bare filename, repo-relative path) match either request
shape; constraints sort first; the rendered block respects the token budget
and says how many it left out; every caller value is a bound parameter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_for_file import MemoryForFileInput
from server.memory.app import for_file as mff


class _CH:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sql = sql
        self.params = params or {}
        return self.rows


def _row(key: str, value: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_id": f"id-{key}",
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
        "tags": [],
        "confidence": 1.0,
        "pinned": 0,
        "status": "active",
        "agent": "claude-code",
        "updated_at": datetime(2026, 9, 11, 12, 0),
        "matched_files": ["src/a.cpp"],
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_path_and_basename_are_bound_and_both_shapes_are_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([])
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o", namespace="ns", path="./src/Storages/MergeTreeData.cpp", tags=["project:ch"]
        )
    )
    assert out.total == 0 and out.rendered == "" and out.hits == []
    assert ch.params["p"] == "src/Storages/MergeTreeData.cpp"
    assert ch.params["b"] == "MergeTreeData.cpp"
    assert ch.params["tags"] == ["project:ch"] and ch.params["lim"] == 8
    assert ch.params["statuses"] == ["active", "conflicted"]
    # bare stored name vs repo-relative request, and the reverse
    assert "endsWith({p:String}, concat('/', f))" in ch.sql
    assert "endsWith(f, concat('/', {b:String}))" in ch.sql
    # the files[] facet is the one route to a file
    assert "arrayExists(f ->" in ch.sql
    # constraints first
    assert "ORDER BY has(tags, 'constraint') DESC, pinned DESC, confidence DESC" in ch.sql
    # no interpolation of caller values into SQL text
    assert "MergeTreeData" not in ch.sql and "project:ch" not in ch.sql
    # an expired row is never served, and a credential is never pushed into a prompt
    assert "(expires_at IS NULL OR expires_at > now())" in ch.sql
    assert "sensitivity != 'high'" in ch.sql


@pytest.mark.asyncio
async def test_hits_carry_their_matched_files_and_render_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(
            "never_allocate_here",
            "Do not call allocate() in this file; the arena owns it.",
            tags=["constraint", "memory"],
            pinned=1,
        ),
        _row("mergetree_data_history", "x" * 900, matched_files=["a.cpp"]),
        _row("third", "Third fact about the file.", matched_files=["MergeTreeData.cpp"]),
    ]
    ch = _CH(rows)
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", token_budget=90)
    )
    assert out.total == 3
    assert out.hits[0].matched_files == ["src/a.cpp"]
    assert out.hits[1].matched_files == ["a.cpp"] and out.hits[1].pinned is False
    assert out.hits[2].matched_files == ["MergeTreeData.cpp"]
    assert out.rendered.startswith("Loom: 3 memories anchored to src/a.cpp")
    assert "- never_allocate_here [constraint] (pinned): Do not call allocate()" in out.rendered
    assert out.truncated is True
    assert 'more; memory.for_file(path="src/a.cpp") lists them.' in out.rendered
    assert out.tokens_estimate <= 90 + 30  # the tail line may run a little past the cap
    # The hit carries the stored value whole; only the rendered block is cut.
    assert out.hits[1].value == "x" * 900
    assert all(len(line) <= mff._LINE_CHARS + 80 for line in out.rendered.splitlines())


def test_render_block_folds_a_value_to_one_capped_line() -> None:
    value = "first line\nsecond line " + "y" * 600
    text, _, truncated = mff.render_block("a.py", [_row("long", value)], 10_000)
    (line,) = text.splitlines()[1:]
    assert truncated is False
    assert line.startswith("- long: first line second line ")
    assert len(line) <= len("- long: ") + mff._LINE_CHARS


def test_render_block_singular_and_empty() -> None:
    text, tokens, truncated = mff.render_block("x.py", [], 300)
    assert (text, tokens, truncated) == ("", 0, False)
    text, tokens, truncated = mff.render_block(
        "x.py", [{"memory_key": "k", "value": "v", "tags": [], "pinned": False}], 300
    )
    assert text == "Loom: 1 memory anchored to x.py — read before editing:\n- k: v"
    assert truncated is False and tokens == (len(text) + 3) // 4


def test_registered_read_only_and_callable_by_name() -> None:
    from server.operations import toolsets
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_descriptions

    op = OPERATIONS_BY_NAME["memory.for_file"]
    assert op.required_role.name == "VIEWER" and op.idempotent and op.audit is False
    assert "memory.for_file" not in toolsets._MCP_HIDDEN_OPS
    assert (
        "before editing" in mcp_descriptions.TOOL_DESCRIPTIONS["memory.for_file"].lower()
        or "before an edit" in mcp_descriptions.TOOL_DESCRIPTIONS["memory.for_file"].lower()
    )


@pytest.mark.asyncio
async def test_a_hot_files_links_are_bound_in_slices_and_the_best_of_all_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    linked = {str(uuid.UUID(int=i)): ["fresh"] for i in range(5000)}
    bound: list[list[str]] = []

    class _Sliced:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            part = (params or {})["linked"]
            bound.append(part)
            # each slice's own best, one stronger the later the slice
            return [_row(f"k{len(bound)}", "v", memory_id=part[0], confidence=len(bound) / 10)]

    monkeypatch.setattr(mff, "get_client", lambda: _Sliced())
    request = MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", top_k=2)
    rows = await mff._rows_for(request, "ns", "src/a.cpp", "a.cpp", [], linked)
    assert len(bound) > 1 and sorted(m for part in bound for m in part) == sorted(linked)
    assert [r["memory_key"] for r in rows] == [f"k{len(bound)}", f"k{len(bound) - 1}"]


@pytest.mark.asyncio
async def test_a_link_read_that_fails_leaves_the_file_match_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads as app_reads
    from server.memory.store import code_links as store_links

    async def one_link(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"memory_id": "m", "code_namespace": "code-a", "state": "fresh"}]

    async def down(*a: Any, **k: Any) -> Any:
        raise ConnectionError("clickhouse")

    monkeypatch.setattr(store_links, "for_code", one_link)
    monkeypatch.setattr(app_reads, "current_links", down)
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", lambda org, ns: "code-a")
    request = MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", extra_namespaces=["x"])
    assert await mff._linked(request, "src/a.cpp", ["ns"]) == {}
