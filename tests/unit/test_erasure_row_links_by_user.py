"""A user's erasure reaches the graph's row-link tables by the writer each link
carries, which outlives the row: a link of an erased user's row is found by its own
user_id, not through a memory key that may be gone."""

from __future__ import annotations

from typing import Any

import pytest

from server.erasure.store import steps_graph


async def test_a_users_row_links_are_deleted_by_the_user_they_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletes: list[tuple[str, str, dict[str, Any]]] = []

    async def delete_scoped(table: str, *, org: str, scope_where: str, params: Any, ch: Any) -> int:
        deletes.append((table, scope_where, params))
        return 0

    async def user_key_pairs(org: str, user_id: str, *, ch: Any) -> list[tuple[str, str]]:
        return [("ns", "k")]

    monkeypatch.setattr(steps_graph, "delete_scoped", delete_scoped)
    monkeypatch.setattr(steps_graph, "user_key_pairs", user_key_pairs)
    await steps_graph.delete_graph_links_by_user("o", "u1", ch=object())
    by_table = {t: (where, params) for t, where, params in deletes}
    for table in ("loom.entity_triple_rows", "loom.entity_edge_rows"):
        assert by_table[table] == ("user_id = {uid:String}", {"uid": "u1"}), table
    for table in ("loom.entity_triple_memories", "loom.entity_edge_memories"):
        where, params = by_table[table]
        assert "{keys:Array(Tuple(String, String))}" in where and params == {"keys": [("ns", "k")]}
