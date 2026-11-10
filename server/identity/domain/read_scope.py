"""Read scope: how far one read may reach inside a namespace it holds
(`namespace` | `own_user` | `own_session`). Why it exists and what each op does under it:
docs/concepts/identities-and-namespaces.md#read-scope-inside-a-namespace.

* The token's `read_scope` claim is a ceiling: a request may narrow it, never widen it.
* A `read_user_id` claim pins the read to one user and refuses a body naming another.
* The result is a (`user_id`, `session_id`) pair of predicates. Both transports resolve it
  once at admission (`server.transport.gates.read_scope`); every store reader of memory
  rows takes it as a required `scope` argument, so no op can skip it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from server.core.errors import AuthInsufficientError, IdentityIncompleteError
from server.core.naming import named

# Higher = narrower. The effective scope is the narrowest of the request and
# the token's ceiling. The keys are the token claim's values
# (`server.auth.jwt.READ_SCOPES`), widest first.
_NARROWNESS: dict[str, int] = {"namespace": 0, "own_user": 1, "own_session": 2}


def narrower(a: str, b: str) -> str:
    """The narrower of two scopes; unknown values count as `namespace`."""
    return a if _NARROWNESS.get(a, 0) >= _NARROWNESS.get(b, 0) else b


def effective_read_scope(requested: str | None, auth: Any) -> str:
    """The request's scope, narrowed by the token's `read_scope` ceiling."""
    req = requested if requested in _NARROWNESS else "namespace"
    claim = (getattr(auth, "read_scope", "") or "").strip() if auth is not None else ""
    return narrower(req, claim if claim in _NARROWNESS else "namespace")


def _clean(value: str | None) -> str | None:
    return named(str(value).strip()) if value is not None else None


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


# The payload key a stored read scope rides under.
READ_SCOPE_KEY = "read_filters"
# The scope stored for a write whose own read scope was refused: no replay runs under it.
REFUSED_READ_SCOPE = "refused"


def stored_read_scope(scope: ReadFilters) -> dict[str, Any]:
    """The JSON form of `scope` a payload stores, for a replay outside the request."""
    return {"scope": scope.scope, "user_id": scope.user_id, "session_id": scope.session_id}


class RefusedReadScope(ValueError):
    """The writer's read scope was refused, so no replay runs under it."""


def replayed_read_scope(stored: Any) -> ReadFilters:
    """The read scope a replay runs under: the one `stored_read_scope` stored. A
    payload stored before scopes were replays over the whole namespace, as it always
    did (no write was scoped then). A stored scope that is not a complete one raises
    ValueError, and one stored as refused `RefusedReadScope`, so the replay fails
    rather than reading wider."""
    if stored is None:
        return WHOLE_NAMESPACE
    if not isinstance(stored, dict):
        raise ValueError(f"stored read scope is incomplete: {stored!r}")
    if stored.get("scope") == REFUSED_READ_SCOPE:
        raise RefusedReadScope("the writer's read scope was refused: no replay runs under it")
    scope = ReadFilters(
        scope=str(stored.get("scope") or ""),
        user_id=stored.get("user_id"),
        session_id=stored.get("session_id"),
    )
    complete = (
        scope.scope == "namespace"
        or (scope.scope == "own_user" and scope.user_id)
        or (scope.scope == "own_session" and scope.user_id and scope.session_id)
    )
    if not complete:
        raise ValueError(f"stored read scope is incomplete: {stored!r}")
    return scope


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


def clear_request_read_filters() -> None:
    """Drop the binding: an MCP batch runs its calls in one context, and a call
    refused before its scope is resolved must not inherit the previous call's."""
    _request_scope.set(None)


@contextmanager
def read_filters_bound(auth: Any, scope: ReadFilters) -> Iterator[None]:
    """Bind `scope` for `auth` inside the block only: a worker replaying a request
    runs it under the scope the request had, and the next one starts clean."""
    token = _request_scope.set((auth, scope))
    try:
        yield
    finally:
        _request_scope.reset(token)


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
        # Saved messages are one user's opt-ins; both legs filter the user's rows.
        "conversation.list_saved_messages",
        "conversation.saved_status",
        "conversation.search_saved_messages",
        # Documents and their knowledge.ingest jobs are the namespace's under any scope, as
        # `knowledge.*` reads them (a job's user is its author); the decision rows carry the
        # scope, and the audit's memory findings are not read under a narrowed one.
        "knowledge.inspect",
        "memory.aggregate",
        "memory.ask",
        # Each retired entry is kept only when its user and session lie inside the scope.
        "memory.auto_retirements",
        "memory.bulk_get",
        "memory.digests",
        "memory.evidence",
        "memory.extraction_retry_mine",
        "memory.extraction_status",
        "memory.extraction_status_mine",
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
        "memory.statuses",
        "memory.timeline",
        # Memory and run counts carry the scope; documents and members are counted as
        # `knowledge.*` and `grants.list` read them. Records (one per key, whoever wrote it)
        # and webhooks (`webhook.list` is refused) are not read under a narrowed scope.
        "namespace.inventory",
        "procedure.list",
        "procedure.match",
        "profile.render",
        "profile.structured",
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

