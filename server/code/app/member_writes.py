"""Members of a repository's code namespace write its team file rules.

A code namespace (a `code-*` entry of the code-repo registry) grants its members
`viewer`: the index is read-only reference data. A repository's shared file rules live
in its primary code namespace, and every member may add them — so a member's
`memory.set` there is admitted although the op needs `writer`, and a member may revise
or delete the memories they wrote there. Nothing else widens: not another person's rows,
not pin / lock / redact, not any `code.*` op, not namespace admin, not any other
namespace kind, not a read. Admins keep their full rights through the ordinary gate,
which runs first. A private repository follows the same rule for its own members: the
member test is a live grant plus the ordinary read gate, which enforces the
restricted-namespace rules.

`authorize_op` is the transports' gate; `assert_own_row` is the handlers'
second check, once the row is known.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

# The ops a code-namespace member may call there at `writer`. memory.set writes a new
# rule (or revises one of the member's own); memory.update / memory.delete by key act on
# the member's own rows only.
CODE_MEMBER_WRITE_OPS: frozenset[str] = frozenset({"memory.set", "memory.update", "memory.delete"})

# True for the current request when the transport admitted it through the
# member rule rather than the ordinary gate: the handler then refuses a row
# another principal wrote.
_member_write: ContextVar[bool] = ContextVar("loom_code_member_write", default=False)


def admitted_as_code_member() -> bool:
    return _member_write.get()


async def code_member_may_write(
    auth: AuthInfo | None, op_name: str, org: str | None, namespace: str | None
) -> bool:
    """True when `auth` is a MEMBER of the code namespace `(org, namespace)` and
    `op_name` is one a member may call there.

    Member = a live grant on the namespace (the subject's own, or a service
    account's creator's — a join writes one; an org-readable read alone is not
    membership) AND the ordinary read gate passes (a restricted namespace also
    needs a token that names it). Any other namespace kind, op, or a missing
    identity is False. No widening for reads: this is asked only after the
    ordinary gate refused a write."""
    from server.auth.authz import _is_code_namespace, _lookup_grant, check_authorized
    from server.core.errors import IdentityIncompleteError
    from server.operations.base import Role

    if auth is None or not org or not namespace or op_name not in CODE_MEMBER_WRITE_OPS:
        return False
    if not _is_code_namespace(org, namespace):
        return False
    subject = str(getattr(auth, "subject", "") or "")
    if not subject or await _lookup_grant(org, namespace, subject) is None:
        return False
    try:
        await check_authorized(auth, Role.VIEWER, org=org, namespace=namespace)
    except (PermissionError, IdentityIncompleteError):
        return False
    return True


async def authorize_op(
    auth: AuthInfo | None, op: Any, *, org: str | None, namespace: str | None, **flags: Any
) -> None:
    """The transports' authorization: the ordinary gate at the op's role; when it
    refuses, the member rule may admit the call, and the request is marked so its
    handler refuses another principal's row."""
    from server.auth.authz import check_authorized

    _member_write.set(False)
    try:
        await check_authorized(auth, op.required_role, org=org, namespace=namespace, **flags)
    except PermissionError:
        if not await code_member_may_write(auth, str(getattr(op, "name", "")), org, namespace):
            raise
        _member_write.set(True)


def _author(actor: str) -> str:
    for prefix in ("human:", "process:"):
        if actor.startswith(prefix):
            return actor[len(prefix) :]
    return actor


def refusal_hint(actor: str, *, key: str, ns: str) -> dict[str, Any]:
    """The structured `hint` a refused member write carries (the refusal says how to
    make it right), so any client can show who wrote the rule and what a member can do
    instead."""
    author = _author(actor) or "another member"
    return {
        "code": "TEAM_RULE_NOT_YOURS",
        "author": author,
        "memory_key": key,
        "namespace": ns,
        "next": [
            f"write a corrected rule for the same file with memory.set in {ns!r} (a new key, "
            "files set, tag constraint): it is shown above this one for everyone",
            f"tell {author} or a maintainer of {ns!r}: they can memory.delete {key!r}",
        ],
    }


def assert_own_row(existing: Any, *, op: str) -> None:
    """The second check: under the member rule, a row written by another principal (its
    server-stamped `actor`) is refused 403 — a member revises and deletes only their own
    rules — with a `hint` naming the author, the key and what the member can do instead.
    A no-op for a call the ordinary gate admitted (an admin), or when there is no row."""
    if existing is None:
        return
    assert_own_author(
        str(getattr(existing, "actor", "") or ""),
        key=str(getattr(existing, "memory_key", "") or ""),
        ns=str(getattr(existing, "namespace", "") or ""),
        op=op,
    )


def assert_own_author(actor: str, *, key: str, ns: str, op: str) -> None:
    """`assert_own_row` for what a row's author is known by without the row (a plan only
    the journal holds: its last commit's actor)."""
    if not _member_write.get():
        return
    from fastapi import HTTPException

    from server.memory.app.provenance import writer_actor

    mine = writer_actor()
    if mine and actor == mine:
        return
    hint = refusal_hint(actor, key=key, ns=ns)
    raise HTTPException(
        status_code=403,
        detail={
            "code": "AUTH_INSUFFICIENT",
            "message": (
                f"{op}: the team rule {hint['memory_key']!r} was written by {hint['author']}; "
                "a member of this code namespace may change only the rules they wrote. "
                f"Instead: {hint['next'][0]}; or {hint['next'][1]}."
            ),
            "hint": hint,
        },
    )
