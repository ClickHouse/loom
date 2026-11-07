"""The usage fold's loom.memory statements on clickhouse-local: each memory takes its own
counters, whichever namespace or org shares its id."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from server.memory.workers import usage_fold
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")

MID = "6b47c9b7-0000-0000-0000-000000000001"
MID2 = "6b47c9b7-0000-0000-0000-000000000002"
SEEN = datetime(2026, 1, 1, tzinfo=UTC)


async def _memory(kept: KeptCH, *rows: tuple[str, str, str, str]) -> None:
    """loom.memory rows (org, namespace, key, memory_id), never folded."""
    values = ", ".join(
        f"('{o}', '{ns}', 'x', '-', '-', '{k}', '{m}', 'v', 1)" for o, ns, k, m in rows
    )
    await kept.execute(
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"memory_id, value, version) VALUES {values}"
    )


def _target(org: str, namespace: str, key: str, memory_id: str, recall: int) -> Any:
    return usage_fold._Target(
        memory_id=memory_id,
        org=org,
        namespace=namespace,
        agent="x",
        user_id="-",
        memory_key=key,
        evidence=1 + recall,
        recall=recall,
        last_recalled=SEEN,
        reinforcement=None,
        last_activity=SEEN,
    )


async def _counters(kept: KeptCH) -> dict[tuple[str, str, str], tuple[int, int, Any]]:
    rows = await kept.query_all(
        "SELECT org, namespace, memory_key, recall_count, evidence_count, last_recalled_at "
        "FROM loom.memory FINAL"
    )
    return {
        (r["org"], r["namespace"], r["memory_key"]): (
            r["recall_count"],
            r["evidence_count"],
            r["last_recalled_at"],
        )
        for r in rows
    }


@pytest.mark.parametrize("alter", [False, True], ids=["lightweight", "alter-update"])
async def test_each_memory_takes_its_own_counters_whoever_shares_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alter: bool
) -> None:
    """One id in two namespaces, in two orgs, and under two keys of one namespace: each
    (org, namespace, id) takes its own totals. (o1, a)'s copy of MID2 has no usage,
    though its namespace and id are both in the statement, and is left alone."""
    kept = KeptCH(tmp_path, ddl("memory"))
    await _memory(
        kept,
        ("o1", "a", "ka", MID),
        ("o1", "b", "kb", MID),
        ("o1", "b", "kb2", MID),  # restored into itself under another key
        ("o2", "a", "kc", MID),  # another org's copy, no usage here
        ("o1", "b", "kd", MID2),
        ("o2", "b", "ke", MID2),  # differs from kd by org alone
        ("o1", "a", "kf", MID2),  # the unused copy
    )
    statements: list[str] = []
    real = kept.execute

    async def _execute(sql: str, params: Any = None, **kw: Any) -> None:
        statements.append(sql)
        await real(sql, params, **kw)

    monkeypatch.setattr(kept, "execute", _execute)
    if alter:
        # The fallback a replica without lightweight updates takes: the same SET and WHERE.
        async def _no_lightweight(sql: str, params: Any = None, **kw: Any) -> None:
            if sql.startswith("UPDATE "):
                raise RuntimeError("Code: 48. DB::Exception: lightweight UPDATE unsupported")
            # each clickhouse-local call is its own process: wait for the mutation here
            await _execute(sql.replace("mutations_sync = 0", "mutations_sync = 2"), params, **kw)

        monkeypatch.setattr(kept, "execute", _no_lightweight)
    await usage_fold._fold_memory_rows(
        kept,
        [
            _target("o1", "a", "ka", MID, 2),
            _target("o1", "b", "kb", MID, 7),
            _target("o1", "b", "kb2", MID, 7),
            _target("o1", "b", "kd", MID2, 1),
            _target("o2", "b", "ke", MID2, 4),
        ],
        500,
    )
    seen = SEEN.replace(tzinfo=None)
    assert await _counters(kept) == {
        ("o1", "a", "ka"): (2, 3, seen),
        ("o1", "b", "kb"): (7, 8, seen),
        ("o1", "b", "kb2"): (7, 8, seen),
        ("o2", "a", "kc"): (0, 1, None),
        ("o1", "b", "kd"): (1, 2, seen),
        ("o2", "b", "ke"): (4, 5, seen),
        ("o1", "a", "kf"): (0, 1, None),
    }
    assert any(sql.startswith("ALTER ") for sql in statements) is alter


async def test_a_cold_start_resumes_at_the_namespace_still_behind(tmp_path: Path) -> None:
    """The watermark compares each (org, namespace, id)'s totals with its own row: a,
    folded in full and bumped last, is not behind; b is, so the pass resumes just
    before b's bump. Summed across both, the id read as behind at a's later bump."""
    kept = KeptCH(tmp_path, ddl("memory"), ddl("memory_usage_totals"))
    await _memory(kept, ("o", "a", "ka", MID), ("o", "b", "kb", MID))
    await kept.execute(
        "ALTER TABLE loom.memory UPDATE recall_count = 5 WHERE namespace = 'a' "
        "SETTINGS mutations_sync = 2"
    )
    await kept.execute(
        "INSERT INTO loom.memory_usage_totals (memory_id, org, namespace, evidence_count, "
        "recall_count, used_count, harm_count, last_recalled_at, bumped_at) VALUES "
        f"('{MID}', 'o', 'a', 0, 5, 0, 0, '2026-01-01 00:00:00', '2026-01-03 00:00:00'), "
        f"('{MID}', 'o', 'b', 0, 3, 0, 0, '2026-01-01 00:00:00', '2026-01-02 00:00:00')"
    )
    hwm = datetime(2026, 2, 1, tzinfo=UTC)
    got = await usage_fold._derive_cold_start_watermark(kept, hwm)
    assert got == datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)


async def test_the_identity_read_returns_only_the_memories_of_the_batch(tmp_path: Path) -> None:
    """The pairs and ids of a batch also match other copies; only the batch's own
    memories come back, each with the `used_cited` sum of its own events."""
    kept = KeptCH(tmp_path, ddl("memory"), ddl("memory_usage_events"))
    await _memory(
        kept,
        ("o1", "a", "ka", MID),
        ("o1", "b", "kd", MID2),
        ("o1", "a", "kf", MID2),  # (o1, a) and MID2 are both in the batch, this copy is not
    )
    await kept.execute(
        "INSERT INTO loom.memory_usage_events (memory_id, org, namespace, event_type, "
        f"event_count) VALUES ('{MID2}', 'o1', 'b', 'used_cited', 2), "
        f"('{MID2}', 'o1', 'a', 'used_cited', 5)"
    )
    keys = [("o1", "a", MID), ("o1", "b", MID2)]
    rows = await kept.query_all(
        usage_fold.IDENTITY_STATEMENT, params=usage_fold._scope_params(keys)
    )
    assert sorted((r["memory_key"], int(r["used_cited_total"])) for r in rows) == [
        ("ka", 0),
        ("kd", 2),
    ]
