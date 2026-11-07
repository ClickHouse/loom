"""History-policy handlers — set / list / delete / check / decisions."""

from __future__ import annotations

import json
from typing import Any

from server.core.errors import ValidationError, stored_caller_text
from server.identity.api.models_policy import (
    PolicyCheckInput,
    PolicyCheckOutput,
    PolicyDecisionRow,
    PolicyDecisionsInput,
    PolicyDecisionsOutput,
    PolicyDeleteInput,
    PolicyDeleteOutput,
    PolicyListInput,
    PolicyListOutput,
    PolicyRuleOut,
    PolicySetInput,
    PolicySetOutput,
)
from server.identity.app import history_policy as _policy
from server.identity.app.request_scope import request_scope
from server.identity.domain import policy_rules
from server.identity.domain.policy_change import ChangeVerdict, classify_change
from server.identity.store import policy as policy_repo
from server.memory.app.provenance import writer_actor


def _rule_out(row: dict[str, Any]) -> PolicyRuleOut:
    cond = row["condition"]
    return PolicyRuleOut(
        policy_id=row["policy_id"],
        effect=str(row["effect"]),
        actions=list(row["actions"]),
        condition=json.loads(cond) if isinstance(cond, str) else dict(cond),
        description=row.get("description", "") or "",
        enabled=bool(row.get("enabled", True)),
        mode=str(row.get("mode") or "inherit"),
        created_by=row.get("created_by", "") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def _audit_widened(org: str, namespace: str, policy_id: str, reasons: list[str]) -> None:
    """A widening is the change an operator wants to hear about: it gets its
    own audit event type, distinct from the routine `policy.set` row."""
    try:
        from server.observability import audit

        await audit.emit(
            "policy.widened",
            org=org,
            namespace=namespace,
            actor=_actor() or "-",
            subject={"policy_id": policy_id, "reasons": reasons[:8]},
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass


def _actor() -> str:
    from server.auth.context import current_auth

    auth = current_auth()
    return auth.subject if auth is not None else ""


async def handle_policy_set(input: PolicySetInput) -> PolicySetOutput:
    """Create or replace a namespace history-policy rule (effect, actions, condition)."""

    try:
        rule = policy_rules.parse_rule(
            {
                "policy_id": input.policy_id,
                "effect": input.effect,
                "actions": input.actions,
                "condition": input.condition,
                "description": input.description,
                "enabled": input.enabled,
                "mode": input.mode,
            }
        )
    except (ValueError, TypeError) as e:
        raise ValidationError(f"policy rule rejected: {e}") from e

    existing = await policy_repo.get_rule_row(input.org, input.namespace, input.policy_id)
    old_rule = None
    if existing is not None:
        try:
            old_rule = policy_repo.row_to_rule(existing)
        except Exception:  # noqa: BLE001 — a stored rule that no longer parses is "unknown"
            old_rule = None
    change = classify_change(old_rule, rule)
    if existing is not None and old_rule is None:
        change = ChangeVerdict(True, ["previous version unreadable (unknown)"])
    row = policy_repo.rule_to_row(
        input.org,
        input.namespace,
        rule,
        # The writing principal; the body's value only when there is none.
        created_by=writer_actor(fallback=input.created_by),
        created_at=existing.get("created_at") if existing else None,
    )
    await policy_repo.upsert_rule(row)
    _policy.invalidate(input.org, input.namespace)
    if change.widening:
        await _audit_widened(input.org, input.namespace, input.policy_id, change.reasons)
    return PolicySetOutput(
        rule=_rule_out(row),
        leaf_count=rule.leaf_count(),
        widening=change.widening,
        change=change.to_list(),
    )


async def handle_policy_list(input: PolicyListInput) -> PolicyListOutput:
    from server.settings import settings

    rows = await policy_repo.list_rules(
        input.org, input.namespace, include_disabled=input.include_disabled
    )
    return PolicyListOutput(rules=[_rule_out(r) for r in rows], mode=str(settings.policy_mode))


async def handle_policy_delete(input: PolicyDeleteInput) -> PolicyDeleteOutput:
    """Remove a history-policy rule (tombstoned, so its past decisions stay explainable)."""

    existing = await policy_repo.get_rule_row(input.org, input.namespace, input.policy_id)
    removed = await policy_repo.tombstone_rule(
        input.org,
        input.namespace,
        input.policy_id,
        removed_by=writer_actor(fallback=input.removed_by),
    )
    _policy.invalidate(input.org, input.namespace)
    widening = False
    if removed:
        old_rule = None
        try:
            old_rule = policy_repo.row_to_rule(existing) if existing else None
        except Exception:  # noqa: BLE001 — an unparsable old rule
            old_rule = None
        change = (
            classify_change(old_rule, None) if old_rule else ChangeVerdict(True, ["rule deleted"])
        )
        widening = change.widening
        if widening:
            await _audit_widened(input.org, input.namespace, input.policy_id, change.reasons)
    return PolicyDeleteOutput(removed=removed, widening=widening)


async def handle_policy_check(input: PolicyCheckInput) -> PolicyCheckOutput:
    """Dry run: the gate's evaluator against a hypothetical call. Never
    blocks anything; `would_enforce` says what `enforce` mode would do."""
    from server.core.ch import get_client
    from server.identity.app.policy_gate import is_write_shaped
    from server.observability.event_args import project_args
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import settings

    if input.rules is not None:
        try:
            rules = [policy_rules.parse_rule(r) for r in input.rules]
        except (ValueError, TypeError) as e:
            raise ValidationError(f"policy rule rejected: {e}") from e
    else:
        rules = await _policy.rules_for(input.org, input.namespace)

    ctx = policy_rules.CallContext(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent or "-",
        user_id=input.user_id or "-",
        session_id=input.session_id or "-",
        operation=input.operation,
        args=project_args(input.args),
        now=input.at,
    )
    from server.observability.events import get_emitter

    await get_emitter().flush_now()  # same trace the gate would see
    decision = await _policy.evaluate(rules, ctx, get_client())

    op = next((o for o in ALL_OPERATIONS if o.name == input.operation), None)
    write_shaped = is_write_shaped(op) if op is not None else None
    would_enforce = decision.decision == "deny" or (bool(decision.errors) and bool(write_shaped))
    return PolicyCheckOutput(
        decision="deny" if would_enforce else "allow",
        would_enforce=would_enforce,
        mode=str(settings.policy_mode),
        write_shaped=write_shaped,
        determining_rules=decision.determining_rules,
        errors=decision.errors,
        rules=decision.to_detail()["rules"],
        latency_ms=decision.latency_ms,
    )


def _caller_error(error: str) -> str:
    """A recorded decision's error as a reader gets it: `<where>: ` (`policy gate` or a
    leaf's op) and `caller_text`'s shape. A build before that rule recorded the
    store's text after the kind; it is cut here."""
    where, _, rest = error.partition(": ")
    shown = stored_caller_text(rest)
    if shown:
        return f"{where}: {shown}"
    return where


async def handle_policy_decisions(input: PolicyDecisionsInput) -> PolicyDecisionsOutput:
    """List the recorded history-policy decisions for a namespace, optionally one verdict."""

    # A token narrowed to one user sees only that user's decisions.
    rows = await policy_repo.list_decisions(
        input.org,
        input.namespace,
        limit=input.limit,
        decision=input.decision,
        scope=request_scope(),
    )
    return PolicyDecisionsOutput(
        rows=[
            PolicyDecisionRow(
                ts=r["ts"],
                agent=r["agent"],
                user_id=r["user_id"],
                session_id=r["session_id"],
                operation=r["operation"],
                request_id=r["request_id"],
                mode=r["mode"],
                decision=r["decision"],
                enforced=bool(r["enforced"]),
                determining_rules=list(r["determining_rules"]),
                errors=[_caller_error(str(e)) for e in r["errors"]],
                latency_ms=int(r["latency_ms"]),
            )
            for r in rows
        ]
    )


__all__ = [
    "handle_policy_check",
    "handle_policy_decisions",
    "handle_policy_delete",
    "handle_policy_list",
    "handle_policy_set",
]
