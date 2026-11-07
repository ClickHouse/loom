"""The policy gate — runs after the role check, before the handler.

Three modes (`LOOM_POLICY_MODE`):

* `off`     — nothing is read, nothing is written; the gate returns at once.
* `shadow`  — rules are evaluated and every applied decision is appended to
              loom.policy_decisions with `enforced = false`; nothing blocks.
              This is how a deployment measures a rule before it bites.
* `enforce` — a denying rule raises PolicyForbiddenError (403, structured).

A rule's own `mode` overrides the deployment's except `off`:
`shadow` under `enforce` makes one rule log-only; `enforce` under
`shadow` lets one rule graduate on its ledger evidence while the rest keep
measuring. Only rules whose effective mode is enforce block or fail closed.

Failure posture: a rule that could not be evaluated is an
error, not a verdict. In `enforce`, an error on a WRITE-shaped operation
(required role WRITER or above) denies — authorizing a write against
history we could not read would be failing open. An
error on a read allows and is reported, because coupling read availability
to the trace store is the wrong trade. In `shadow`, errors are recorded and
never block.

The gate never applies to `policy.*` itself (an operator must always be
able to fix a rule) or to `ops.health`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.core.background import TaskSet
from server.core.errors import LoomServerError, caller_text
from server.identity.app import history_policy as _policy
from server.identity.domain import policy_rules
from server.operations.base import Operation, Role

log = logging.getLogger(__name__)

_SENTINEL = "-"
_EXEMPT_PREFIXES = ("policy.", "ops.")

# The loop holds only a weak reference to a task: an unreferenced ledger
# write can be collected before it runs. The set keeps it until it is done.
_LEDGER_TASKS = TaskSet()


async def drain(timeout_s: float = 5.0) -> int:
    """Wait for the decision rows still being written (at most `timeout_s`);
    returns how many were in flight. Shutdown calls it before the client
    disconnects, so an enforced denial's row is not lost with the process."""
    live = [t for t in _LEDGER_TASKS if not t.done()]
    if live:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


class PolicyForbiddenError(LoomServerError):
    """403 with the decision attached: which rules denied, per-leaf values,
    and any evaluation errors — "denied by policy" and "denied because the
    check could not run" are distinguishable by `errors`."""

    code = "POLICY_FORBIDDEN"
    status_code = 403

    def __init__(self, message: str, decision: policy_rules.PolicyDecision) -> None:
        super().__init__(message, **decision.to_detail())
        self.decision = decision


def _mode() -> str:
    from server.settings import settings

    return str(settings.policy_mode)


def is_write_shaped(op: Operation) -> bool:
    return op.required_role.value >= Role.WRITER.value


def _record(
    *,
    ctx: policy_rules.CallContext,
    request_id: str,
    mode: str,
    decision: policy_rules.PolicyDecision,
    enforced: bool,
) -> None:
    """Append the decision to the ledger and tick the counter. Fire-and-forget."""
    try:
        from server.observability import metrics as _metrics

        _metrics.record_policy_decision(
            namespace=ctx.namespace,
            mode=mode,
            decision=decision.decision,
            errored=bool(decision.errors),
        )
    except Exception:  # noqa: BLE001 — the metric never changes the decision
        pass
    try:
        from server.identity.store import policy as policy_repo

        row = policy_repo.decision_row(
            org=ctx.org,
            namespace=ctx.namespace,
            agent=ctx.agent,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            operation=ctx.operation,
            request_id=request_id,
            mode=mode,
            decision=decision.decision,
            enforced=enforced,
            determining_rules=decision.determining_rules,
            errors=decision.errors,
            detail=decision.to_detail(),
            latency_ms=decision.latency_ms,
        )

        async def _write() -> None:
            try:
                await policy_repo.insert_decision(row)
            except Exception as e:  # noqa: BLE001 — fire-and-forget: the decision already returned
                log.warning("policy_decisions insert failed: %s", e)

        # Not detached: the insert is this request's write and carries its
        # query tag.
        _LEDGER_TASKS.spawn(_write(), name="policy-decision-row")
    except Exception as e:  # noqa: BLE001 — recording never changes the decision
        log.warning("policy decision record skipped: %s", e)


