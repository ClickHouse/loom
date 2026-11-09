"""Column registries and row builders for the loom.code_* tables.

Every version row of a ReplacingMergeTree key mirrors EVERY persisted column:
the ``CODE_*_COLS`` dicts (column -> default) are the single source of truth,
and `build_row` raises on an unknown key so a schema/builder drift is a unit
test failure, not a silently zeroed column under FINAL.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from server.code.domain.pr_lexemes import linked_issues, pr_lexemes
from server.core.ch_writes import MAX_PARTITIONS_PER_INSERT, partition_blocks
from server.core.datetimes import naive_utc_now as _now

# ---------------------------------------------------------------------------
# Column registries (single source of truth; mirror the table DDL)
# ---------------------------------------------------------------------------

CODE_FILE_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "path": "",
    "lang": "",
    "module": "",
    "content_hash": "",
    "size_bytes": 0,
    "loc": 0,
    "chunk_count": 0,
    "symbol_count": 0,
    "index_error": "",
    "indexed_commit": "",
    # git's blob id of the bytes that were parsed: lets the
    # reconcile compare the whole index against one
    # `ls-tree` of head instead of one diff per distinct indexed commit.
    # '' on older rows — those fall back to the diff walk.
    "blob_sha": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_CHUNK_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "embedding_model": "",
    "path": "",
    "chunk_id": "",
    "scope": "",
    "kind": "other",
    "start_line": 0,
    "end_line": 0,
    "content": "",
    "content_hash": "",
    "context_text": "",
    "context_description": "",
    "context_source": "structural",
    "lexemes": "",
    "symbol_ids": [],
    "embedding": [],
    "embedding_dim": 0,
    "embed_attempts": 0,
    "embed_last_error": "",
    "embed_failed_at": None,
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_SYMBOL_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "tier": "treesitter",
    "qualified_name": "",
    "symbol_id": "",
    "name": "",
    "kind": "",
    "signature": "",
    "return_type": "",  # The type a function returns, '' when unknown
    "path": "",
    "start_line": 0,
    "end_line": 0,
    "decl_paths": [],
    "doc_comment": "",
    "scip_symbol": "",
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_EDGE_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "tier": "treesitter",
    "edge_kind": "calls",
    "to_symbol_id": "",
    "from_symbol_id": "",
    "path": "",
    "line": 0,
    "from_qualified_name": "",
    "to_qualified_name": "",
    "confidence": 1.0,
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_CALL_MISS_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "callee_name": "",
    "path": "",
    "line": 0,
    "from_symbol_id": "",
    "from_qualified_name": "",
    "call_text": "",
    "disposition": "unresolved",
    "candidates": 0,
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


# Caller guards, one row per caller function per file: the distinct
# (kind, condition) pairs the caller's call sites sit under, and per
# guarded site its line, callee tag and guard indexes. Sparse — a caller with
# no conditional call has no row — so `sites` is never empty here.
CODE_CALLER_GUARD_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "from_symbol_id": "",
    "path": "",
    "guards": [],
    "guard_kinds": [],
    "sites": [],
    "site_callees": [],
    "site_guards": [],
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_PR_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "pr_number": 0,
    "title": "",
    "body_excerpt": "",
    "author": "",
    "labels": [],
    "files": [],
    "assignees": [],
    "additions": 0,
    "deletions": 0,
    "merge_commit_sha": "",
    "merged_at": None,
    "opened_at": None,
    # App-tokenized title + description + linked-issue
    # titles, and the issues the description closes.
    "lexemes": "",
    "linked_issues": [],
    # The branch the PR merged into (GitHub's base.ref);
    # '' = written before the column or not resolved.
    "target_branch": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_GH_ITEM_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "number": 0,
    "is_pr": False,
    "state": "",
    "state_reason": "",
    "author": "",
    "title": "",
    "labels": [],
    "opened_at": None,
    "closed_at": None,
    "merged_at": None,
    "updated_at": None,
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
    # An open PR's head, base and file list
    "head_sha": "",
    "base_sha": "",
    "files": [],
}


CODE_TEST_MAP_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "src_path": "",
    "method": "path_convention",
    "test_path": "",
    "confidence": 1.0,
    "evidence": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_COVERAGE_FILE_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "path": "",
    "commit_sha": "",
    "run_at": None,
    "lines": "[]",
    "tests": 0,
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_SUMMARY_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "embedding_model": "",
    "scope_type": "file",
    "scope_path": "",
    "summary": "",
    "summary_model": "",
    "inputs_hash": "",
    "pagerank": 0.0,
    "in_degree": 0,
    "out_degree": 0,
    "key_symbols": [],
    "embedding": [],
    "embedding_dim": 0,
    "embed_attempts": 0,
    "embed_last_error": "",
    "embed_failed_at": None,
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_ANSWER_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "question_id": "",
    "question": "",
    "question_embedding": [],
    "embedding_model": "",
    "answer": "",
    "citations": "",
    "cited_paths": [],
    "cited_hashes": [],
    "llm_model": "",
    "retrieval_confidence": 0.0,
    "indexed_commit": "",
    # What shaped the entry, what it was shown, what it cost.
    "cache_key": "",
    "prompt_version": "",
    "entry_kind": "answer",
    "abstain_reason": "",
    "retrieval_paths": [],
    "retrieval_hashes": [],
    "cost_usd": 0.0,
    "latency_ms": 0,
    "reused_from": "",
    # The memory the answer quoted, for the erasure saga: its namespace (in the row's
    # org) and the keys of its excerpts.
    "memory_namespace": "",
    "memory_keys": [],
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


# Weekly per-symbol fan-in. No created_at / tombstoned_at: a
# pass rewrites every called symbol at one version and re-emits vanished
# ones at zero, which every read shape drops.
CODE_SYMBOL_STATS_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "symbol_id": "",
    "qualified_name": "",
    "path": "",
    "fan_in_calls": 0,
    "fan_in_nontest": 0,
    "fan_in_name": 0,
    "caller_files": 0,
    "defined_files": 0,
    "is_test": 0,
    "computed_at": None,
    "version": 0,
}


CODE_UNIT_RISK_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "unit_id": "",
    "path": "",
    "churn": 0,
    "amp": 0,
    "tested": 0,
    "callees": "[]",
    "computed_at": None,
    "version": 0,
}


CODE_CONTEXT_PACK_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "pr_number": 0,
    "merge_commit_sha": "",
    "pack": "",
    "pack_tokens": 0,
    "inputs_hash": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_REVIEW_BRIEF_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "pr_number": 0,
    "merge_sha": "",
    "payload": "",
    "sections": [],
    "built_at": None,
    "indexed_commit": "",
    "tokens": 0,
    "build_ms": 0,
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


CODE_TEST_FACETS_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "facet_kind": "",
    "name": "",
    "path": "",
    "ordinal": 0,
    "line": 0,
    "confidence": 1.0,
    "indexed_commit": "",
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
}


_TABLE_COLS: dict[str, dict[str, Any]] = {
    "code_test_facets": CODE_TEST_FACETS_COLS,
    "code_files": CODE_FILE_COLS,
    "code_chunks": CODE_CHUNK_COLS,
    "code_symbols": CODE_SYMBOL_COLS,
    "code_edges": CODE_EDGE_COLS,
    "code_call_sites_unresolved": CODE_CALL_MISS_COLS,
    "code_caller_guards": CODE_CALLER_GUARD_COLS,
    "code_pr_history": CODE_PR_COLS,
    "code_gh_items": CODE_GH_ITEM_COLS,
    "code_test_map": CODE_TEST_MAP_COLS,
    "code_coverage_files": CODE_COVERAGE_FILE_COLS,
    "code_summaries": CODE_SUMMARY_COLS,
    "code_context_packs": CODE_CONTEXT_PACK_COLS,
    "code_review_briefs": CODE_REVIEW_BRIEF_COLS,
    "code_answers": CODE_ANSWER_COLS,
    "code_symbol_stats": CODE_SYMBOL_STATS_COLS,
    "code_unit_risk": CODE_UNIT_RISK_COLS,
}


# Columns that every version row of one ReplacingMergeTree key carries
# unchanged. `code_chunks` keys are content-addressed (chunk_id =
# uuid5(path#scope@sha16) — chunker.py), tombstone_of() mirrors the live row
# and moved_of() may only refresh spans/commits, so the content predicate of
# a grep is constant across a key's versions. That is what lets the grep
# statements set `use_skip_indexes_if_final_exact_mode = 0`: a skipped
# granule can hold no version whose content differs from the kept
# ones, so FINAL over the kept granules alone picks the same row it would
# have picked over all of them. Widen this set only with that invariant in
# mind; moved_of() refuses to touch these columns.
VERSION_INVARIANT_COLS: dict[str, frozenset[str]] = {
    "code_chunks": frozenset({"content", "content_hash", "lexemes", "path", "chunk_id"}),
}


def version_now() -> int:
    return time.time_ns()


def build_row(table: str, **overrides: Any) -> dict[str, Any]:
    """Full row for `table` with every persisted column present. Raises on
    an unknown key (schema/builder drift is a loud failure)."""
    cols = _TABLE_COLS[table]
    unknown = set(overrides) - set(cols)
    if unknown:
        raise ValueError(f"{table}: unknown columns {sorted(unknown)}")
    row = {k: (list(v) if isinstance(v, list) else v) for k, v in cols.items()}
    row.update(overrides)
    if "created_at" in cols and row.get("created_at") is None:
        row["created_at"] = _now()
    if "built_at" in cols and row.get("built_at") is None:
        row["built_at"] = _now()
    if "computed_at" in cols and row.get("computed_at") is None:
        row["computed_at"] = _now()
    if not row.get("version"):
        row["version"] = version_now()
    return row


def _mirror_row(table: str, live_row: dict[str, Any]) -> dict[str, Any]:
    """Copy a live row read back from CH into an insertable newer version:
    every persisted column present, UUID/FixedString values coerced to str
    (FixedString pads with NULs; strip them), version bumped, created_at
    kept (or set when the read lacked it)."""
    import uuid as _uuid

    def _coerce(v: Any) -> Any:
        if isinstance(v, _uuid.UUID):
            return str(v)
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace").rstrip("\x00")
        return v

    cols = _TABLE_COLS[table]
    row = {k: _coerce(live_row.get(k, d)) for k, d in cols.items()}
    row["version"] = version_now()
    if "created_at" in cols and row.get("created_at") is None:
        row["created_at"] = _now()
    return row


def namespace_blocks(
    rows: list[dict[str, Any]], cap: int = MAX_PARTITIONS_PER_INSERT
) -> list[list[dict[str, Any]]]:
    """`rows` as one block, or — when they span more than `cap` namespaces —
    blocks of at most `cap` namespaces each (every writer currently inserts one
    namespace, or one per configured repository, so this is a guard). loom.code_chunks is
    PARTITION BY (org, namespace)."""
    return partition_blocks(rows, lambda r: str(r.get("namespace") or ""), cap)


def pr_history_row(
    *,
    org: str,
    namespace: str,
    pr_number: int,
    title: str,
    body_excerpt: str,
    author: str,
    files: list[str],
    merged_at: Any,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    additions: int = 0,
    deletions: int = 0,
    merge_commit_sha: str = "",
    opened_at: Any = None,
    linked: list[int] | None = None,
    issue_titles: Iterable[str] = (),
    target_branch: str = "",
) -> dict[str, Any]:
    """One full `code_pr_history` row (every persisted column) with lexemes
    and linked_issues derived here. `linked` overrides the numbers parsed
    from the excerpt — the fetch path passes what it saw in the full body."""
    numbers = list(linked) if linked is not None else linked_issues(body_excerpt)
    return build_row(
        "code_pr_history",
        org=org,
        namespace=namespace,
        pr_number=int(pr_number),
        title=title,
        body_excerpt=body_excerpt,
        author=author,
        labels=list(labels or []),
        files=list(files),
        assignees=list(assignees or []),
        additions=int(additions),
        deletions=int(deletions),
        merge_commit_sha=merge_commit_sha,
        merged_at=merged_at,
        **({"opened_at": opened_at} if opened_at is not None else {}),
        lexemes=pr_lexemes(title, body_excerpt, issue_titles),
        linked_issues=[int(n) for n in numbers],
        target_branch=str(target_branch or ""),
    )
