"""The ReplacingMergeTree write is typed.

`insert_memory_versions` refuses a loom.memory row that is not a full
version row; MEMORY_PERSISTED_COLS equals the schema's column list (and the
append-only mirror's); every loom.memory write in server/ goes through
`insert_memory_versions` (a raw `ch.insert("loom.memory", …)` site is
allow-listed here with a reason) and every loom.shared insert reaches its
builder; a restored row equals its source on every column except
the identity overrides; a copy carries the source's facets and resets its
lifecycle. CH-free."""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from server.memory import store as mr

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# the typed insert
# ---------------------------------------------------------------------------


def _full_row(**over: Any) -> dict[str, Any]:
    row = dict.fromkeys(mr.MEMORY_PERSISTED_COLS, None)
    row.update({"org": "o", "namespace": "n", "memory_key": "k", "version": 1})
    row.update(over)
    return row


class _CH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]], dict[str, Any] | None]] = []

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows, settings))


def test_insert_memory_versions_refuses_a_row_missing_a_persisted_column() -> None:
    row = _full_row()
    del row["sensitivity"]
    with pytest.raises(ValueError, match=r"missing=\['sensitivity'\]"):
        mr.check_memory_rows([row])
    ch = _CH()
    with pytest.raises(ValueError, match="'k'"):
        asyncio.run(mr.insert_memory_versions([row], ch=ch))
    assert ch.inserted == []  # nothing lands when one row is short


def test_insert_memory_versions_refuses_an_unknown_column_and_writes_a_full_row_committed() -> None:
    with pytest.raises(ValueError, match=r"unknown=\['sensitivty'\]"):
        mr.check_memory_rows([_full_row(sensitivty="high")])
    ch = _CH()
    n = asyncio.run(mr.insert_memory_versions([_full_row(), _full_row(memory_key="k2")], ch=ch))
    assert n == 2
    ((table, rows, settings),) = ch.inserted
    assert table == "loom.memory" and len(rows) == 2
    assert settings == {"async_insert": 0, "wait_for_async_insert": 1}
    assert asyncio.run(mr.insert_memory_versions([], ch=_CH())) == 0


# ---------------------------------------------------------------------------
# the column set is the schema's
# ---------------------------------------------------------------------------


def _schema_columns(table: str) -> list[str]:
    text = (ROOT / "schemas" / "schema.sql").read_text()
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\n\((.*?)\n\)\nENGINE", text, re.S
    )
    assert m, table
    # A MATERIALIZED column is the table's own (`value_preview`,
    # `value_len`) — no writer carries it, so it is not a
    # persisted column here.
    return [
        name
        for name, rest in re.findall(r"(?m)^\s{4}`(\w+)`(.*)$", m.group(1))
        if " MATERIALIZED " not in rest
    ]


def test_memory_persisted_cols_equal_the_schema_and_the_versions_mirror() -> None:
    memory = _schema_columns("loom.memory")
    versions = _schema_columns("loom.memory_versions")
    assert len(memory) == 61
    assert set(mr.MEMORY_PERSISTED_COLS) == set(memory), set(mr.MEMORY_PERSISTED_COLS) ^ set(memory)
    assert set(versions) == set(memory), set(versions) ^ set(memory)
    assert len(set(mr.MEMORY_PERSISTED_COLS)) == len(mr.MEMORY_PERSISTED_COLS)
    # MemoryRow carries every column the builder reads
    assert set(mr.MEMORY_PERSISTED_COLS) <= set(mr.MemoryRow.model_fields)


# ---------------------------------------------------------------------------
# AST lint: every loom.memory / loom.shared insert reaches the builder
# ---------------------------------------------------------------------------

_BUILDER_NAMES = {
    "build_memory_version_row",
    "insert_memory_versions",
    "MEMORY_PERSISTED_COLS",
    # loom.shared's own column set, checked by `shared_repo.insert_shared_row`
    "SHARED_PERSISTED_COLS",
}
# The one raw loom.memory insert: the checked funnel itself.
_MEMORY_FUNNEL = ("server/memory/store/inserts.py", "insert_memory_versions")


