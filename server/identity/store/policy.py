"""Repository for loom.namespace_policies and the loom.policy_decisions ledger.

Rules are one row per (org, namespace, policy_id) under ReplacingMergeTree
on `version`; policy.set re-inserts, policy.delete re-inserts with
`tombstoned_at`. The decisions ledger is append-only and written
fire-and-forget by the gate; it is the shadow-mode instrument ("what would
enforce have denied").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.policy_rules import PolicyRule, parse_rule

_RULE_COLUMNS = (
    "org",
    "namespace",
    "policy_id",
    "effect",
    "description",
    "actions",
    "condition",
    "enabled",
    "mode",
    "created_by",
    "created_at",
    "updated_at",
    "tombstoned_at",
)


def rule_to_row(
    org: str,
    namespace: str,
    rule: PolicyRule,
    *,
    created_by: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "org": org,
        "namespace": namespace,
        "policy_id": rule.policy_id,
        "effect": rule.effect,
        "description": rule.description,
        "actions": list(rule.actions),
        "condition": rule.condition.model_dump_json(),
        "enabled": bool(rule.enabled),
        "mode": rule.mode,
        "created_by": created_by,
        "created_at": (created_at or now).replace(tzinfo=None) if created_at else now,
        "updated_at": now,
        "tombstoned_at": None,
    }


def row_to_rule(row: dict[str, Any]) -> PolicyRule:
    cond = row["condition"]
    return parse_rule(
        {
            "policy_id": row["policy_id"],
            "effect": row["effect"],
            "actions": list(row["actions"]),
            "condition": json.loads(cond) if isinstance(cond, str) else cond,
            "description": row.get("description", "") or "",
            "enabled": bool(row.get("enabled", True)),
            "mode": row.get("mode") or "inherit",
        },
        stored=True,
    )


async def upsert_rule(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.namespace_policies",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def get_rule_row(
    org: str,
    namespace: str,
    policy_id: str,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT {", ".join(_RULE_COLUMNS)}
        FROM loom.namespace_policies FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND policy_id = {{pid:String}} AND tombstoned_at IS NULL
        LIMIT 1
        """,
        {"org": org, "ns": namespace, "pid": policy_id},
    )
    return rows[0] if rows else None


async def list_rules(
    org: str,
    namespace: str,
    *,
    include_disabled: bool = True,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    ch = ch or _default_client()
    enabled_clause = "" if include_disabled else " AND enabled = true"
    return await ch.query_all(
        f"""
        SELECT {", ".join(_RULE_COLUMNS)}
        FROM loom.namespace_policies FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL{enabled_clause}
        ORDER BY policy_id
        """,
        {"org": org, "ns": namespace},
    )


async def tombstone_rule(
    org: str,
    namespace: str,
    policy_id: str,
    *,
    removed_by: str,
    ch: _CHClient | None = None,
) -> bool:
    """Re-insert the live row with `tombstoned_at` set; RMT collapses it."""
    ch = ch or _default_client()
    existing = await get_rule_row(org, namespace, policy_id, ch=ch)
    if existing is None:
        return False
    now = datetime.now(UTC).replace(tzinfo=None)
    row = dict(existing)
    row["updated_at"] = now
    row["tombstoned_at"] = now
    row["created_by"] = existing.get("created_by") or removed_by
    await ch.insert(
        "loom.namespace_policies",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return True


_DECISION_COLUMNS = (
    "ts",
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "operation",
    "request_id",
    "mode",
    "decision",
    "enforced",
    "determining_rules",
    "errors",
    "detail",
    "latency_ms",
)


def decision_row(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    operation: str,
    request_id: str,
    mode: str,
    decision: str,
    enforced: bool,
    determining_rules: list[str],
    errors: list[str],
    detail: dict[str, Any],
    latency_ms: int,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(UTC),
        "org": org,
        "namespace": namespace,
        "agent": agent,
        "user_id": user_id,
        "session_id": session_id,
        "operation": operation,
        "request_id": request_id,
        "mode": mode,
        "decision": decision,
        "enforced": bool(enforced),
        "determining_rules": list(determining_rules),
        "errors": [e[:400] for e in errors][:8],
        "detail": json.dumps(detail, default=str)[:8000],
        "latency_ms": int(latency_ms),
    }


async def insert_decision(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert("loom.policy_decisions", [row])


async def list_decisions(
    org: str,
    namespace: str,
    *,
    limit: int = 100,
    decision: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    ch = ch or _default_client()
    clause = " AND decision = {dec:String}" if decision else ""
    return await ch.query_all(
        f"""
        SELECT {", ".join(_DECISION_COLUMNS)}
        FROM loom.policy_decisions
        WHERE org = {{org:String}} AND namespace = {{ns:String}}{clause}
        ORDER BY ts DESC
        LIMIT {{lim:UInt32}}
        """,
        {"org": org, "ns": namespace, "lim": int(limit), "dec": decision or ""},
    )


__all__ = [
    "decision_row",
    "get_rule_row",
    "insert_decision",
    "list_decisions",
    "list_rules",
    "row_to_rule",
    "rule_to_row",
    "tombstone_rule",
    "upsert_rule",
]