def context_for(
    op: Operation,
    identity: dict[str, Any],
    raw_args: dict[str, Any] | None,
) -> policy_rules.CallContext:
    from server.auth.context import current_auth
    from server.observability.event_args import project_args

    auth = current_auth()
    return policy_rules.CallContext(
        org=str(identity.get("org", _SENTINEL)),
        namespace=str(identity.get("namespace", _SENTINEL)),
        agent=str(identity.get("agent", _SENTINEL) or _SENTINEL),
        user_id=str(identity.get("user_id", _SENTINEL) or _SENTINEL),
        session_id=str(identity.get("session_id", _SENTINEL) or _SENTINEL),
        operation=op.name,
        args=project_args(raw_args or {}),
        actor=str(getattr(auth, "subject", "") or "") if auth is not None else "",
    )


async def gate(
    op: Operation,
    identity: dict[str, Any],
    raw_args: dict[str, Any] | None,
    *,
    request_id: str = "",
    ch: Any | None = None,
) -> policy_rules.PolicyDecision | None:
    """Decide the call. Returns None when no rule applied (or mode is off);
    raises PolicyForbiddenError in enforce mode when the call is denied."""
    mode = _mode()
    if mode == "off":
        return None
    if op.name.startswith(_EXEMPT_PREFIXES):
        return None
    org = identity.get("org", _SENTINEL)
    ns = identity.get("namespace", _SENTINEL)
    if not org or org == _SENTINEL or not ns or ns == _SENTINEL:
        return None

    ctx = context_for(op, identity, raw_args)
    write = is_write_shaped(op)
    try:
        rules = await _policy.rules_for(str(org), str(ns), ch=ch)
        if not any(r.enabled and r.applies_to(op.name) for r in rules):
            return None
        from server.core.ch import get_client, sequential_reads

        if ch is None:
            # The caller's previous call may still sit in this replica's
            # emitter queue; land it before the leaves read the trace.
            from server.observability.events import get_emitter

            await get_emitter().flush_now()
        # A witness another replica committed is in a sequentially consistent read.
        with sequential_reads():
            decision = await _policy.evaluate(rules, ctx, ch or get_client(), mode)
    except Exception as e:  # noqa: BLE001 — the gate itself failed
        decision = policy_rules.PolicyDecision(
            decision="allow",
            determining_rules=[],
            # The text can carry a statement; the caller's 403 names `caller_text(e)`.
            errors=[f"policy gate: {caller_text(e)}"[:400]],
            verdicts=[],
        )
        log.warning("policy gate failed for %s: %s", op.name, e)

    # Enforcement is per rule: a rule blocks only when ITS
    # effective mode is enforce — the deployment's mode is the default, a
    # rule may shadow under enforce (LOG_ONLY) or enforce under shadow.
    blocks = bool(decision.enforcing_rules)
    # Fail closed on writes when an ENFORCING rule's history could not be read.
    if not blocks and decision.enforcing_errors and write:
        decision = policy_rules.PolicyDecision(
            decision="deny",
            determining_rules=decision.determining_rules,
            errors=decision.errors,
            verdicts=decision.verdicts,
            latency_ms=decision.latency_ms,
            enforcing_rules=[],
            enforcing_errors=decision.enforcing_errors,
        )
        blocks = True
    if not blocks and mode == "enforce" and not decision.verdicts and decision.errors and write:
        # the gate itself failed before any rule ran (rules unreadable)
        decision = policy_rules.PolicyDecision(
            decision="deny",
            determining_rules=[],
            errors=decision.errors,
            verdicts=[],
            latency_ms=decision.latency_ms,
            enforcing_errors=decision.errors,
        )
        blocks = True

    if decision.verdicts or decision.errors:
        _record(
            ctx=ctx,
            request_id=request_id,
            mode=mode,
            decision=decision,
            enforced=blocks,
        )

    if blocks:
        if decision.enforcing_rules:
            msg = f"{op.name} denied by policy {', '.join(decision.enforcing_rules)} on {org}/{ns}"
        else:
            msg = (
                f"{op.name} denied: its history policy could not be evaluated "
                f"({len(decision.enforcing_errors)} error(s)); writes fail closed"
            )
        raise PolicyForbiddenError(msg, decision)
    return decision


__all__ = ["PolicyForbiddenError", "context_for", "drain", "gate", "is_write_shaped"]
