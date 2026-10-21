"""Every memory-plane reader keeps to the run, and so does the write side.

A run-scope leak has one shape: a leg builds its own WHERE list instead of
sharing the fact leg's `_build_where`, so `run_id` never reaches it. This
lint finds every function in the two search modules whose SQL reads
loom.memory_embeddings, loom.memory_chunks or loom.session_summaries and
requires it to (a) mention `run_id`, (b) build its predicates through
`_build_where`, or (c) be on the explicit allow-list below with a reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    *sorted((ROOT / "server" / "memory" / "app" / "retrieval").glob("*.py")),
    *sorted((ROOT / "server" / "memory" / "app" / "search").glob("*.py")),
    ROOT / "server" / "memory" / "app" / "search_tag_filter.py",
)
_READ = re.compile(r"FROM\s+loom\.(memory_embeddings|memory_chunks|session_summaries)\b")

# Functions that read a memory-plane table and may legitimately carry no run
# predicate. Every entry needs a reason; a removed function fails the test so
# the list cannot rot.
ALLOW: dict[str, str] = {
    # Receive the fact leg's already-built clauses (run_id included by
    # `_build_where`) as arguments; they compose, they do not filter.
    "_fact_leg_sql": "vector_search's SQL phase: receives the `where` list _build_where built there (run included)",
    "key_axis_auto": "corpus-shape probe: key reuse over the whole namespace, no rows read out",
    "_chunk_candidates_sql": "receives the chunk leg's predicates (run included) as a string",
    "_chunk_overlap_join": (
        "the entity-overlap join over the parents' embedding rows: joined onto chunks the "
        "chunk leg's identity terms (run included, from _chunk_identity_pred) already scope"
    ),
    "_chunk_stage2_one_trip_sql": "receives the chunk leg's predicates (run included) as a string; both union branches carry them",
    "key_match_sql": "composes the key-match arm's `where` list built in _rows_for_ns (run included)",
    "key_match_precheck_sql": (
        "a keys-only existence probe (`SELECT 1 … LIMIT 1` over memory_key) "
        "scoped by the same org/namespace identity predicate as the key-match arm it guards; "
        "it returns no row content and only decides whether the run-scoped arm statement is issued"
    ),
    # Point reads by memory identity, not a search window.
    "fetch_current_state_observation": "single-attribute current-state read over a named key set",
    "_apply_mention_boost": "re-reads keys already in the scoped pool to count mentions; serves no new row",
    "read_row_tags": "re-reads pooled / served keys for their `tags` — include_tags and the hard tag belt; serves no new row",
    "_chunk_parent_pred": "builds the chunk leg's parent semi-join predicate — narrows the leg that already carries `c.run_id`; serves no row itself",
    # Statement builders: the predicates arrive as `where_sql`
    # from `_anchor_pool_where` / `_digest_descent_where`, which carry the run.
    "anchor_pool_sql": "renders the anchor pool's statement over a where_sql built by _anchor_pool_where (run predicate there)",
    "digest_descent_sql": "renders the descent's statement over a where_sql built by _digest_descent_where (run predicate there)",
}


def _functions_with_reads(path: Path) -> dict[str, str]:
    src = path.read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            seg = ast.get_source_segment(src, node) or ""
            if _READ.search(seg):
                out[node.name] = seg
    return out


def _all_reads() -> dict[str, tuple[Path, str]]:
    found: dict[str, tuple[Path, str]] = {}
    for m in MODULES:
        for name, seg in _functions_with_reads(m).items():
            found[f"{m.name}:{name}"] = (m, seg)
    return found


def test_build_where_carries_the_run_predicate() -> None:
    src = (ROOT / "server" / "memory" / "domain" / "search_sql.py").read_text()
    assert "e.run_id = {run_id:String}" in src


def test_every_memory_plane_reader_is_run_scoped_or_allow_listed() -> None:
    reads = _all_reads()
    assert reads, "no memory-plane reads found — the regex or module paths are stale"
    leaks = []
    for key, (_m, seg) in sorted(reads.items()):
        name = key.split(":", 1)[1]
        scoped = "run_id" in seg or "_build_where(" in seg
        if not scoped and name not in ALLOW:
            leaks.append(key)
    assert not leaks, (
        f"memory-plane readers with no run predicate and no allow-list reason: {leaks}"
    )


def test_allow_list_names_real_functions() -> None:
    names = {key.split(":", 1)[1] for key in _all_reads()}
    stale = sorted(n for n in ALLOW if n not in names)
    assert not stale, f"allow-list entries no longer read a memory-plane table: {stale}"


# ---------------------------------------------------------------- write side


def test_every_merge_verdict_consults_the_run() -> None:
    """A run's write is the run's row. Every `merge`
    verdict (no insert; another row's id returned) is constructed in ONE
    helper, `merge_or_run_duplicate`, which decides it with the writer's run
    in view; the canonical match calls it, so it cannot drop a run's own write
    as another row's duplicate."""
    # The verdict rule and the check that calls it live in two modules.
    sources = [
        (ROOT / "server" / "memory" / sub / "conflicts.py").read_text() for sub in ("app", "domain")
    ]
    functions = [
        (node, ast.get_source_segment(src, node) or "")
        for src in sources
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    helper = None
    offenders = []
    for node, seg in functions:
        if node.name == "merge_or_run_duplicate":
            helper = seg
        elif 'verdict="merge"' in seg:
            offenders.append(node.name)
    assert helper is not None, "merge_or_run_duplicate is the one place a merge verdict is built"
    assert "new_run_id" in helper, (
        "merge_or_run_duplicate must decide with the writer's run in view"
    )
    assert not offenders, f"merge verdicts constructed outside merge_or_run_duplicate: {offenders}"
    callers = [
        n.name
        for n, seg in functions
        if n.name != "merge_or_run_duplicate" and "merge_or_run_duplicate(" in seg
    ]
    assert {"check_on_commit"} <= set(callers), callers
