"""The Flow page's tool activity is per namespace, writes
count every way an agent populates a namespace, and a cross-namespace recall
is a read that actually fanned out."""

from __future__ import annotations

from server.identity.app.me_routes import (
    _SHARING_RECALL_OPS,
    _SHARING_WRITE_OPS,
    _fold_sharing_agent_rows,
)
from server.operations.registry import ALL_OPERATIONS


def test_sharing_ops_are_registry_ops_and_cover_the_real_write_paths() -> None:
    names = {op.name for op in ALL_OPERATIONS}
    assert set(_SHARING_WRITE_OPS) <= names
    assert set(_SHARING_RECALL_OPS) <= names
    # the plugins' capture path, a seeded namespace, typed records, knowledge
    for op in (
        "observe.record",
        "memory.set_from_messages",
        "memory.record.write",
        "knowledge.add",
    ):
        assert op in _SHARING_WRITE_OPS
    assert not set(_SHARING_WRITE_OPS) & set(_SHARING_RECALL_OPS)


def test_fold_keeps_namespace_rows_and_sums_per_agent() -> None:
    rows = [
        {
            "agent": "-",
            "namespace": "code-clickhouse",
            "writes": 0,
            "recalls": 44000,
            "cross_ns": 0,
        },
        {"agent": "claude-code", "namespace": "team", "writes": 12, "recalls": 30, "cross_ns": 4},
        {
            "agent": "claude-code",
            "namespace": "code-clickhouse",
            "writes": 0,
            "recalls": 5,
            "cross_ns": 0,
        },
    ]
    agents, per_ns = _fold_sharing_agent_rows(rows)
    assert per_ns[1] == {
        "agent": "claude-code",
        "namespace": "team",
        "writes": 12,
        "recalls": 30,
        "cross_namespace_recalls": 4,
    }
    assert agents == [
        {"agent": "-", "writes": 0, "recalls": 44000, "cross_namespace_recalls": 0},
        {"agent": "claude-code", "writes": 12, "recalls": 35, "cross_namespace_recalls": 4},
    ]


def test_fold_of_nothing_is_two_empty_lists() -> None:
    assert _fold_sharing_agent_rows([]) == ([], [])
