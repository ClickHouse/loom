"""The operator's extraction policy: extraction_policy.set / get / clear, and
each process's copy that the extraction paths read.

An org admin sets one policy for the whole org and may add one per namespace,
for at most `LOOM_EXTRACTION_POLICY_MAX_NAMESPACES` namespaces; an extraction
runs under both (`domain.extraction_policy.combine`). Every process reads every
live policy at most every `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`, as it reads
the model-call switch, and a change made here is read again at once. A copy
older than `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` is not trusted:
`effective` raises `customer.SwitchUnread`, so an inline extraction is refused
and queued work waits, rather than run without its policy.
"""

from __future__ import annotations

import logging
import time
from typing import NamedTuple

from server.auth import fail_closed
from server.auth.authz import row_in_scope
from server.auth.sharing import PERSONAL_ALIAS
from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import ValidationError
from server.core.polled_copy import PolledCopy
from server.identity.app.org_admin import caller, one_org
from server.llm import customer
from server.memory.api.models_extraction_policy import (
    ExtractionPolicyClearInput,
    ExtractionPolicyGetInput,
    ExtractionPolicyOutput,
    ExtractionPolicySetInput,
    NamespacePolicy,
)
from server.memory.domain.extraction_policy import combine
from server.memory.store import extraction_policy as repo
from server.operations.base import Role
from server.settings import settings


async def _read() -> dict[tuple[str, str], str]:
    # Sequentially consistent: the read after a change made here must see it.
    with own_write_reads():
        rows = await repo.live_rows()
    return {(str(r["org"]), str(r["namespace"])): str(r["policy"]) for r in rows}


# Every live policy by (org, namespace), namespace "" for the org's own.
_policies: PolledCopy[dict[tuple[str, str], str]] = PolledCopy(
    "extraction policy", _read, logging.getLogger(__name__)
)


async def effective(org: str, namespace: str) -> str:
    """The policy an extraction in (org, namespace) runs under; "" when none is
    set. `namespace` is the one the request resolved to: HTTP maps an end
    user's `@me` to their personal collection before any handler runs. Raises
    `customer.SwitchUnread` when this process's copy is too old."""
    policies = await _policies.get(
        refresh_s=settings.model_calls_policy_refresh_seconds,
        max_stale_s=settings.model_calls_policy_max_stale_seconds,
    )
    if policies is None:
        raise customer.SwitchUnread("the extraction policy could not be read; try again")
    return combine(policies.get((org, ""), ""), policies.get((org, namespace), ""))


class CallerPolicy(NamedTuple):
    """The policy in force for (org, namespace), as a report shows it to its caller."""

    # The effective policy for an org admin ("" when none is set); None otherwise.
    policy: str | None
    # Why `policy` is None, or "".
    unavailable: str
    # The reason to list the section as skipped, or "" when it was read or refused cleanly.
    skipped: str


async def for_caller(org: str, namespace: str) -> CallerPolicy:
    """The policy extractions in (org, namespace) run under, read for the caller of a
    report: an org admin reads it, anyone else is told why not. A grant read that fails
    refuses (fails closed) and is reported as unreadable, not as a non-admin's refusal;
    a process copy too old to trust withholds the policy."""
    # Cleared first: a clean refusal restores a note an earlier check in the request left.
    fail_closed.reset()
    if await row_in_scope(org, namespace, Role.ORG_ADMIN):
        try:
            return CallerPolicy(await effective(org, namespace), "", "")
        except customer.SwitchUnread as exc:
            return CallerPolicy(None, "the policy could not be read", type(exc).__name__)
    if fail_closed.unreadable():
        return CallerPolicy(
            None, "the caller's access could not be read", "the caller's access was unreadable"
        )
    return CallerPolicy(None, "only an org admin reads the operator extraction policy", "")


def note_policy_change() -> None:
    """Make the next extraction read the policies again, for a change just made here."""
    _policies.note_change()