def _names_in(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            found.add(n.id)
        elif isinstance(n, ast.Attribute):
            found.add(n.attr)
    return found


def _reaches(
    fn: ast.AST, funcs: dict[str, ast.AST], seen: set[str], builders: set[str] = _BUILDER_NAMES
) -> bool:
    """The function names a builder, or calls a same-module function that does."""
    names = _names_in(fn)
    if names & builders:
        return True
    for callee in names & set(funcs):
        if callee not in seen:
            seen.add(callee)
            if _reaches(funcs[callee], funcs, seen, builders):
                return True
    return False


def _is_memory_insert(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in ("loom.memory", "loom.shared")
    )


def _sites_in(path: Path) -> list[tuple[str, str, str, bool]]:
    src = path.read_text()
    if '"loom.memory"' not in src and '"loom.shared"' not in src:
        return []
    tree = ast.parse(src)
    funcs: dict[str, ast.AST] = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    out: list[tuple[str, str, str, bool]] = []
    stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    while stack:
        node, fn = stack.pop()
        if _is_memory_insert(node):
            ok = bool(fn is not None and _reaches(fn, funcs, {getattr(fn, "name", "")}))
            table = node.args[0].value  # type: ignore[attr-defined]
            out.append((str(path.relative_to(ROOT)), getattr(fn, "name", "<module>"), table, ok))
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
            stack.append((child, inner))
    return out


def _insert_sites() -> list[tuple[str, str, str, bool]]:
    """(relative file, enclosing function, table, reaches_builder) for every
    `.insert("loom.memory"|"loom.shared", …)` under server/."""
    out: list[tuple[str, str, str, bool]] = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        out.extend(_sites_in(path))
    return out


def test_every_loom_memory_write_goes_through_the_checked_funnel() -> None:
    sites = _insert_sites()
    raw = {(f, fn) for f, fn, table, _ in sites if table == "loom.memory"}
    assert _MEMORY_FUNNEL in raw, sites
    outside = sorted(raw - {_MEMORY_FUNNEL})
    assert not outside, (
        "raw loom.memory inserts outside store/inserts.insert_memory_versions; route them "
        f"through it (it checks every row carries MEMORY_PERSISTED_COLS): {outside}"
    )


def test_every_loom_shared_insert_reaches_its_builder() -> None:
    shared = [(f, fn, ok) for f, fn, table, ok in _insert_sites() if table == "loom.shared"]
    assert shared, "no loom.shared insert found: the scan is broken"
    assert all(ok for _, _, ok in shared), shared


# ---------------------------------------------------------------------------
# AST lint: every memory_embeddings write is a builder row
# ---------------------------------------------------------------------------

# search_document_row builds its row with build_embedding_row.
_EMBEDDING_BUILDER_NAMES = {
    "build_embedding_row",
    "EMBEDDING_PERSISTED_COLS",
    "search_document_row",
}
# (file, function) → why its rows are builder rows the lint cannot see.
_EMBEDDING_ALLOW: dict[tuple[str, str], str] = {
    # The funnel every other write goes through: it inserts what it is handed.
    ("server/memory/store/inserts.py", "insert_embedding_rows"): "the funnel",
    ("server/memory/store/lifecycle.py", "insert_documented_versions"): (
        "inserts the documents its caller built (period_digest.build_period_memory_rows, "
        "with build_embedding_row) within the commit's lease"
    ),
    ("server/memory/app/set_commit.py", "_insert_embedding"): (
        "_insert_row_and_vector inserts the row its caller built with search_document_row"
    ),
    ("server/memory/app/bulk_set.py", "_insert_embedding_rows"): (
        "bulk_set's embed phase builds each row with build_embedding_row into "
        "_BulkWriteState.embedding_rows; _bulk_insert's nested insert only inserts them"
    ),
    ("server/memory/app/update.py", "_insert_revision"): (
        "_write_revision builds emb_rows with _update_embedding_row (build_embedding_row) "
        "and hands them over"
    ),
    ("server/memory/app/update.py", "_document"): (
        "_commit_revision's nested insert of the emb_rows _write_revision built"
    ),
    ("server/memory/app/namespace_rollback.py", "_follow_ups"): (
        "apply_rollback builds each stub with build_embedding_row (_stub); _write's nested "
        "follow-up inserts them under the commit's lease"
    ),
}


def _is_embedding_write(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if (
        isinstance(f, ast.Attribute)
        and f.attr == "insert"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "loom.memory_embeddings"
    ):
        return True
    return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == (
        "insert_embedding_rows"
    )


def _embedding_write_sites() -> list[tuple[str, str, bool]]:
    """(file, innermost function, reaches_builder) for every direct
    `.insert("loom.memory_embeddings", …)` and every `insert_embedding_rows(…)`
    call under server/. A nested writer counts as reaching the builder when a
    function enclosing it does: the rows are built there and inserted inside."""
    out: list[tuple[str, str, bool]] = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        src = path.read_text()
        if "memory_embeddings" not in src and "insert_embedding_rows" not in src:
            continue
        tree = ast.parse(src)
        funcs: dict[str, ast.AST] = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }

        stack: list[tuple[ast.AST, tuple[ast.AST, ...]]] = [(tree, ())]
        while stack:
            node, chain = stack.pop()
            if _is_embedding_write(node):
                ok = any(
                    _reaches(fn, funcs, {getattr(fn, "name", "")}, _EMBEDDING_BUILDER_NAMES)
                    for fn in chain
                )
                name = getattr(chain[-1], "name", "<module>") if chain else "<module>"
                out.append((str(path.relative_to(ROOT)), name, ok))
            for child in ast.iter_child_nodes(node):
                is_fn = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                stack.append((child, (*chain, child) if is_fn else chain))
    return out


def test_every_embedding_write_reaches_the_embedding_builder() -> None:
    sites = _embedding_write_sites()
    assert len(sites) >= 20, sites
    bypass = {(f, fn) for f, fn, ok in sites if not ok}
    unexplained = sorted(bypass - set(_EMBEDDING_ALLOW))
    assert not unexplained, (
        "loom.memory_embeddings writes whose rows are not built by build_embedding_row "
        f"(an omitted column is served as its DDL default under FINAL): {unexplained}"
    )
    assert not sorted(set(_EMBEDDING_ALLOW) - bypass), "stale allow-list entry"


def test_embedding_persisted_cols_equal_the_schema() -> None:
    cols = _schema_columns("loom.memory_embeddings")
    assert set(mr.EMBEDDING_PERSISTED_COLS) == set(cols), set(mr.EMBEDDING_PERSISTED_COLS) ^ set(
        cols
    )


# ---------------------------------------------------------------------------
# restore round-trip and the copy
# ---------------------------------------------------------------------------


def _every_column_row() -> mr.MemoryRow:
    t = datetime(2026, 1, 2, 3, 4, 5)
    return mr.MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="src-org",
        namespace="src-ns",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="preference",
        value={"x": 1},
        confidence=0.7,
        version=5,
        pinned=True,
        created_at=t,
        updated_at=datetime(2026, 1, 3),
        expires_at=datetime(2027, 1, 1),
        tombstoned_at=None,
        valid_at=datetime(2025, 12, 1),
        valid_to=datetime(2026, 6, 1),
        invalid_at=datetime(2026, 5, 1),
        char_limit=500,
        run_id="r",
        source_trace="trace",
        tags=["t1"],
        entities=["e1"],
        status="conflicted",
        content_sha256="ab" * 32,
        recall_count=3,
        last_recalled_at=t,
        value_json='{"x":1}',
        redacted_at=None,
        redaction_reason="",
        conflict_with=["66666666-6666-6666-6666-666666666666"],
        duplicate_of="77777777-7777-7777-7777-777777777777",
        evidence_count=4,
        cross_promoted_from_memory_id="",
        cross_promoted_from_namespace="",
        superseded_by="sup",
        rejection_reason="rej",
        temporal_anchor=datetime(2025, 11, 1),
        temporal_confidence=0.4,
        reinforcement_count=6,
        last_reinforced_at=t,
        mentioned_at=t,
        source_resource_id="res",
        verified_by="human:alice",
        verified_at=t,
        sensitivity="high",
        verified_sha256="cd" * 32,
        grounding="quoted",
        evidence="the quote",
        actor="human:bob",
        source_class="asserted",
        lock_mode="hard",
        lock_reason="frozen",
        lock_until=datetime(2030, 1, 1),
        locked_by="human:bob",
        locked_at=t,
        on_expire="demote",
        expire_reason="stale",
        files=["a.py"],
        concepts=["Foo"],
    )


