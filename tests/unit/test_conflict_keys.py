"""The conflict chips read keys, not rows.

The Memories view and the memory inspector mark conflicted cards using only
the memory keys; a `conflicts.list` of up to 500 rows with their values
would read megabytes for a few kB of answer on a namespace of large values.
`keys_only` answers the keys from one FINAL read of the key columns."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import server.memory.app.conflicts_ops as conflicts_mod
from server.memory.store import conflicts as conflicts_repo


class _Fake:
    def __init__(self) -> None:
        self.keys_calls = 0
        self.rows_calls = 0

    async def conflicted_keys(self, org: str, namespace: str, **kw: Any) -> list[str]:
        self.keys_calls += 1
        return ["pref/tea", "pref/coffee"]

    async def list_conflicted(self, org: str, namespace: str, **kw: Any) -> list[dict[str, Any]]:
        self.rows_calls += 1
        return []


@pytest.mark.asyncio
async def test_keys_only_answers_the_keys_and_reads_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_conflicts import ConflictsListInput

    fake = _Fake()
    monkeypatch.setattr(conflicts_repo, "conflicted_keys", fake.conflicted_keys)
    monkeypatch.setattr(conflicts_repo, "list_conflicted", fake.list_conflicted)
    out = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team", keys_only=True)
    )
    assert out.keys == ["pref/tea", "pref/coffee"] and out.rows == [] and out.total is None
    assert (fake.keys_calls, fake.rows_calls) == (1, 0)


@pytest.mark.asyncio
async def test_the_row_listing_is_unchanged_without_keys_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_conflicts import ConflictsListInput

    fake = _Fake()
    monkeypatch.setattr(conflicts_repo, "conflicted_keys", fake.conflicted_keys)
    monkeypatch.setattr(conflicts_repo, "list_conflicted", fake.list_conflicted)
    out = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team")
    )
    assert out.keys is None and out.rows == [] and fake.rows_calls == 1 and fake.keys_calls == 0


def test_the_keys_statement_selects_no_value_column() -> None:
    text = inspect.getsource(conflicts_repo)
    start = text.index("async def conflicted_keys(")
    src = text[start : text.index("\n\n\nasync def", start)]
    sql = src.split('"""')[3]
    assert "SELECT memory_key" in sql and "FINAL" in sql and "value" not in sql


def test_keys_only_is_hidden_from_the_mcp_listing() -> None:
    from server.transport.mcp_schemas import _strip_blob_fields_from_knowledge

    schema = {
        "properties": {"namespace": {"type": "string"}, "keys_only": {"type": "boolean"}},
        "required": [],
    }
    out = _strip_blob_fields_from_knowledge("conflicts.list", schema)
    assert "keys_only" not in out["properties"] and "namespace" in out["properties"]


@pytest.mark.asyncio
async def test_a_projected_conflicts_list_still_says_why_a_value_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A high row is listed without its value; `sensitivity` rides every projection, as on
    # memory.list, so a caller can tell that from a value that failed to load.
    from datetime import datetime

    from server.memory.api.models_conflicts import ConflictsListInput

    async def _rows(org: str, namespace: str, **kw: Any) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": "00000000-0000-0000-0000-000000000001",
                "memory_key": "vault",
                "memory_type": "semantic",
                "value": None,
                "version": 1,
                "pinned": False,
                "created_at": datetime(2026, 9, 1),
                "conflict_with": [],
                "sensitivity": "high",
            }
        ]

    monkeypatch.setattr(conflicts_repo, "list_conflicted", _rows)
    out = await conflicts_mod.handle_conflicts_list(
        ConflictsListInput(org="acme", namespace="team", fields=["memory_key", "value"])
    )
    assert out.model_dump()["rows"] == [
        {"memory_key": "vault", "value": None, "sensitivity": "high"}
    ]
