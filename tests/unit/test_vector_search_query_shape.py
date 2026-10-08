"""Guards the query shape CH's vector-search rewrite requires.

ClickHouse can only push `ORDER BY <distance> LIMIT n` into a
`vector_similarity` index (or, on 26.7+, into a `Quantized(...)` codec's
two-stage scan) when the plan matches exactly

    LimitStep -> SortingStep -> ExpressionStep -> [FilterStep] -> ReadFromMergeTree

and the sort has a single key, holding a *bare* distance call, ASC. Anything
else — `1.0 - cosineDistance(...) AS sim ... ORDER BY sim DESC`, a second sort
key, DESC on a distance — silently falls back to a full brute-force scan. There
is no error and no log line, so only a test catches the regression.

These assertions are deliberately textual: the point is to fail when someone
edits the SQL, not to re-derive CH's optimizer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.core.vectors.search_settings import vector_search_settings
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]
MEMORY_SEARCH_FILES = sorted(
    p.relative_to(ROOT).as_posix() for p in (ROOT / "server/memory/app/retrieval").glob("*.py")
)

# Files whose vector-ranked SQL must keep the eligible shape, and the sort keys
# that are allowed to appear in them.
_EXPECTED_ORDER_BY = {
    "server/knowledge/app/conversation_search.py": ["ORDER BY distance ASC"],
    "server/knowledge/app/search.py": ["ORDER BY distance ASC"],
    # _ann_entity_boosts keeps `ORDER BY distance ASC` on its outer stage, but
    # its inner stage GROUPs BY entity_text to rank distinct entities rather
    # than edges — which forfeits the rewrite by design. See the comment there;
    # the table is one namespace's entity edges, too small to warrant an index.
    "server/memory/app/search/ann_entity_boosts.py": ["ORDER BY distance ASC"],
}


def _read(path: str) -> str:
    """Source with comment lines stripped.

    The modules explain the disqualifying shapes in prose (`ORDER BY sim DESC`
    among them), so scanning raw text would match the warning rather than a
    real sort key. Drop Python (`#`) and SQL (`--`) comment lines first.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    lines = (root / path).read_text().splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith(("#", "--")))


@pytest.mark.parametrize("path", sorted(_EXPECTED_ORDER_BY))
def test_vector_queries_order_by_bare_distance_asc(path: str) -> None:
    src = _read(path)
    for expected in _EXPECTED_ORDER_BY[path]:
        assert expected in src, f"{path} lost its bare-distance ORDER BY"
    # No similarity-flavoured sort keys: `1.0 - cosineDistance(...)` as the
    # ORDER BY expression is the exact shape that disqualifies the rewrite.
    assert "ORDER BY sim DESC" not in src, f"{path} reintroduced ORDER BY sim DESC"


@pytest.mark.parametrize(
    "path",
    [
        *MEMORY_SEARCH_FILES,
        "server/knowledge/app/conversation_search.py",
        "server/knowledge/app/search.py",
        "server/memory/app/search/ann_entity_boosts.py",
    ],
)
def test_no_similarity_expression_as_sort_key(path: str) -> None:
    """`ORDER BY sim DESC` must not come back anywhere in the search path."""
    assert "ORDER BY sim DESC" not in _read(path)


def test_no_query_pins_ef_search_below_the_clickhouse_default() -> None:
    """No helper plane pins ef_search=64 — a quarter of CH's 256.

    They are the legs that exist to catch what the main leg missed, so trading
    their recall for latency is backwards. Every ef_search comes from
    settings; nothing may hardcode a literal below the CH default.
    """
    literals: list[tuple[str, int]] = []
    for path in (
        *MEMORY_SEARCH_FILES,
        "server/knowledge/app/conversation_search.py",
        "server/knowledge/app/search.py",
        "server/memory/app/search/ann_entity_boosts.py",
        "server/memory/domain/search_sql.py",
    ):
        for m in re.finditer(r"hnsw_candidate_list_size_for_search\s*=\s*(\d+)", _read(path)):
            literals.append((path, int(m.group(1))))
    bad = [(p, v) for p, v in literals if v < 256]
    assert not bad, f"ef_search pinned below the CH default of 256: {bad}"


def test_vector_search_settings_fragment_carries_both_knobs() -> None:
    frag = vector_search_settings()
    assert frag.startswith("SETTINGS ")
    assert f"hnsw_candidate_list_size_for_search = {settings.vector_search_ef_search}" in frag
    # Over-fetch inside the index. At CH's default of 1, a selective tenant
    # predicate post-filters top-N down to fewer than N returned rows.
    assert "vector_search_index_fetch_multiplier = " in frag
    assert settings.vector_search_index_fetch_multiplier > 1.0


def test_vector_search_settings_overrides_are_honoured() -> None:
    frag = vector_search_settings(ef_search=512, fetch_multiplier=3.0)
    assert "hnsw_candidate_list_size_for_search = 512" in frag
    assert "vector_search_index_fetch_multiplier = 3.0" in frag
