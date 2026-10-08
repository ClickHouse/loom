"""A revision (a new value over the live row of the same key) carries every
column the write does not set, except the ones about the old value.

A revision written as a hand-built row would take the DDL default under FINAL
for every column it left out, so a revised fact would lose its recall and
evidence counts (which rank it), its last recall, and its cross-promotion
lineage."""

from __future__ import annotations

import re
from datetime import datetime

from server.memory import store as mr
from server.memory.store import rows as rows_mod
from tests.unit.schema_ddl import ddl
from tests.unit.test_rmt_write_is_typed import _every_column_row

_NEW_SHA = "ef" * 32


def _new(**over):
    return {"value": '"new"', "value_json": "{}", "content_sha256": _NEW_SHA, **over}


def test_every_column_is_carried_or_reset_by_the_documented_rule() -> None:
    src = _every_column_row()
    carried = mr.build_memory_version_row(src)
    row = mr.build_revision_row(src, _new())
    assert set(row) == set(mr.MEMORY_PERSISTED_COLS)
    resets = rows_mod.REVISION_RESETS
    for col in mr.MEMORY_PERSISTED_COLS:
        if col in _new():
            assert row[col] == _new()[col], col
        elif col in resets:
            assert row[col] == resets[col], col
        else:
            assert row[col] == carried[col], col
    # the usage and lineage columns, by name
    assert (row["recall_count"], row["evidence_count"]) == (3, 4)
    assert row["last_recalled_at"] == datetime(2026, 1, 2, 3, 4, 5)
    assert row["cross_promoted_from_namespace"] == src.cross_promoted_from_namespace
    # ...and the verdicts on the old value, by name
    assert row["status"] == "active" and row["superseded_by"] == "" and row["invalid_at"] is None


def test_the_attestation_is_carried_and_a_changed_value_reads_stale() -> None:
    """Same contract as memory.update: an edit keeps who attested and the
    hash they attested, so verification_state reads it as stale."""
    from server.memory.domain.trust import verification_state

    src = _every_column_row()
    same = mr.build_revision_row(src, _new(content_sha256=src.content_sha256))
    assert (same["verified_by"], same["verified_sha256"]) == ("human:alice", "cd" * 32)
    changed = mr.build_revision_row(src, _new())
    assert (changed["verified_by"], changed["verified_at"], changed["verified_sha256"]) == (
        src.verified_by,
        src.verified_at,
        src.verified_sha256,
    )
    assert changed["content_sha256"] != src.verified_sha256
    assert (
        verification_state(
            changed["verified_by"], changed["verified_sha256"], changed["content_sha256"]
        )
        == "stale"
    )


def test_what_the_write_sets_wins_over_a_reset() -> None:
    src = _every_column_row()
    row = mr.build_revision_row(src, _new(status="conflicted", grounding="quoted"))
    assert row["status"] == "conflicted" and row["grounding"] == "quoted"


def test_rows_get_their_own_lists() -> None:
    a = mr.build_revision_row(_every_column_row(), _new())
    a["conflict_with"].append("x")
    b = mr.build_revision_row(_every_column_row(), _new())
    assert b["conflict_with"] == [] and rows_mod.REVISION_RESETS["conflict_with"] == []


def _ddl_default(col: str) -> str:
    (line,) = [ln for ln in ddl("memory").splitlines() if ln.strip().startswith(f"`{col}`")]
    m = re.search(r"DEFAULT (.+?),?$", line.strip())
    return m.group(1) if m else ("NULL" if "Nullable(" in line else "")


def test_first_version_defaults_are_the_ddl_defaults() -> None:
    now = datetime(2026, 9, 28, 12, 0, 0)
    for col, value in rows_mod.FIRST_VERSION_DEFAULTS.items():
        want = _ddl_default(col)
        got = (
            "NULL"
            if value is None
            else "[]"
            if value == []
            else f"'{value}'"
            if isinstance(value, str)
            else str(value)
        )
        if want in ("0", "0.", "0.0") and got == "0.0":
            continue
        assert got == want, (col, got, want)
    row = mr.build_first_version_row({"memory_key": "k"}, now=now)
    assert row["updated_at"] == row["last_reinforced_at"] == now
    assert _ddl_default("updated_at") == _ddl_default("last_reinforced_at") == "now()"


def test_memory_set_revision_carries_the_live_rows_usage() -> None:
    """The point path's version row over an existing key, built by its phase 6."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import set_commit as set_commit
    from server.memory.app import write as mw

    src = _every_column_row()
    inp = MemorySetInput(
        org=src.org,
        namespace=src.namespace,
        agent=src.agent,
        user_id=src.user_id,
        session_id=src.session_id,
        memory_key=src.memory_key,
        value="a revised value",
        memory_type="preference",
    )
    w = set_commit._SetWrite(input=inp, version=10, retyped_from="", existing=src, expires_at=None)
    w.serialized_value, w.new_sha = '"a revised value"', _NEW_SHA
    mw._set_build_memory_row(w)
    row = w.memory_row
    mr.check_memory_rows([row])
    assert (row["recall_count"], row["evidence_count"]) == (3, 4)
    assert row["memory_id"] == src.memory_id and row["created_at"] == src.created_at
    assert row["value"] == '"a revised value"' and row["version"] == 10
    assert row["verified_by"] == src.verified_by and row["status"] == "active"


def test_memory_set_first_version_row_is_complete_with_ddl_defaults() -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import set_commit as set_commit
    from server.memory.app import write as mw

    inp = MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value="v")
    w = set_commit._SetWrite(input=inp, version=1, retyped_from="", existing=None, expires_at=None)
    w.serialized_value, w.new_sha = '"v"', _NEW_SHA
    mw._set_build_memory_row(w)
    row = w.memory_row
    mr.check_memory_rows([row])  # the funnel accepts it: every persisted column
    assert row["recall_count"] == 0 and row["evidence_count"] == 1
    assert row["status"] == "active" and row["verified_by"] == ""
    assert row["lock_mode"] == "" and row["memory_id"]
