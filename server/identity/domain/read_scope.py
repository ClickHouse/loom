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

Both transports resolve the scope once at admission
(`server.transport.gates.read_scope`) and bind it for the request; every
store reader that returns memory rows takes it as a required `scope`
argument and adds `scope_where`'s predicates, so no op can skip it.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from server.core.errors import AuthInsufficientError, IdentityIncompleteError

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

    @property
    def narrowed(self) -> bool:
        """Whether this read is bounded below the whole namespace."""
        return self.scope != "namespace"


# The scope of a read the caller's token does not bound: auth off, a worker,
# or a write path that reads the row it is about to replace.
WHOLE_NAMESPACE = ReadFilters(scope="namespace", user_id=None, session_id=None)


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
    scope = effective_read_scope(requested, auth)
    uid = _clean(user_id)
    sid = _clean(session_id)

    pinned = (getattr(auth, "read_user_id", "") or "").strip() if auth is not None else ""
    if pinned:
        if uid is not None and uid != pinned:
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
    # `*` is the readers' "every user / every session" wildcard.
    if (scope != "namespace" and uid == "*") or (scope == "own_session" and sid == "*"):
        raise AuthInsufficientError(f"read_scope={scope} does not allow the '*' wildcard")
    return ReadFilters(scope=scope, user_id=uid, session_id=sid)


def scope_where(scope: ReadFilters, params: dict[str, Any], alias: str = "") -> list[str]:
    """The predicates that keep one statement inside `scope`, with their
    params added to `params`. Nothing under `namespace`: the reader's own body
    filters apply as before. Under `own_user` only the user's rows match (a
    row stamped '-' belongs to no user and is not served, as in search)."""
    if not scope.narrowed:
        return []
    col = f"{alias}." if alias else ""
    parts = [f"{col}user_id = {{scope_uid:String}}"]
    params["scope_uid"] = scope.user_id
    if scope.scope == "own_session":
        parts.append(f"{col}session_id = {{scope_sid:String}}")
        params["scope_sid"] = scope.session_id
    return parts


def scope_and(scope: ReadFilters, params: dict[str, Any], alias: str = "") -> str:
    """`scope_where` as one `AND …` clause to append to a WHERE ('' when unbounded)."""
    return "".join(f" AND {p}" for p in scope_where(scope, params, alias))


def row_in_read_scope(scope: ReadFilters, user_id: Any, session_id: Any) -> bool:
    """Whether a row already fetched (by id, or by a wildcard lookup) lies inside `scope`."""
    if not scope.narrowed:
        return True
    if str(user_id or "") != scope.user_id:
        return False
    return scope.scope != "own_session" or str(session_id or "") == scope.session_id


# The scope the transport resolved for this request, beside the token it was
# resolved for (a binding for another principal is never reused). A refusal
# is kept and raised by the first reader that asks.
_request_scope: ContextVar[tuple[Any, ReadFilters | Exception] | None] = ContextVar(
    "loom_read_scope", default=None
)


def bind_request_read_filters(auth: Any, result: ReadFilters | Exception) -> None:
    _request_scope.set((auth, result))


def request_read_filters(auth: Any) -> ReadFilters:
    """The read scope of the current request. Without a binding for `auth`
    (a handler called outside the transports) the token alone decides: a
    pinned token still reads only its user, an unpinned `own_user` token is
    refused."""
    bound = _request_scope.get()
    if bound is not None and bound[0] is auth:
        if isinstance(bound[1], Exception):
            raise bound[1]
        return bound[1]
    return resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth)


def request_read_narrowed(auth: Any) -> bool:
    """Whether this request reads below the whole namespace. A scope that does
    not resolve counts as narrowed (fail closed)."""
    try:
        return request_read_filters(auth).narrowed
    except (AuthInsufficientError, IdentityIncompleteError):
        return True


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


