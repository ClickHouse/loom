"""The per-file edge read has a key that starts with the file.

loom.code_edges carries a projection ordered by
(org, namespace, tier, path, line) so `live_edges_for_path` reads the granules
of one path instead of the whole (org, namespace, tier) range. These tests pin
what keeps that true: the projection's key and rebuild modes in the schema
dump, and the read's predicate staying a prefix of the
projection key (a read that adds an earlier column, or drops one, would fall
back to the base table and a ~51 MB-per-call scan).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.schema_ddl import ddl

ROOT = Path(__file__).resolve().parent.parent.parent
CODE_EDGES = ROOT / "server" / "code" / "store" / "index" / "edges.py"

PROJECTION_KEY = ("org", "namespace", "tier", "path", "line")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_schema_dump_carries_the_projection_and_settings() -> None:
    squashed = _squash(ddl("code_edges"))
    # ReplacingMergeTree refuses a projection unless a deduplicating merge
    # and a lightweight delete are told what to do with it.
    assert "PROJECTION by_path ( SELECT * ORDER BY org, namespace, tier, path, line )" in squashed
    assert "deduplicate_merge_projection_mode = 'rebuild'" in squashed
    assert "lightweight_mutation_projection_mode = 'rebuild'" in squashed
    # The base key is unchanged: the callers/callees walk still reads by symbol id.
    assert (
        "ORDER BY (org, namespace, tier, to_symbol_id, edge_kind, from_symbol_id, path, line)"
        in squashed
    )


def test_per_path_edge_read_predicate_is_a_prefix_of_the_projection_key() -> None:
    src = CODE_EDGES.read_text()
    start = src.index("async def live_edges_for_path(")
    body = src[start : src.index("async def ", start + 10)]
    where = _squash(body.split("WHERE", 1)[1].split("GROUP BY", 1)[0])
    cols = set(
        re.findall(r"\b(org|namespace|tier|path|line|to_symbol_id|from_symbol_id)\s*=", where)
    )
    # Exactly the first four projection-key columns are pinned by equality;
    # nothing outside the key sneaks in and nothing in the prefix is missing.
    assert cols == set(PROJECTION_KEY[:4]), cols
    sql = body.split('"""', 3)[2] if body.count('"""') >= 3 else body
    assert re.search(r"code_edges\s+FINAL", sql) is None, (
        "a FINAL read cannot be served by a projection"
    )
