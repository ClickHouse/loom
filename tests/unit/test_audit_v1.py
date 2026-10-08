"""Op + field declarations for agent-pipeline audit v1.

The handlers + service-layer behavior is exercised by the integration
test against a live ClickHouse; these unit tests lock in the static
shape so accidental breaking changes surface in CI without a CH.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# ============================================================
# hybrid search + rerank
# ============================================================


def test_memory_search_input_accepts_hybrid_mode() -> None:
    from server.memory.api.models_search import MemorySearchInput

    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        query="ColumnObject::getExtremes",
        search_mode="hybrid",
        alpha=0.7,
        include_top_n_unmatched=20,
    )
    assert inp.search_mode == "hybrid"
    assert inp.alpha == 0.7
    assert inp.include_top_n_unmatched == 20


def test_memory_set_from_messages_judge_extraction_default_and_override() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesInput

    base = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="a",
        messages=[{"role": "user", "content": "I prefer dark mode"}],
    )
    judged = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="a",
        messages=[{"role": "user", "content": "I prefer dark mode"}],
        judge_extraction=True,
    )

    assert base.judge_extraction is False
    assert judged.judge_extraction is True


# ============================================================
# per-namespace memory_type registry
# ============================================================


def test_namespace_register_input_accepts_tag_registry() -> None:
    """tag_registry is the field name; there is no type_registry alias."""
    from server.identity.api.models import NamespaceRegisterInput, NamespaceTypeConfig

    inp_new = NamespaceRegisterInput(
        org="o",
        namespace="n",
        owner_email="x",
        tag_registry=[
            NamespaceTypeConfig(
                memory_type="observation",
                retention_days=120,
                dedup_threshold=0.80,
                hot_quota=8,
            ),
            NamespaceTypeConfig(
                memory_type="principle",
                retention_days=0,
                dedup_threshold=0.85,
                hot_quota=5,
                conflict_policy="manual_review",
            ),
        ],
    )
    assert len(inp_new.tag_registry) == 2
    assert inp_new.tag_registry[0].memory_type == "observation"
    assert inp_new.tag_registry[1].conflict_policy == "manual_review"


# ============================================================
# usage-driven lifecycle
# ============================================================


def test_memory_search_input_auto_touch_defaults_true() -> None:
    """Default is ON so the /me Inspector's "Last recalled" field
    populates without callers having to opt in. Callers explicitly
    wanting telemetry-free search can still pass auto_touch=False."""
    from server.memory.api.models_search import MemorySearchInput

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    assert inp.auto_touch is True
    assert (
        MemorySearchInput(
            org="o",
            namespace="n",
            agent="a",
            query="q",
            auto_touch=False,
        ).auto_touch
        is False
    )


def test_memory_outcome_op_registered() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    assert "memory.outcome" in OPERATIONS_BY_NAME
    op = OPERATIONS_BY_NAME["memory.outcome"]
    assert op.audit is True
    from server.operations.base import Role

    assert op.required_role == Role.WRITER


def test_namespace_register_input_has_policy_defaults() -> None:
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    inp = NamespaceRegisterInput(org="o", namespace="n", owner_email="x")
    # None keeps the stored value; a fresh row takes the default.
    assert inp.default_conflict_policy is None and inp.consolidation_enabled is None
    fresh = registry_row(inp, None, datetime(2026, 1, 1))
    assert fresh["default_conflict_policy"] == "auto_reinforce"
    assert fresh["consolidation_enabled"] is False


def test_registry_row_carries_the_unread_promotion_columns() -> None:
    """The auto-promote columns have no reader and no wire field, but the
    registry row is a full ReplacingMergeTree row: it carries the stored
    values forward and writes the column defaults for a fresh row."""
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    inp = NamespaceRegisterInput(org="o", namespace="n", owner_email="x")
    now = datetime(2026, 1, 1)
    fresh = registry_row(inp, None, now)
    assert (
        fresh["auto_promote_enabled"],
        fresh["auto_promote_gate"],
        fresh["evidence_threshold"],
    ) == (False, "k_supporters", 5)
    stored = {
        "auto_promote_enabled": True,
        "auto_promote_gate": "evidence_threshold",
        "evidence_threshold": 3,
    }
    kept = registry_row(inp, {**stored, "conversation_logging_enabled": False}, now)
    assert {k: kept[k] for k in stored} == stored


def test_projection_failure_ops_are_registered() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    assert "ops.projection_failures.list" in OPERATIONS_BY_NAME
    assert "ops.projection_failures.retry" in OPERATIONS_BY_NAME
    assert "ops.projection_failures.resolve" in OPERATIONS_BY_NAME
