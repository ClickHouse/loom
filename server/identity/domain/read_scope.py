"""Read scope: how far one read may reach inside a namespace it holds.

Membership is the read boundary (docs/concepts/identities-and-namespaces.md): a token that
holds a namespace reads every row in it, whoever wrote it. That is the right
shape for a team's shared memory. It is the wrong shape for one agent that
serves many end users through one token, because a prompt from one user then
retrieves — and can be induced to echo — another user's memories. A few dozen
prompts can extract a large share of such a memory; user- and session-level
isolation is the mitigation that works.

Loom already stores `user_id` and `session_id` on every row and keeps both in
the sort key of `loom.memory_embeddings`. This module turns them into a
read-time bound:

* the request asks for a scope (`namespace` | `own_user` | `own_session`);
* the token may carry a `read_scope` claim, which is a ceiling — the request
  may narrow further, never widen;
* the token may carry a `read_user_id` claim, which pins the read to one end
  user whatever the body says, and refuses a body that names another.

The result is a pair of predicates (`user_id`, `session_id`) that every
retrieval leg's WHERE carries (server/memory/domain/search_sql._build_where).

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

# Higher = narrower. The effective scope is the narrowest of the request and
# the token's ceiling. The keys are the token claim's values
# (`server.auth.jwt.READ_SCOPES`), widest first.
_NARROWNESS: dict[str, int] = {"namespace": 0, "own_user": 1, "own_session": 2}

SENTINEL = "-"


def narrower(a: str, b: str) -> str:
    """The narrower of two scopes; unknown values count as `namespace`."""
    return a if _NARROWNESS.get(a, 0) >= _NARROWNESS.get(b, 0) else b


def effective_read_scope(requested: str | None, auth: Any) -> str:
    """The request's scope, narrowed by the token's `read_scope` ceiling."""
    req = requested if requested in _NARROWNESS else "namespace"
    claim = (getattr(auth, "read_scope", "") or "").strip() if auth is not None else ""
    return narrower(req, claim if claim in _NARROWNESS else "namespace")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v if v and v != SENTINEL else None


@dataclass(frozen=True)
class ReadFilters:
    scope: str
    user_id: str | None
    session_id: str | None


def resolve_read_filters(
    *,
    requested: str | None,
    user_id: str | None,
    session_id: str | None,
    auth: Any,
) -> ReadFilters:
    """Turn (request scope, body identity, token) into the predicates a read
    must carry.

    * `namespace`: explicit body filters are honoured, nothing is added.
    * `own_user`: `user_id` is required (body, or the token's pin).
    * `own_session`: `session_id` is required; `user_id` is applied when known.

    A token pinned with `read_user_id` overrides an absent body user and
    refuses a different one — silently substituting would hide the client bug
    that put the wrong user in the body.
    """
    from server.core.errors import IdentityIncompleteError

    scope = effective_read_scope(requested, auth)
    uid = _clean(user_id)
    sid = _clean(session_id)

    pinned = (getattr(auth, "read_user_id", "") or "").strip() if auth is not None else ""
    if pinned:
        if uid is not None and uid != pinned:
            from server.core.errors import AuthInsufficientError

            raise AuthInsufficientError(
                f"token is pinned to user_id {pinned!r}; the request named {uid!r}"
            )
        uid = pinned
        # A pinned token never reads another user's rows, whatever the
        # requested scope says.
        scope = narrower(scope, "own_user")

    if scope in ("own_user", "own_session") and uid is None:
        raise IdentityIncompleteError(
            f"read_scope={scope} requires user_id in the request body "
            "(or a token minted with --read-user-id)"
        )
    if scope == "own_session" and sid is None:
        raise IdentityIncompleteError(
            "read_scope=own_session requires session_id in the request body"
        )
    return ReadFilters(scope=scope, user_id=uid, session_id=sid)


def pin_read_filters[M: BaseModel](input: M, auth: Any) -> tuple[M, bool]:
    """`input` with `resolve_read_filters`' predicates pinned on its
    `user_id` / `session_id` / `read_scope`, and whether pinning changed
    anything. A read that runs its own statements beside memory.search (the
    ask pipeline's completion and mask reads) pins first, so each statement
    carries the identity the token allows rather than the body's. Raises
    what `resolve_read_filters` raises."""
    rf = resolve_read_filters(
        requested=getattr(input, "read_scope", "namespace"),
        user_id=getattr(input, "user_id", None),
        session_id=getattr(input, "session_id", None),
        auth=auth,
    )
    update: dict[str, Any] = {}
    if rf.user_id != getattr(input, "user_id", None):
        update["user_id"] = rf.user_id
    if rf.session_id != getattr(input, "session_id", None):
        update["session_id"] = rf.session_id
    requested = getattr(input, "read_scope", None)
    if requested is not None and rf.scope != requested:
        update["read_scope"] = rf.scope
    if not update:
        return input, False
    return input.model_copy(update=update), True
