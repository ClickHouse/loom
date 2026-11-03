"""The admission sequence both transports run for a registry op.

authn → authz target → authz → traffic class → history policy → rate limit
→ dispatch, holding one of the org's in-flight slots (`tenant_slot`). HTTP
and MCP each verify the token and render refusals in their own protocol (a
status + detail, an `isError` tool result); the decisions in between are made
here, once:

- `authz_target`: the (org, namespace) a call is authorized against, which
  is also what its handler reads. An empty or "-" field is absent. The org is
  the request's, else the op's plane default (code.* / tracker.*), else the
  verified token's own org. A namespace is never authorized without an org:
  that is a refusal (IDENTITY_INCOMPLETE), not a flat-role pass.
- `with_target`: the input with an absent org / namespace set to the
  authorized target, so the handler reads and writes where the call was
  authorized.
- `authorize`: first-contact provisioning, the role + namespace gate
  (`authorize_op`, raising PermissionError / IdentityIncompleteError), then
  the restricted-access audit row and the first-write registration.
- `policy`: the history policy gate on the authorized identity (raises
  PolicyForbiddenError).
- `read_scope`: the read scope (token ceiling and pin, narrowed by the
  request), resolved once and bound for the handler's store reads.
- `traffic_class` and `rate_limit`: availability controls. Both FAIL OPEN on
  their own errors — an unclassifiable call is `interactive`, a limiter that
  raises admits the call — and each failure is counted and logged once per
  interval, so a broken limiter is visible without logging every request.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from server.code.app.common import plane_namespace_for, plane_org_for
from server.core.errors import IdentityIncompleteError, RateLimitedError
from server.core.naming import named
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo
    from server.core.idempotency import CacheHit, Scope
    from server.core.ratelimit import CheckResult
    from server.operations.base import Operation

log = logging.getLogger("server.transport.gates")

# The org-level ops an end user may call (server/auth/gateway.py):
# make and list collections, read their own standing, and read the static agent guide.
END_USER_ORG_OPS = frozenset(
    {
        "collection.create",
        "collection.list",
        "namespace.list_mine",
        "grants.pending_for_me",
        "mentor.guide",
    }
)

# The code a role / namespace refusal carries on both transports: the one the
# SDKs map (`AuthInsufficient`) and docs/reference/errors.md lists.
ROLE_REFUSAL_CODE = "AUTH_INSUFFICIENT"

# A fail-open gate logs at most once per this many seconds, with the count of
# failures since its last line.
FAIL_OPEN_LOG_INTERVAL_S = 60.0

_FAIL_OPEN_TOTAL: dict[str, int] = {}
_FAIL_OPEN_SINCE_LOG: dict[str, int] = {}
_FAIL_OPEN_LAST_LOG: dict[str, float] = {}


def present(value: Any) -> str | None:
    """A request's identity field as the gates read it: None when absent,
    empty or the "-" sentinel."""
    return named(str(value or "").strip())


def _token_org(auth: AuthInfo | None) -> str | None:
    org = present(getattr(auth, "org", None)) if auth is not None else None
    return None if org == "*" else org


@dataclass(frozen=True, slots=True)
class Target:
    """Where one call is authorized and what it names.

    `org` / `namespace`: the authorization target (`namespace` is None for
    an op that skips the namespace check or names none). `named_namespace`:
    the namespace the request itself names — the rate limiter's namespace
    tier keys on it, so a call that names none never draws a plane's shared
    bucket. `register_namespace`: the namespace a first write registers."""

    org: str | None
    namespace: str | None
    named_namespace: str | None
    register_namespace: str | None


def authz_target(op: Operation, *, org: Any, namespace: Any, auth: AuthInfo | None) -> Target:
    """The authorization target of a call whose request carries `org` /
    `namespace`. Raises IdentityIncompleteError when an authenticated call
    names a namespace with no resolvable org (auth off authorizes nothing)."""
    named_ns = present(namespace)
    resolved_org = present(org) or present(plane_org_for(op.name)) or _token_org(auth)
    resolved_ns = named_ns or present(plane_namespace_for(op.name))
    authz_ns = None if op.skip_namespace_authz else resolved_ns
    if auth is not None and authz_ns is not None and resolved_org is None:
        raise IdentityIncompleteError(
            f"{op.name} names namespace {authz_ns!r} but no org, and the token has no "
            "single org to take it from; put `org` in the request"
        )
    return Target(
        org=resolved_org,
        namespace=authz_ns,
        named_namespace=named_ns,
        register_namespace=resolved_ns,
    )


def with_target(input_obj: Any, target: Target) -> Any:
    """The input with an absent org / namespace set to the target the gate
    authorized; a namespace the op requires is left as sent."""
    fields = type(input_obj).model_fields
    update: dict[str, str] = {}
    # Every memory op requires `org`, so a required org is filled too.
    if target.org and "org" in fields and present(input_obj.org) is None:
        update["org"] = target.org
    # An admin write on "-" (a code repository's access or source) must name
    # its repository, never land on the plane's default index.
    ns_field = fields.get("namespace")
    resolved_ns = target.register_namespace
    if resolved_ns and ns_field is not None and not ns_field.is_required():
        if present(input_obj.namespace) is None:
            update["namespace"] = resolved_ns
    return input_obj.model_copy(update=update) if update else input_obj


async def authorize(
    op: Operation, auth: AuthInfo | None, target: Target, *, transport: str
) -> None:
    """The role + namespace gate for an authenticated call, fail closed, then
    what an admitted call leaves (restricted-access audit, first-write
    registration). No-op without a principal (auth off), but for `@me`."""
    from server.auth.sharing import PERSONAL_ALIAS

    # HTTP maps `@me` to the end user's personal collection before this gate;
    # anywhere else it names nothing, and is never a namespace of its own.
    if target.named_namespace == PERSONAL_ALIAS:
        raise PermissionError(f"{PERSONAL_ALIAS!r} names an end user's personal collection")
    if auth is None:
        _note_admitted(target)
        return
    from server.auth.authz import scope_flags
    from server.auth.gateway import is_end_user
    from server.auth.personal_namespace import ensure_personal_namespace_cached
    from server.code.app.member_writes import authorize_op

    # First contact for a federated subject that never ran self-signup.
    # Idempotent and process-cached; after impersonation, so a trusted caller
    # provisions for the effective user. An end user gets a personal
    # collection instead, the first time a request addresses `@me`.
    if is_end_user(auth):
        # Of the ops no namespace gates, an end user may call only those in
        # END_USER_ORG_OPS: no directory self-service (register, join, browse).
        org_level = op.skip_namespace_authz or op.deployment_scoped
        if org_level and op.name not in END_USER_ORG_OPS:
            raise PermissionError(f"{op.name} is not open to an end user")
    else:
        await ensure_personal_namespace_cached(
            org=auth.org, subject=auth.subject, is_service_account=auth.is_service_account
        )
    await authorize_op(auth, op, org=target.org, namespace=target.namespace, **scope_flags(op))
    _note_admitted(target)
    await _admitted(op, auth, target, transport=transport)


def _note_admitted(target: Target) -> None:
    from server.core import request_work

    bag = request_work.current()
    if bag is not None:
        bag.admitted = (target.org or "-", target.register_namespace or "-")


async def _admitted(op: Operation, auth: AuthInfo, target: Target, *, transport: str) -> None:
    """What an admitted call leaves: one `namespace.restricted_access` audit
    row when the target is restricted, then the first-write registration of
    the namespace the request names (best-effort, after authz so an
    unauthorized write registers nothing; it reads `register_namespace`,
    which is set even for an op that skips the namespace check)."""
    from server.identity.app.namespace_autoregister import maybe_autoregister
    from server.identity.app.restricted_access import note_restricted_access

    await note_restricted_access(
        auth, op=op.name, org=target.org, namespace=target.namespace, transport=transport
    )
    await maybe_autoregister(op, org=target.org, namespace=target.register_namespace, auth=auth)


async def policy(
    op: Operation, target: Target, input_obj: Any, raw_args: dict[str, Any], *, request_id: str
) -> None:
    """The history policy gate, after authorization and before the handler,
    on the identity the handler reads: the authorized org, the namespace the
    request names, and the validated input's agent / user / session. Raises
    PolicyForbiddenError when an enforcing rule denies; a no-op with
    `LOOM_POLICY_MODE=off` or no rule."""
    from server.identity.app.policy_gate import gate

    identity = {
        "org": target.org or "-",
        "namespace": target.register_namespace or "-",
        **{k: str(getattr(input_obj, k, None) or "-") for k in ("agent", "user_id", "session_id")},
    }
    await gate(op, identity, raw_args, request_id=request_id)


def read_scope(op: Operation, auth: AuthInfo | None, input_obj: Any) -> Any:
    """Resolve the call's read scope and bind it for the request
    (`server.identity.domain.read_scope`; the rules are in
    docs/concepts/identities-and-namespaces.md). Returns the input the handler runs with:
    under a narrowed scope it names the bound user (and session).

    Not obvious: a body user the token is not pinned to, or `*` under a narrowed scope, is
    refused for every op, writes included. A scope that does not resolve is refused here for
    a read op (`SCOPED_OPS`) and for an op that would otherwise write or count as user '-'
    (`_would_act_as_nobody`); any other op is admitted and its first memory read raises.
    An op that cannot keep its answer inside a narrowed scope is refused with
    AuthInsufficientError."""
    from server.core.errors import AuthInsufficientError
    from server.identity.domain.read_scope import (
        SCOPED_OPS,
        bind_request_read_filters,
        honours_read_scope,
        resolve_read_filters,
        with_read_identity,
    )

    try:
        result: Any = resolve_read_filters(
            requested=getattr(input_obj, "read_scope", None),
            user_id=getattr(input_obj, "user_id", None),
            session_id=getattr(input_obj, "session_id", None),
            auth=auth,
        )
        narrowed = result.narrowed
    except (AuthInsufficientError, IdentityIncompleteError) as e:
        result, narrowed = e, True
    bind_request_read_filters(auth, result)
    if narrowed and not honours_read_scope(op.name):
        raise AuthInsufficientError(
            f"{op.name} cannot keep its answer inside this token's read scope "
            "(read_scope / read_user_id); call it with a namespace-scoped token"
        )
    # A body naming another user than the token's pin (or `*`) is refused for
    # every op, writes included: a pinned token never writes as someone else.
    if isinstance(result, AuthInsufficientError):
        raise result
    if isinstance(result, Exception):
        if op.name in SCOPED_OPS or _would_act_as_nobody(op):
            raise _read_scope_error(op, result)
        # Bound above: a memory read inside this op raises the same refusal.
        return input_obj
    return with_read_identity(input_obj, result)


def _read_scope_error(op: Operation, e: Exception) -> Exception:
    """Explain an incomplete scope for an op whose input has neither user_id nor
    session_id, so the request body cannot complete it."""
    fields = op.input_model.model_fields
    if not isinstance(e, IdentityIncompleteError) or "user_id" in fields or "session_id" in fields:
        return e
    return IdentityIncompleteError(
        f"{op.name} takes no user_id or session_id, so it cannot complete this token's read "
        "scope; call it with a namespace-scoped token or one pinned to a user "
        "(read_scope own_user with read_user_id)"
    )


def _would_act_as_nobody(op: Operation) -> bool:
    """An op that, admitted under a scope that did not resolve, would write or count
    as user '-': a write whose body carries a user or session, and cost.summary (its
    counts are filtered by the body's user). Code-index ops carry a user field but
    reach no user's rows: their memory reads raise the bound refusal on their own."""
    if op.name.startswith("code."):
        return False
    fields = getattr(op.input_model, "model_fields", {})
    carries_identity = "user_id" in fields or "session_id" in fields
    return carries_identity and (not op.read_only or op.name == "cost.summary")


def _fail_open(gate: str, op_name: str, err: BaseException) -> None:
    """Count one fail-open and log it at most once per interval per gate."""
    _FAIL_OPEN_TOTAL[gate] = _FAIL_OPEN_TOTAL.get(gate, 0) + 1
    _FAIL_OPEN_SINCE_LOG[gate] = _FAIL_OPEN_SINCE_LOG.get(gate, 0) + 1
    now = time.monotonic()
    last = _FAIL_OPEN_LAST_LOG.get(gate)
    if last is None or now - last >= FAIL_OPEN_LOG_INTERVAL_S:
        n = _FAIL_OPEN_SINCE_LOG.pop(gate, 1)
        _FAIL_OPEN_LAST_LOG[gate] = now
        log.warning(
            "%s failed open (%d call(s) since the last line; latest %s): %s",
            gate,
            n,
            op_name,
            err,
        )


def fail_open_counts() -> dict[str, int]:
    """Fail-opens per gate since the process started."""
    return dict(_FAIL_OPEN_TOTAL)


async def traffic_class(
    op: Operation, auth: AuthInfo | None, target: Target, args: Any
) -> tuple[str, str]:
    """The call's traffic class and where it came from, published on the
    request context. An anonymous call reads only the process cache, never
    the namespace registry. Fails open to `interactive`."""
    from server.identity.app.traffic_class_registry import classify, classify_cached
    from server.observability.traffic_class import set_current_traffic_class

    try:
        if auth is None:
            cls, source = classify_cached(auth, target.org, target.namespace, args)
        else:
            cls, source = await classify(auth, target.org, target.namespace, args)
    except Exception as e:  # noqa: BLE001 — FAILS OPEN: classification never refuses a call
        _fail_open("traffic_class", op.name, e)
        cls, source = "interactive", "default"
    set_current_traffic_class(cls, source)
    return cls, source


def _rate_limit_org(auth: AuthInfo | None, target: Target) -> str | None:
    """The org bucket a call draws: the verified token's org, else the
    target's (a wildcard-org service account). Never a body value alone."""
    return _token_org(auth) or target.org


def rate_limit(
    op: Operation, auth: AuthInfo | None, target: Target, traffic: str
) -> CheckResult | None:
    """The limiter's answer for this call, or None when it does not apply (the
    request names no namespace) or the limiter itself failed (FAILS OPEN)."""
    if target.named_namespace is None:
        return None
    from server.core.ratelimit import get_limiter

    try:
        return get_limiter().check(
            target.named_namespace,
            op.name,
            org=_rate_limit_org(auth, target),
            actor=(auth.subject if auth is not None and auth.subject else None),
            traffic_class=traffic,
        )
    except Exception as e:  # noqa: BLE001 — FAILS OPEN: a limiter error admits the call
        _fail_open("rate_limit", op.name, e)
        return None


def session_end_rate_limit(auth: AuthInfo | None, org: str) -> CheckResult | None:
    """The limiter's answer for a `DELETE /mcp`, which names no namespace: it
    draws run.complete's buckets from a slot of its own per subject, so one
    token's DELETEs cannot use up another's slot; every DELETE still counts
    against the org's shared bucket. FAILS OPEN, as `rate_limit` does."""
    from server.core.ratelimit import get_limiter

    subject = auth.subject if auth is not None and auth.subject else ""
    try:
        return get_limiter().check(
            f"mcp-session-end:{subject}",
            "run.complete",
            org=org or None,
        )
    except Exception as e:  # noqa: BLE001 — FAILS OPEN: a limiter error admits the DELETE
        _fail_open("rate_limit", "run.complete", e)
        return None


# Requests each org has running in this process (org -> count).
_IN_FLIGHT: dict[str, int] = {}


def tenant_inflight_cap() -> int:
    """This process's share of LOOM_TENANT_MAX_CONCURRENT_REQUESTS, divided the way
    the rate limits are (replicas × web workers, at least 1); 0 = no cap."""
    configured = int(settings.tenant_max_concurrent_requests)
    if configured <= 0:
        return 0
    from server.core.ratelimit import effective_divisor

    return max(1, configured // effective_divisor(shared=False))


@contextmanager
def tenant_slot(auth: AuthInfo | None, target: Target | None) -> Iterator[None]:
    """Hold one of the org's in-flight slots while the handler runs, released however
    it ends. RateLimitedError (429 RATE_LIMITED) when the org's share is all in use. The
    org is the verified token's, else the target's, as for the rate limiter."""
    org = _rate_limit_org(auth, target) if target is not None else _token_org(auth)
    cap = tenant_inflight_cap()
    if not org or cap <= 0:
        yield
        return
    if _IN_FLIGHT.get(org, 0) >= cap:
        raise RateLimitedError(
            f"org {org!r} already has {cap} requests running on this server; retry shortly",
            retry_after_s=1,
        )
    _IN_FLIGHT[org] = _IN_FLIGHT.get(org, 0) + 1
    try:
        yield
    finally:
        left = _IN_FLIGHT[org] - 1
        if left > 0:
            _IN_FLIGHT[org] = left
        else:
            del _IN_FLIGHT[org]


@dataclass(frozen=True, slots=True)
class Replay:
    """The idempotency decision for one admitted call. `scope` and
    `body_hash` ride to the row stored after the handler (None: no key, or
    the op is not idempotent or read-only); `conflict` means the key was used with other
    arguments, and `hit` is the stored answer to replay."""

    scope: Scope | None = None
    body_hash: str | None = None
    conflict: bool = False
    hit: CacheHit | None = None
    # With `conflict`: a request with this key is still running (not other arguments).
    in_progress: bool = False


async def idempotency_check(
    op: Operation,
    auth: AuthInfo | None,
    target: Target,
    client_key: str,
    input_obj: Any,
    *,
    unset: tuple[str, ...] = (),
) -> Replay:
    """For an idempotent op sent with a key, once the call is admitted: the
    replay cache holds nothing for a read-only op (its retry just reads
    again), so it costs no lookup and no in-progress claim. Otherwise the
    key scoped to the verified caller (an anonymous call to the target's org)
    and the op, the validated input's hash — `unset` names fields the
    transport filled for the caller — and what the replay cache holds for
    them. The cache fails open (a failed read is a miss)."""
    if not op.idempotent or op.read_only or not client_key:
        return Replay()
    from server.core import idempotency

    scope = idempotency.scope(
        client_key,
        org=auth.org if auth is not None else (target.org or ""),
        subject=auth.subject if auth is not None else "",
        operation=op.name,
    )
    body_hash = idempotency.request_hash(input_obj, unset=unset)
    cached = await idempotency.check(scope.key, body_hash)
    return Replay(
        scope=scope,
        body_hash=body_hash,
        conflict=cached.conflict,
        hit=cached.hit,
        in_progress=cached.in_progress,
    )


IN_PROGRESS_MESSAGE = "a request with this idempotency key is still running; retry once it answers"


def retry_after_s(rl: CheckResult) -> int:
    return max(1, int(rl.retry_after_s + 0.99))
