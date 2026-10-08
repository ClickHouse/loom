"""Liveness under argMax must not skip NULL.

`argMax(tombstoned_at, version)` ignores rows whose `tombstoned_at` is NULL,
so for a key that was tombstoned once and re-inserted live afterwards it
returns the OLD tombstone and the key reads as dead, hiding live rows from
the ingest key-diff and from the audit. The code plane reads liveness as
`argMax(tombstoned_at IS NULL, version) = 1`, the memory plane's idiom.
This guard keeps the NULL-skipping form out of the code plane's sources.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CODE_REPO = sorted((ROOT / "server/code/store/index").glob("*.py"))
_CODE_PLANE = (
    *(str(p.relative_to(ROOT)) for p in _CODE_REPO),
    "server/code/app/index/packs.py",
    "server/code/app/index/dependents.py",
    "scripts/ops/code_edge_audit.py",
    "bench/code_dependents_replay.py",
)


def test_code_plane_never_decides_liveness_with_a_null_skipping_argmax() -> None:
    for rel in _CODE_PLANE:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "argMax(tombstoned_at, version)" not in src, rel
        assert "argMax(tombstoned_at, created_at)" not in src, rel


def test_code_repo_liveness_reads_use_the_null_safe_idiom() -> None:
    # Every liveness read in the code_repo package uses the idiom, among them
    # `name_candidates` (over `code_symbol_names`), `live_call_guards_for_path`,
    # `call_guards_for_hops`, `caller_counts`, the round-wide
    # `live_edges_for_paths` / `live_call_misses_for_paths` /
    # `live_call_guards_for_paths`, `has_live_scip_override_edges`, `exact_rerank_by_ids` (the shortlist's live
    # keys, read beside the vectors without FINAL) and `symbol_names_for_paths`
    # (the idiom in a keyed subquery, `WHERE is_live = 1` outside) and
    # `live_callers_of_symbols` (a merged PR's context pack).
    src = "".join(p.read_text(encoding="utf-8") for p in _CODE_REPO)
    assert src.count("argMax(tombstoned_at IS NULL, version) AS is_live") == 20
    assert src.count("HAVING is_live = 1") == 19
