"""The operator's extraction policy: extraction_policy.set / get / clear, and
each process's copy that the extraction paths read.

An org admin sets one policy for the whole org and may add one per namespace;
an extraction runs under both (`domain.extraction_policy.combine`). Every
process reads every live policy at most every
`LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`, as it reads the model-call switch,
and a change made here is read again at once. A copy older than
`LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` is not trusted: `effective` raises
`customer.SwitchUnread`, so an inline extraction is refused and queued work
waits, rather than run without its policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from server.core.errors import ValidationError
from server.llm import customer
from server.memory.api.models_extraction_policy import (
    ExtractionPolicyClearInput,
    ExtractionPolicyGetInput,
    ExtractionPolicyOutput,
    ExtractionPolicySetInput,
)
from server.memory.domain.extraction_policy import combine
from server.memory.store import extraction_policy as repo
from server.settings import settings

log = logging.getLogger(__name__)

# Every live policy by (org, namespace), namespace "" for the org's own; when it
# was read (monotonic, 0.0 = never), when the next read is due, and when a change
# here last asked for one.
_policies: dict[tuple[str, str], str] = {}
_read_at = 0.0
_next_read = 0.0
_forgot_at = 0.0
_lock = asyncio.Lock()


async def effective(org: str, namespace: str) -> str:
    """The policy an extraction in (org, namespace) runs under; "" when none is
    set. Raises `customer.SwitchUnread` when this process's copy is too old."""
    if not org:
        return ""
    if time.monotonic() >= _next_read:
        async with _lock:
            if time.monotonic() >= _next_read:
                await _read()
    if not _read_at or time.monotonic() - _read_at > settings.model_calls_policy_max_stale_seconds:
        raise customer.SwitchUnread(
            "the extraction policy could not be read; extraction waits until it is"
        )
    return combine(_policies.get((org, ""), ""), _policies.get((org, namespace), ""))


async def _read() -> None:
    global _policies, _read_at, _next_read
    from server.core.ch import own_write_reads

    started = time.monotonic()
    try:
        # Sequentially consistent: the read after a change made here must see it.
        with own_write_reads():
            rows = await repo.live_rows()
    except Exception as e:  # noqa: BLE001 — the last copy serves until it is too old
        log.warning("extraction policy: read failed (%s); serving the last copy", e)
    else:
        _policies = {(str(r["org"]), str(r["namespace"])): str(r["policy"]) for r in rows}
        _read_at = time.monotonic()
    # Due again only once this read is over, so a caller that arrives during the
    # first one waits on the lock rather than finding no copy, and a cancelled read
    # leaves the next caller to read. A change made here while it ran may postdate
    # it, so the next caller reads again.
    refresh = settings.model_calls_policy_refresh_seconds
    _next_read = 0.0 if _forgot_at >= started else started + refresh


def forget() -> None:
    """Make the next extraction read the policies again, for a change just made
    here. The copy in hand keeps its age, so a failed read serves it as before."""
    global _next_read, _forgot_at
    _next_read, _forgot_at = 0.0, time.monotonic()


def _reset_for_tests(
    policies: dict[tuple[str, str], str] | None = None, *, read: bool = True
) -> None:
    global _policies, _read_at, _next_read
    now = time.monotonic()
    _policies = dict(policies or {})
    _read_at = now if read else 0.0
    _next_read = now + settings.model_calls_policy_refresh_seconds if read else 0.0


def _scope(input: Any) -> tuple[str, str]:
    """(org, namespace) of the row the request names; namespace "" for the org's."""
    # "", "-" and "*" name no org or namespace.
    org = input.org
    if org.strip() in ("", "-", "*") or org != org.strip():
        raise ValidationError(f"org must name one org, got {org!r}")
    if input.scope == "org":
        return org, ""
    namespace = input.namespace
    if namespace.strip() in ("", "-", "*") or namespace != namespace.strip():
        raise ValidationError(f"scope 'namespace' needs one namespace, got {namespace!r}")
    return org, namespace


def _caller() -> str:
    """The caller's subject; "admin" with auth off (development)."""
    from server.auth.context import current_auth

    auth = current_auth()
    return (auth.subject if auth is not None else "") or "admin"


async def _write(org: str, namespace: str, policy: str) -> None:
    await repo.insert_row(
        {
            "org": org,
            "namespace": namespace,
            "policy": policy,
            "updated_by": _caller(),
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
            "deleted": int(not policy),
            "version": time.time_ns(),
        }
    )
    forget()


async def handle_extraction_policy_set(input: ExtractionPolicySetInput) -> ExtractionPolicyOutput:
    """Set the scope's policy, replacing the one it held. Every process extracts
    under it within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`."""
    org, namespace = _scope(input)
    policy = input.policy.strip()
    if not policy:
        raise ValidationError("policy is empty; extraction_policy.clear removes one")
    await _write(org, namespace, policy)
    return await _output(org, namespace)


async def handle_extraction_policy_clear(
    input: ExtractionPolicyClearInput,
) -> ExtractionPolicyOutput:
    """Remove the scope's policy. A namespace's clear leaves the org's in force."""
    org, namespace = _scope(input)
    await _write(org, namespace, "")
    return await _output(org, namespace)


async def handle_extraction_policy_get(input: ExtractionPolicyGetInput) -> ExtractionPolicyOutput:
    """The scope's policy and the one its extractions run under."""
    org, namespace = _scope(input)
    return await _output(org, namespace)


async def _output(org: str, namespace: str) -> ExtractionPolicyOutput:
    from server.core.ch import own_write_reads

    with own_write_reads():  # an admin reads right after setting it, on any replica
        row: dict[str, Any] | None = await repo.get(org, namespace)
        org_row = row if not namespace else await repo.get(org, "")
    policy = str((row or {}).get("policy") or "")
    return ExtractionPolicyOutput(
        org=org,
        scope="namespace" if namespace else "org",
        namespace=namespace,
        policy=policy,
        updated_by=str((row or {}).get("updated_by") or ""),
        updated_at=(row or {}).get("updated_at"),
        effective=combine(str((org_row or {}).get("policy") or ""), policy if namespace else ""),
    )