def test_a_restored_row_equals_its_source_on_every_column_except_the_identity_overrides() -> None:
    from server.identity.app.namespace_snapshot import snapshot_memory_row
    from server.memory.app.snapshot_rows import RESTORE_IDENTITY_OVERRIDES, restore_rows

    src = _every_column_row()
    source_row = mr.build_memory_version_row(src)  # the loom.memory row as CH would return it
    payload_row = snapshot_memory_row(source_row)
    (restored,) = restore_rows(
        [payload_row],
        org="dst-org",
        target_namespace="dst-ns",
        rebind_agent=None,
        version_base=99,
        actor="process:restore",
    )
    assert set(restored) == set(mr.MEMORY_PERSISTED_COLS)
    diff = {c for c in mr.MEMORY_PERSISTED_COLS if restored[c] != source_row[c]}
    assert diff <= RESTORE_IDENTITY_OVERRIDES, sorted(diff - RESTORE_IDENTITY_OVERRIDES)
    assert restored["created_at"] == src.created_at  # never the restore time
    assert restored["temporal_anchor"] == src.temporal_anchor and restored["sensitivity"] == "high"
    assert restored["status"] == "conflicted" and restored["concepts"] == ["Foo"]
    assert restored["lock_mode"] == "hard" and restored["files"] == ["a.py"]
    assert restored["org"] == "dst-org" and restored["namespace"] == "dst-ns"
    assert restored["version"] == 99 and restored["source_class"] == "imported"
    assert restored["actor"] == "process:restore" and restored["tombstoned_at"] is None
    mr.check_memory_rows([restored])