# Ops a narrowed token may call as they are, with the reason: they return no
# other user's memory, either none at all or only what they read under the
# caller's own scope (code.ask, code.search, code.symbol). A key ending in "."
# names an op group. Code ops are listed one by one, so a new one stays refused
# until someone classifies it.
NARROW_SAFE_OPS: dict[str, str] = {
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
    # The tie-break reads the files named by memories inside the caller's read scope.
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
    "knowledge.": "namespace documents: access is the namespace's; the user column is authorship",
    "task.": "the task inbox: tasks, not memory",
    "inbox.": "inbox registrations and policies",
    "grants.": "namespace grants",
    # Not policy.set / policy.delete: a rule is namespace-wide and denies every user's
    # writes, a Refuse the token can't manage (as blacklist.* below). Not policy.check:
    # it evaluates caller-supplied rules over every user's agent_events.
    "policy.list": "history policy rules",
    "policy.decisions": "the caller's own policy decisions",
    # Not cost.agent_summary: it has no user axis, and its session_id grouping lists
    # every user's sessions and spend.
    "cost.summary": "the caller's own cost counters (pinned through its user_id)",
    "cost.budget_status": "the namespace's budget counters",
    "cost.record_turns": "the caller's own model turns (usage rows, no memory)",
    # Not ops.projection_failures.*: a failure row has no user axis, and carries
    # other users' memory keys and payloads.
    "ops.capabilities": "the deployment's capabilities",
    "ops.health": "deployment health",
    "mentor.guide": "the deployment's static agent guide, no memory",
    "help.search": "the deployment's shipped documentation, no memory",
    "help.get": "the deployment's shipped documentation, no memory",
    # Not blacklist.*: a pattern has no user axis (created_by is the token subject,
    # which many users' tokens can share). List, remove and sweep would reach another
    # user's patterns, and an add would be a namespace-wide Refuse the token can't manage.
    # Not memory_schema.register / propose / approve: a schema is namespace-wide and
    # refuses every user's non-matching writes.
    "memory_schema.list": "value schemas",
    # A narrowed token is not told the subject that set the schema (updated_by).
    "profile_schema.get": "operator configuration: property names and descriptions",
    # The same text the session-start pack gives such a token; the writer (updated_by)
    # is not shown. Not guide_history: it lists every revision's writer.
    "namespace.guide_get": "the namespace's conventions",
    "admin.users": "the deployment's users",
    # Not agents.connected: no user axis; it lists every user's agents, their ops,
    # when they were seen and what they cost.
    # Not audit.list: a row's subject carries memory keys, ids and content hashes of
    # any user (and erasure targets), with no user axis to pin.
    "calibration.get": "embedder calibration numbers",
    "ui.vitals": "browser timing beacons",
    # Not erasure.*: an erasure's target is a namespace or a user_id, checked for admin
    # authority only, so a token narrowed to one user would erase every user's rows
    # (request) or see other users' erasures, their user ids and requesters (status,
    # list).
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
    # Writes that answer with what they wrote (ids, versions, counts). Each looks
    # its target row up inside the read scope, so another user's row is absent,
    # and the transport refuses a body naming a user other than the token's pin.
    # Not here: writes to tables keyed without a user (records, decisions,
    # procedure_runs, conversation threads, context blobs), which would fold
    # into or overwrite another user's row.
    "conversation.save_message": "a write: the turn is saved as the pinned user, and a "
    "stored turn another user owns is refused, not claimed",
    "conversation.unsave_message": "a write: only the pinned user's saved row changes; "
    "another user's reads as not found",
    "memory.set": "a write",
    "memory.pin": "a write",
    "memory.bulk_set": "a write",
    "memory.import": (
        "a write: the items land as the pinned user and a key's state is read inside the "
        "scope; it links no category, since a link has no user axis and "
        "memory.assign_category is refused to such a token"
    ),
    "memory.set_from_messages": (
        "a write: the facts it extracted from the caller's messages; a bot rule it "
        "files is a learnings candidate, never a memory: for the token's signed agent "
        "when the token is pinned to one user (`read_user_id`), else for the body's agent"
    ),
    "memory.delete": "a write",
    "memory.touch": "a write",
    "memory.lock": "a write",
    "memory.unlock": "a write",
    "memory.unpin": "a write",
    "memory.outcome": "a write",
    "working_memory.set": "a write",
    "working_memory.append": "a write",
    "working_memory.replace": "a write",
    "working_memory.delete": "a write",
    "profile.set": "a write",
    "profile.append": "a write",
    "profile.replace": "a write",
    "procedure.add": "a write",
    "observe.record": "a write",
    "run.begin": "a write: another user's run_id starts a new run",
    "run.complete": "a write: completes only a run inside the read scope",
    "collection.": "collections and their places in the tree; no user's memory",
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
    if op_name in SCOPED_OPS or op_name in NARROW_SAFE_OPS:
        return True
    return any(k.endswith(".") and op_name.startswith(k) for k in NARROW_SAFE_OPS)
