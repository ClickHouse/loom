"""The callee direction reads through its own key.

loom.code_edges carries a projection ordered by
(org, namespace, tier, from_symbol_id, edge_kind, to_symbol_id, path, line) so
`edges_for_symbols(direction="callees")` reads the granules of its anchors
instead of the bloom-filtered (org, namespace, tier) range (ten anchors on the
live table: 307 MB against 80 MB on the projection). These tests pin what keeps
that true: the projection's key in the schema dump, the callee
read staying a statement the projection can serve (no FINAL, no predicate on a
column ahead of the symbol id), and the graph leg's per-anchor cap.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from tests.unit.schema_ddl import ddl

PROJECTION_KEY = (
    "org",
    "namespace",
    "tier",
    "from_symbol_id",
    "edge_kind",
    "to_symbol_id",
    "path",
    "line",
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_schema_dump_carries_both_projections_and_the_unchanged_base_key() -> None:
    squashed = _squash(ddl("code_edges"))
    assert "PROJECTION by_path ( SELECT * ORDER BY org, namespace, tier, path, line )" in squashed
    assert (
        "PROJECTION by_from ( SELECT * ORDER BY org, namespace, tier, from_symbol_id, "
        "edge_kind, to_symbol_id, path, line )" in squashed
    )
    assert "deduplicate_merge_projection_mode = 'rebuild'" in squashed
    assert (
        "ORDER BY (org, namespace, tier, to_symbol_id, edge_kind, from_symbol_id, path, line)"
        in squashed
    )


class _StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


@pytest.mark.asyncio
async def test_callee_read_is_a_statement_the_by_from_projection_can_serve() -> None:
    """No FINAL (a projection cannot serve one), the tenant pair plus the
    symbol id as the only key predicates — `tier` unbound is fine (two
    values, generic exclusion), a predicate on a column outside the
    projection key is not — and the anchor column is the projection's."""
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.edges_for_symbols(
        "o", "n", ["s1", "s2"], direction="callees", limit=500, per_match=24
    )
    sql, params = ch.calls[0]
    n = _squash(sql)
    assert re.search(r"code_edges\s+FINAL", n) is None
    where = n.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
    cols = set(re.findall(r"\b([a-z_]+)\s*(?:=|IN)\s*\{", where))
    assert cols == {"org", "namespace", "from_symbol_id", "edge_kind"}, cols
    assert set(cols) <= set(PROJECTION_KEY)
    assert "LIMIT {per_match:UInt32} BY from_symbol_id" in n
    assert params["ids"] == ["s1", "s2"]