# Which ops a token bounded below the namespace may call. An op not named here
# is refused under such a token (`honours_read_scope`), so a new op is
# unavailable to it until someone classifies it.
#
# Ops whose every read of memory rows (and of what is derived from them)
# carries the request's read scope.
SCOPED_OPS = frozenset(
    {
        "context.pack",
        "conflicts.list",
        "memory.aggregate",
        "memory.ask",
        "memory.bulk_get",
        "memory.digests",
        "memory.evidence",
        "memory.get",
        "memory.get_by_id",
        "memory.history",
        "memory.list",
        "memory.owner_report",
        "memory.proposals",
        "memory.provenance",
        "memory.recall",
        "memory.related",
        "memory.search",
        "memory.search_trace",
        "memory.stack",
        "memory.timeline",
        "procedure.list",
        "procedure.match",
        "profile.render",
        "search.query",
        "session.brief_delete",
        "session.brief_get",
        "session.brief_list",
        "session.brief_save",
        "working_memory.get",
        "working_memory.render",
    }
)

_CODE_INDEX = "the code index: repository content, no user's memory"

# Ops that return no memory content, with the reason. A key ending in "." names
# an op group. Code ops are listed one by one: some read memory (code.ask,
# code.search, code.symbol), so a new one stays refused until someone
# classifies it.
NO_MEMORY_CONTENT: dict[str, str] = {
    # Memory enrichment searches under the caller's scope. The shared answer cache
    # has no user or session in its key, so a narrowed caller skips it (answers.ask).
    "code.ask": "code answers; memory excerpts read under the caller's scope",
    "code.backport_gap": _CODE_INDEX,
    "code.backport_gaps": _CODE_INDEX,
    "code.bisect_candidates": _CODE_INDEX,
    "code.blame_range": _CODE_INDEX,
    "code.calibrate": _CODE_INDEX,
    "code.callees": _CODE_INDEX,
    "code.callers": _CODE_INDEX,
    "code.context_pack": _CODE_INDEX,
    "code.context_packs": _CODE_INDEX,
    "code.conventions": _CODE_INDEX,
    "code.coverage": _CODE_INDEX,
    "code.coverage.unwatch": _CODE_INDEX,
    "code.coverage.watch": _CODE_INDEX,
    "code.coverage_claim": _CODE_INDEX,
    "code.enclosing": _CODE_INDEX,
    "code.examples": _CODE_INDEX,
    "code.files": _CODE_INDEX,
    "code.first_build": _CODE_INDEX,
    "code.grep": _CODE_INDEX,
    "code.history": _CODE_INDEX,
    "code.impact": _CODE_INDEX,
    "code.index_errors": _CODE_INDEX,
    "code.index_status": _CODE_INDEX,
    "code.map": _CODE_INDEX,
    "code.outcome": _CODE_INDEX,
    "code.outline": _CODE_INDEX,
    "code.path_guards": _CODE_INDEX,
    "code.pr_check": _CODE_INDEX,
    "code.recent_commits": _CODE_INDEX,
    "code.refresh": _CODE_INDEX,
    "code.reindex": _CODE_INDEX,
    "code.repo.add": _CODE_INDEX,
    "code.repo.add_branch": _CODE_INDEX,
    "code.repo.remote_branches": _CODE_INDEX,
    "code.repo.remove": _CODE_INDEX,
    "code.repo.set_access": _CODE_INDEX,
    "code.repo.set_source": _CODE_INDEX,
    "code.repos": _CODE_INDEX,
    "code.review_brief": _CODE_INDEX,
    "code.scip_ingest": _CODE_INDEX,
    "code.scip_status": _CODE_INDEX,
    # The tie-break reads the files named by the scope's own user's memories.
    "code.search": _CODE_INDEX,
    "code.setting_facts": _CODE_INDEX,
    # Attaches no lessons under a narrowed scope.
    "code.symbol": _CODE_INDEX,
    "code.symbol_history": _CODE_INDEX,
    "code.test_gate": _CODE_INDEX,
    "code.test_home": _CODE_INDEX,
    "code.tests_for": _CODE_INDEX,
    "code.trace": _CODE_INDEX,
    "code.verify": _CODE_INDEX,
    "code.verify_citations": _CODE_INDEX,
    # The groups below are planes with no path to memory rows.
    "tracker.": "tracker records: issues and their sources, no user's memory",
    "knowledge.": "namespace documents: the plane has no user or session axis",
    "task.": "the task inbox: tasks, not memory",
    "inbox.": "inbox registrations and policies",
    "grants.": "namespace grants",
    "policy.": "history policy rules and decisions",
    "cost.": "cost and budget counters",
    "ops.": "deployment health and projection failures",
    "blacklist.": "refusal patterns and sweep counts",
    "memory_schema.": "value schemas",
    "admin.users": "the deployment's users",
    "agents.connected": "connected agent names",
    "audit.list": "hash-only audit rows",
    "calibration.get": "embedder calibration numbers",
    "embedding.embed": "vectors of the caller's own text",
    "llm.complete": "a completion of the caller's own prompt",
    "ui.vitals": "browser timing beacons",
    "erasure.request": "starts an erasure; answers with its id",
    "erasure.status": "an erasure's progress counters",
    "erasure.list": "erasure requests and their counters",
    "namespace.get": "the namespace's registration",
    "namespace.list_mine": "the caller's namespaces",
    "namespace.register": "registers a namespace",
    "namespace.join": "a membership request",
    "namespace.leave": "a membership change",
    "namespace.cancel_request": "a membership request",
    "namespace.mark_seen": "a seen-marker",
    "namespace.list_categories": "registered category names",
    "namespace.register_category": "registers a category",
    "namespace.list_ontology": "the registered ontology",
    "namespace.register_ontology": "registers an ontology",
    # Writes that answer with what they wrote (ids, versions, counts); a
    # conflict verdict names only peers inside the read scope, and the
    # transport refuses a body naming a user other than the token's pin
    # (`server.transport.gates.read_scope`).
    "memory.set": "a write",
    "memory.pin": "a write",
    "memory.bulk_set": "a write",
    "memory.set_from_messages": "a write: the facts it extracted from the caller's messages",
    "memory.delete": "a write",
    "memory.touch": "a write",
    "memory.lock": "a write",
    "memory.outcome": "a write",
    "memory.record.write": "a write",
    "working_memory.set": "a write",
    "working_memory.append": "a write",
    "working_memory.replace": "a write",
    "working_memory.delete": "a write",
    "profile.set": "a write",
    "profile.append": "a write",
    "profile.replace": "a write",
    "procedure.add": "a write",
    "procedure.record_run": "a write",
    "observe.record": "a write",
    "run.begin": "a write",
    "run.complete": "a write: the run's own counters",
    "decision.record": "a write",
    "conversation.create": "a write",
    "conversation.append": "a write",
    "context.offload": "a write: stores the caller's own text",
}


def with_read_identity[M: BaseModel](input: M, scope: ReadFilters) -> M:
    """`input` naming the user (and, under `own_session`, the session) a
    narrowed scope binds, so a body that names none reads the caller's own
    rows rather than the '-' identity's. Unchanged under `namespace`."""
    if not scope.narrowed:
        return input
    update: dict[str, Any] = {}
    fields = getattr(type(input), "model_fields", {})
    if "user_id" in fields:
        update["user_id"] = scope.user_id
    if scope.scope == "own_session" and "session_id" in fields:
        update["session_id"] = scope.session_id
    return input.model_copy(update=update) if update else input


def honours_read_scope(op_name: str) -> bool:
    """Whether `op_name` may be served to a token bounded below the namespace."""
    if op_name in SCOPED_OPS or op_name in NO_MEMORY_CONTENT:
        return True
    return any(k.endswith(".") and op_name.startswith(k) for k in NO_MEMORY_CONTENT)