def test_an_older_payload_restores_with_the_model_defaults_and_a_fresh_created_at() -> None:
    from server.identity.api.models import _SnapshotMemoryRow
    from server.memory.app.snapshot_rows import restore_rows

    old = _SnapshotMemoryRow(
        memory_id="44444444-4444-4444-4444-444444444444",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        memory_key="k",
        memory_type="semantic",
        value='"v"',
        confidence=1.0,
        source_trace="",
        pinned=False,
    )
    before = datetime.now(UTC).replace(tzinfo=None)
    (row,) = restore_rows(
        [old], org="o", target_namespace="n", rebind_agent="b", version_base=1, actor=""
    )
    assert row["created_at"] >= before.replace(microsecond=0) and row["agent"] == "b"
    assert row["status"] == "active" and row["sensitivity"] == "" and row["value"] == '"v"'
    assert row["content_sha256"] == "0" and row["actor"] == ""
    mr.check_memory_rows([row])


def test_a_copy_carries_the_facets_and_attestation_and_resets_the_lifecycle() -> None:
    from server.memory.app.copy_ops import _COPY_RESETS, copied_memory_row

    src = _every_column_row()
    now = datetime(2026, 9, 15, 12, 0, 0)
    row = copied_memory_row(
        src,
        org="src-org",
        namespace="dst-ns",
        agent="b",
        memory_key="k2",
        memory_id="22222222-2222-2222-2222-222222222222",
        version=7,
        value='{"x": 1}',
        from_memory_id=src.memory_id,
        from_namespace="src-ns",
        actor="human:carol",
        now=now,
    )
    mr.check_memory_rows([row])
    carried = (
        "tags",
        "entities",
        "files",
        "concepts",
        "valid_at",
        "valid_to",
        "temporal_anchor",
        "temporal_confidence",
        "sensitivity",
        "grounding",
        "evidence",
        "char_limit",
        "mentioned_at",
        "content_sha256",
        "verified_by",
        "verified_at",
        "verified_sha256",
        "memory_type",
        "confidence",
        "source_trace",
        "source_resource_id",
    )
    for c in carried:
        assert row[c] == mr.build_memory_version_row(src)[c], c
    for c, v in _COPY_RESETS.items():
        assert row[c] == v, c
    assert row["created_at"] == row["updated_at"] == row["last_reinforced_at"] == now
    assert row["cross_promoted_from_memory_id"] == src.memory_id
    assert row["cross_promoted_from_namespace"] == "src-ns" and row["namespace"] == "dst-ns"
    assert row["memory_key"] == "k2" and row["version"] == 7 and row["actor"] == "human:carol"
    # a copy of a zero-uuid lineage lands NULL, not the zero uuid
    row2 = copied_memory_row(
        src,
        org="o",
        namespace="n",
        agent="b",
        memory_key="k",
        memory_id="m",
        version=1,
        value="v",
        from_memory_id=mr.ZERO_UUID,
        from_namespace="n",
        actor="",
        now=now,
    )
    assert row2["cross_promoted_from_memory_id"] is None


def test_a_version_row_built_from_a_read_carries_every_column_the_read_returned() -> None:
    """The read → re-emit seam every builder caller crosses: a row as the
    full FINAL projection returns it, hydrated and rebuilt with no
    overrides, equals itself on every persisted column. A column the
    projection selects but `_hydrate` drops lands NULL on the next version
    row (a superseded row's `invalid_at`, say)."""
    stored = mr.build_memory_version_row(_every_column_row())
    read = {**stored, "value_text": stored["value"]}  # the projection's alias
    rebuilt = mr.build_memory_version_row(mr._hydrate(read))
    diff = {c for c in mr.MEMORY_PERSISTED_COLS if rebuilt[c] != stored[c]}
    assert not diff, sorted(diff)
    assert rebuilt["invalid_at"] == datetime(2026, 5, 1)


def test_the_full_projection_selects_every_persisted_column() -> None:
    projection = mr._MEMORY_FINAL_PROJECTION
    missing = [c for c in mr.MEMORY_PERSISTED_COLS if not re.search(rf"\b{c}\b", projection)]
    assert not missing, missing


def test_from_snapshot_takes_defaults_for_missing_columns_and_keeps_a_serialized_value() -> None:
    m = mr.MemoryRow.from_snapshot(
        {"memory_key": "k", "value": '"v"', "agent": "a"}, org="o", namespace="n"
    )
    assert m.value == '"v"' and m.status == "active" and m.user_id == "-" and m.version == 0
    assert m.value_text == '"v"'  # The payload's text is the stored bytes
    row = mr.build_memory_version_row(m)
    assert row["value"] == '"v"'  # a serialized payload is written verbatim
    assert set(row) == set(mr.MEMORY_PERSISTED_COLS)
