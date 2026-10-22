"""Guards the query shape CH's vector-search rewrite requires.

ClickHouse can only push `ORDER BY <distance> LIMIT n` into a
`Quantized(...)` codec's two-stage scan (26.7+) when the plan matches exactly

    LimitStep -> SortingStep -> ExpressionStep -> [FilterStep] -> ReadFromMergeTree

and the sort has a single key, holding a *bare* distance call, ASC. Anything
else — `1.0 - cosineDistance(...) AS sim ... ORDER BY sim DESC`, a second sort
key, DESC on a distance — silently falls back to a full brute-force scan. There
is no error and no log line, so only a test catches the regression.

These assertions are deliberately textual: the point is to fail when someone
edits the SQL, not to re-derive CH's optimizer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_no_read_sends_a_vector_index_setting() -> None:
    """No table has a vector-similarity index, so no read carries the index's
    search settings."""
    for path in sorted((ROOT / "server").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "hnsw_candidate_list_size_for_search" not in text, path