def _scope(
    input: ExtractionPolicySetInput | ExtractionPolicyGetInput | ExtractionPolicyClearInput,
) -> tuple[str, str]:
    """(org, namespace) of the row the request names; namespace "" for the org's."""
    one_org(input.org)
    if input.scope == "org":
        return input.org, ""
    # "", "-" and "*" name no namespace. "@me" is an end user's alias, never a row:
    # HTTP resolves it first, so this is defence in depth for in-process callers.
    namespace = input.namespace
    if namespace.strip() in ("", "-", "*", PERSONAL_ALIAS) or namespace != namespace.strip():
        raise ValidationError(f"scope 'namespace' needs one namespace, got {namespace!r}")
    return input.org, namespace


async def _write(org: str, namespace: str, policy: str) -> None:
    await repo.insert_row(
        {
            "org": org,
            "namespace": namespace,
            "policy": policy,
            "updated_by": caller()[0],
            "updated_at": naive_utc_now(),
            "deleted": int(not policy),
            "version": time.time_ns(),
        }
    )
    note_policy_change()


async def handle_extraction_policy_set(input: ExtractionPolicySetInput) -> ExtractionPolicyOutput:
    """Set the scope's policy, replacing the one it held. Every process
    extracts under it within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`. The
    passes it turns off are listed in the extraction guide
    (docs/features/memory/extraction.md, "The trade-off");
    `memory.set_from_messages` answers `under_operator_policy: true` while one
    is in force."""
    org, namespace = _scope(input)
    policy = input.policy.strip()
    if not policy:
        raise ValidationError("policy is empty; extraction_policy.clear removes one")
    if namespace:
        await _check_namespace_cap(org, namespace)
    await _write(org, namespace, policy)
    return await _output(org, namespace)


async def _check_namespace_cap(org: str, namespace: str) -> None:
    """Refuse a policy for one more namespace once the org holds
    `LOOM_EXTRACTION_POLICY_MAX_NAMESPACES`; replacing a namespace's policy is
    always allowed. Two concurrent sets for new namespaces may both pass."""
    cap = settings.extraction_policy_max_namespaces
    with own_write_reads():
        if await repo.get(org, namespace) is not None:
            return
        count = await repo.namespace_count(org)
    if count >= cap:
        raise ValidationError(
            f"org {org!r} already has {count} namespace extraction policies, the most "
            f"LOOM_EXTRACTION_POLICY_MAX_NAMESPACES allows ({cap}); "
            "extraction_policy.get with scope 'org' lists them; clear one, or set "
            "the rule in the org's policy"
        )


async def handle_extraction_policy_clear(
    input: ExtractionPolicyClearInput,
) -> ExtractionPolicyOutput:
    """Remove the scope's policy. A namespace's clear leaves the org's in force.
    A scope with no live policy is left as it is: no row is written, so clearing
    names that were never set cannot grow the table every process reads."""
    org, namespace = _scope(input)
    with own_write_reads():
        live = await repo.get(org, namespace)
    if live is not None:
        await _write(org, namespace, "")
    return await _output(org, namespace)


async def handle_extraction_policy_get(input: ExtractionPolicyGetInput) -> ExtractionPolicyOutput:
    """The scope's policy and the one its extractions run under. For scope
    `org`, also every namespace that holds a policy of its own."""
    org, namespace = _scope(input)
    return await _output(org, namespace)


async def _output(org: str, namespace: str) -> ExtractionPolicyOutput:
    with own_write_reads():  # an admin reads right after setting it, on any replica
        row = await repo.get(org, namespace)
        org_row = await repo.get(org, "") if namespace else row
        namespace_rows = [] if namespace else await repo.namespace_rows(org)
    policy = str((row or {}).get("policy") or "")
    org_policy = str((org_row or {}).get("policy") or "")
    ns_policy = policy if namespace else ""
    return ExtractionPolicyOutput(
        org=org,
        scope="namespace" if namespace else "org",
        namespace=namespace,
        policy=policy,
        updated_by=str((row or {}).get("updated_by") or ""),
        updated_at=(row or {}).get("updated_at"),
        effective=combine(org_policy, ns_policy),
        namespace_policies=[
            NamespacePolicy(
                namespace=str(r["namespace"]),
                updated_by=str(r.get("updated_by") or ""),
                updated_at=r.get("updated_at"),
            )
            for r in namespace_rows
        ],
    )
