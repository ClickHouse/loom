"""The read scope is resolved once at admission and every memory reader takes it.

Pure unit: no ClickHouse. Covers the `*` refusal, the admission gate and its
op classification over the whole registry, the scope predicates each store
reader adds, and the caches that must not carry rows across scopes.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError, IdentityIncompleteError
from server.identity.domain.read_scope import (
    NARROW_SAFE_OPS,
    SCOPED_OPS,
    WHOLE_NAMESPACE,
    ReadFilters,
    bind_request_read_filters,
    honours_read_scope,
    request_read_filters,
    resolve_read_filters,
    row_in_read_scope,
    scope_where,
)
from server.memory.store.rows import Identity
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.transport import gates

U1 = ReadFilters(scope="own_user", user_id="u1", session_id=None)
S1 = ReadFilters(scope="own_session", user_id="u1", session_id="s1")


def _auth(**kw: Any) -> AuthInfo:
    base: dict[str, Any] = dict(
        subject="sa:bot", org="acme", namespaces=("n",), roles=(), is_service_account=True
    )
    base.update(kw)
    return AuthInfo(**base)


PINNED = _auth(read_user_id="u1")


@pytest.fixture(autouse=True)
def _clean_context():
    yield
    set_current_auth(None)
    bind_request_read_filters(None, WHOLE_NAMESPACE)


def _bind(auth: AuthInfo | None, result: ReadFilters | Exception) -> None:
    set_current_auth(auth)
    bind_request_read_filters(auth, result)


# --------------------------------------------------------------------------
# The wildcard and the predicates
# --------------------------------------------------------------------------


def test_wildcard_user_is_refused_below_the_namespace() -> None:
    own_user = _auth(read_scope="own_user")
    with pytest.raises(AuthInsufficientError):
        resolve_read_filters(requested=None, user_id="*", session_id=None, auth=own_user)
    with pytest.raises(AuthInsufficientError):
        resolve_read_filters(requested=None, user_id="*", session_id=None, auth=PINNED)
    # Namespace scope keeps the wildcard: the readers' "every user".
    rf = resolve_read_filters(requested=None, user_id="*", session_id=None, auth=_auth())
    assert rf.scope == "namespace" and not rf.narrowed


def test_wildcard_session_is_refused_under_own_session() -> None:
    with pytest.raises(AuthInsufficientError):
        resolve_read_filters(requested="own_session", user_id="u1", session_id="*", auth=None)
    # Under own_user a `*` session stays inside the user's own rows.
    rf = resolve_read_filters(requested=None, user_id="-", session_id="*", auth=PINNED)
    assert rf == ReadFilters(scope="own_user", user_id="u1", session_id="*")


def test_scope_where_binds_the_user_and_the_session() -> None:
    params: dict[str, Any] = {}
    assert scope_where(WHOLE_NAMESPACE, params) == [] and params == {}
    assert scope_where(U1, params, "e") == ["e.user_id = {scope_uid:String}"]
    assert params == {"scope_uid": "u1"}
    params = {}
    assert scope_where(S1, params) == [
        "user_id = {scope_uid:String}",
        "session_id = {scope_sid:String}",
    ]
    assert params == {"scope_uid": "u1", "scope_sid": "s1"}


def test_row_in_read_scope() -> None:
    assert row_in_read_scope(WHOLE_NAMESPACE, "anyone", "-")
    assert row_in_read_scope(U1, "u1", "s9")
    assert not row_in_read_scope(U1, "u2", "s1")
    assert not row_in_read_scope(U1, "-", "s1")  # a row of no user is not the user's
    assert row_in_read_scope(S1, "u1", "s1")
    assert not row_in_read_scope(S1, "u1", "s2")


# --------------------------------------------------------------------------
# The binding
# --------------------------------------------------------------------------


def test_a_binding_serves_only_the_principal_it_was_resolved_for() -> None:
    other = _auth(read_user_id="u2")
    bind_request_read_filters(other, ReadFilters("own_user", "u2", None))
    # A stale binding for another token is never reused: the token alone decides.
    assert request_read_filters(PINNED) == U1
    assert request_read_filters(None) == WHOLE_NAMESPACE


def test_a_bound_refusal_is_raised_by_the_first_read() -> None:
    bind_request_read_filters(PINNED, AuthInsufficientError("pinned to u1"))
    with pytest.raises(AuthInsufficientError):
        request_read_filters(PINNED)


def test_an_unbound_unpinned_own_user_token_is_refused() -> None:
    with pytest.raises(IdentityIncompleteError):
        request_read_filters(_auth(read_scope="own_user"))


# --------------------------------------------------------------------------
# The gate, over the whole registry
# --------------------------------------------------------------------------


def test_every_classified_name_is_a_registered_op() -> None:
    names = {op.name for op in ALL_OPERATIONS}
    assert SCOPED_OPS <= names, sorted(SCOPED_OPS - names)
    exact = {k for k in NARROW_SAFE_OPS if not k.endswith(".")}
    assert exact <= names, sorted(exact - names)
    assert not SCOPED_OPS & exact
    for group in (k for k in NARROW_SAFE_OPS if k.endswith(".")):
        assert any(n.startswith(group) for n in names), group


@pytest.mark.parametrize("op", ALL_OPERATIONS, ids=lambda op: op.name)
def test_a_pinned_token_reaches_only_ops_that_honour_its_scope(op: Any) -> None:
    body = SimpleNamespace(user_id="-", session_id="-")
    if honours_read_scope(op.name):
        gates.read_scope(op, PINNED, body)
        assert request_read_filters(PINNED) == U1
    else:
        with pytest.raises(AuthInsufficientError):
            gates.read_scope(op, PINNED, body)
    # A namespace-scoped token and auth off are never refused for their scope.
    gates.read_scope(op, _auth(), body)
    gates.read_scope(op, None, body)
    assert request_read_filters(None) == WHOLE_NAMESPACE


@pytest.mark.parametrize(
    "name",
    [
        "facts.traverse",  # entity_edges / entity_triples carry no user
        "ops.projection_failures.list",  # failure rows carry no user, and others' keys
        "ops.projection_failures.retry",
        "ops.projection_failures.resolve",
        "facts.related",
        "conversation.get",
        "conversation.search",
        "namespace.snapshot",
        "namespace.browse",
        "memory.backlinks",
        "memory.dedup_list",
        "decision.precedents",
        "profile.activity",
        "erasure.request",
        "erasure.status",
        "erasure.list",
        "audit.list",
        "cost.agent_summary",
        "agents.connected",
        "policy.set",
        "policy.delete",
        "memory_schema.register",
        "memory_schema.propose",
        "memory_schema.approve",
    ],
)
def test_ops_that_cannot_bound_their_answer_are_refused(name: str) -> None:
    assert name in OPERATIONS_BY_NAME
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(OPERATIONS_BY_NAME[name], PINNED, SimpleNamespace())


def test_a_read_naming_another_user_is_refused_at_admission() -> None:
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(OPERATIONS_BY_NAME["memory.list"], PINNED, SimpleNamespace(user_id="u2"))
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(
            OPERATIONS_BY_NAME["memory.bulk_get"], PINNED, SimpleNamespace(user_id="*")
        )


@pytest.mark.parametrize(
    "name",
    [
        "memory.set",
        "memory.bulk_set",
        "memory.delete",
        "memory.pin",
        "memory.lock",
        "memory.touch",
        "session.brief_delete",
        "working_memory.set",
        "profile.set",
        "procedure.add",
        "code.search",
    ],
)
def test_a_pinned_token_never_names_another_user_on_any_op(name: str) -> None:
    op = OPERATIONS_BY_NAME[name]
    for other in ("u2", "*"):
        with pytest.raises(AuthInsufficientError):
            gates.read_scope(op, PINNED, SimpleNamespace(user_id=other, session_id="-"))


def test_a_write_naming_no_user_writes_as_the_pinned_one() -> None:
    from server.memory.api.models_crud import MemorySetInput

    body = MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value="v")
    assert body.user_id == "-"
    out = gates.read_scope(OPERATIONS_BY_NAME["memory.set"], PINNED, body)
    assert out.user_id == "u1" and out.session_id == "-"
    # Under own_session the write also lands in the scope's session.
    own_session = _auth(read_user_id="u1", read_scope="own_session")
    body = body.model_copy(update={"session_id": "s1"})
    out = gates.read_scope(OPERATIONS_BY_NAME["memory.set"], own_session, body)
    assert (out.user_id, out.session_id) == ("u1", "s1")
    # A namespace-scoped token writes what the body says.
    assert gates.read_scope(OPERATIONS_BY_NAME["memory.set"], _auth(), body) is body


def test_a_write_under_an_unresolved_scope_is_refused() -> None:
    """Unpinned it would write as user '-' and could overwrite that user's row."""
    own_user = _auth(read_scope="own_user")
    with pytest.raises(IdentityIncompleteError):
        gates.read_scope(OPERATIONS_BY_NAME["memory.set"], own_user, SimpleNamespace(user_id="-"))
    with pytest.raises(IdentityIncompleteError):
        request_read_filters(own_user)


# Narrow-safe ops that would write or count as user '-' if run under a scope that
# does not resolve: written out, so a change to the predicate or a new op has to
# be decided here rather than follow the rule it is checked against.
_ACTS_AS_A_USER = [
    "conversation.save_message",
    "conversation.unsave_message",
    "cost.record_turns",
    "cost.summary",
    # A document records the session it was written in: under a session ceiling
    # with no session named, it would be written outside what the token reads.
    "knowledge.add",
    "knowledge.bulk_add",
    "knowledge.ingest",
    "knowledge.upsert",
    "memory.bulk_set",
    "memory.delete",
    "memory.import",
    "memory.lock",
    "memory.outcome",
    "memory.pin",
    "memory.set",
    "memory.set_from_messages",
    "memory.touch",
    "memory.unlock",
    "memory.unpin",
    "observe.record",
    "procedure.add",
    "profile.append",
    "profile.replace",
    "profile.set",
    "run.begin",
    "run.complete",
    "session.brief_delete",
    "session.brief_save",
    "working_memory.append",
    "working_memory.delete",
    "working_memory.replace",
    "working_memory.set",
]


def test_the_ops_refused_under_an_unresolved_scope_are_the_listed_ones() -> None:
    from server.identity.domain.read_scope import honours_read_scope

    refused = {
        op.name
        for op in ALL_OPERATIONS
        if honours_read_scope(op.name) and gates._would_act_as_nobody(op)
    }
    assert refused == set(_ACTS_AS_A_USER)


@pytest.mark.parametrize("name", _ACTS_AS_A_USER)
def test_a_session_ceiling_with_no_session_in_the_body_is_refused(name: str) -> None:
    """A token pinned to a user with an own_session ceiling and a body naming no
    session does not resolve; an op that would write or count as user '-' is refused
    rather than run unpinned."""
    token = _auth(read_scope="own_session", read_user_id="u1")
    with pytest.raises((AuthInsufficientError, IdentityIncompleteError)):
        gates.read_scope(OPERATIONS_BY_NAME[name], token, SimpleNamespace(user_id="u1"))


def test_a_scoped_read_runs_as_the_user_its_scope_binds() -> None:
    from server.memory.api.models_crud import MemoryListInput

    body = MemoryListInput(org="o", namespace="n", agent="*")
    assert gates.read_scope(OPERATIONS_BY_NAME["memory.list"], PINNED, body).user_id == "u1"
    # A namespace-scoped token's body is left as it came.
    assert gates.read_scope(OPERATIONS_BY_NAME["memory.list"], _auth(), body) is body


def test_the_gate_refuses_an_unpinned_own_user_token_on_an_unscoped_op() -> None:
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(
            OPERATIONS_BY_NAME["facts.traverse"], _auth(read_scope="own_user"), SimpleNamespace()
        )


# --------------------------------------------------------------------------
# Readers require the scope and apply it
# --------------------------------------------------------------------------


def _readers() -> list[Any]:
    from server.memory.app import curation, profile_search
    from server.memory.store import conflicts, history, reads, session_briefs
    from server.tasks.store import runs

    return [
        reads.live_tally,
        reads.identity_tally,
        runs.run_tally,
        reads.get_by_key,
        reads.get_by_id,
        reads.get_many_by_keys,
        reads.find_related_by_entities,
        reads.list_by_namespace,
        reads.count_by_namespace,
        history.list_history,
        conflicts.list_conflicted,
        conflicts.count_conflicted,
        conflicts.conflicted_keys,
        session_briefs.current,
        session_briefs.by_revision,
        session_briefs.listing,
        curation.fetch_timeline,
        profile_search.search_profile,
    ]


@pytest.mark.parametrize("fn", _readers(), ids=lambda f: f"{f.__module__}.{f.__name__}")
def test_memory_readers_require_a_scope(fn: Any) -> None:
    param = inspect.signature(fn).parameters.get("scope")
    assert param is not None and param.default is inspect.Parameter.empty


class _CH:
    """Records every statement; answers each from `answers` in turn, then empty."""

    def __init__(self, *answers: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = list(answers)

    def _next(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return self._answers.pop(0) if self._answers else []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self._next(sql, params)

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = self._next(sql, params)
        return rows[0] if rows else None


def _bound_to_u1(ch: _CH, *, every: bool = True) -> None:
    hits = [("{scope_uid:String}" in sql and p.get("scope_uid") == "u1") for sql, p in ch.calls]
    assert ch.calls and (all(hits) if every else hits[0]), ch.calls


_WILD = Identity(org="o", namespace="n", agent="*", user_id="*", session_id="*")


async def test_point_and_bulk_reads_carry_the_scope() -> None:
    from server.memory.store import reads

    ch = _CH()
    assert await reads.get_by_key(_WILD, "k", scope=U1, ch=ch) is None
    _bound_to_u1(ch)
    ch = _CH()
    # Another user's id is not found: the bound is on the locating statement.
    assert (
        await reads.get_by_id(
            "00000000-0000-0000-0000-000000000001", org="o", namespace="n", scope=U1, ch=ch
        )
        is None
    )
    _bound_to_u1(ch)
    ch = _CH()
    assert await reads.get_many_by_keys(_WILD, ["k"], scope=U1, ch=ch) == []
    _bound_to_u1(ch)


async def test_listing_and_counts_carry_the_scope_under_a_wildcard_identity() -> None:
    from server.memory.store import reads

    ch = _CH()
    await reads.list_by_namespace(_WILD, scope=U1, ch=ch)
    await reads.count_by_namespace(_WILD, scope=U1, ch=ch)
    _bound_to_u1(ch)


async def test_related_bounds_the_source_and_the_neighbours() -> None:
    from server.memory.store import reads

    ident = Identity(org="o", namespace="n", agent="a", user_id="u1", session_id="-")
    ch = _CH([{"entities": ["acme"]}])
    await reads.find_related_by_entities(ident, "k", scope=U1, ch=ch)
    assert len(ch.calls) == 2
    _bound_to_u1(ch)


async def test_history_conflicts_and_briefs_carry_the_scope() -> None:
    from server.memory.store import conflicts, history, session_briefs

    ch = _CH()
    await history.list_history(_WILD, "k", scope=U1, ch=ch)
    await conflicts.list_conflicted("o", "n", scope=U1, ch=ch)
    await conflicts.count_conflicted("o", "n", scope=U1, ch=ch)
    await conflicts.conflicted_keys("o", "n", scope=U1, ch=ch)
    await session_briefs.current(ch, "o", "n", "b", scope=U1)
    await session_briefs.by_revision(ch, "o", "n", "b", 2, scope=U1)
    await session_briefs.listing(ch, "o", "n", scope=U1)
    _bound_to_u1(ch)


async def test_policy_decisions_carry_the_scope() -> None:
    from server.identity.store import policy

    ch = _CH()
    await policy.list_decisions("o", "n", scope=U1, ch=ch)
    _bound_to_u1(ch)


async def test_timeline_and_the_profile_leg_carry_the_scope() -> None:
    from server.memory.app import curation, profile_search

    ch = _CH()
    await curation.fetch_timeline(org="o", namespace="n", entity="acme", scope=U1, ch=ch)
    await profile_search.search_profile(
        "o", ["n"], "q", top_k=3, lexical=lambda *a: False, scope=U1, ch=ch
    )
    _bound_to_u1(ch)


# --------------------------------------------------------------------------
# Handlers that answer from the identity alone
# --------------------------------------------------------------------------


async def test_working_memory_of_another_user_is_not_found() -> None:
    from server.memory.api.models_working_memory import (
        WorkingMemoryGetInput,
        WorkingMemoryRenderInput,
    )
    from server.memory.app import working_memory

    _bind(PINNED, U1)
    ident = dict(org="o", namespace="n", agent="a", user_id="u2", session_id="-")
    out = await working_memory.handle_working_memory_get(
        WorkingMemoryGetInput(**ident, block_key="scratchpad")
    )
    assert out.found is False
    rendered = await working_memory.handle_working_memory_render(WorkingMemoryRenderInput(**ident))
    assert rendered.blocks == [] and rendered.rendered == ""


async def test_recall_thread_cannot_lift_an_own_session_read() -> None:
    from server.memory.api.models_temporal import MemoryRecallInput
    from server.memory.app.recall import handle_memory_recall

    set_current_auth(None)
    inp = MemoryRecallInput(
        org="o",
        namespace="n",
        agent="a",
        user_id="u1",
        session_id="s1",
        read_scope="own_session",
        thread="s2",
        query="rotate the key",
    )
    with pytest.raises(AuthInsufficientError):
        await handle_memory_recall(inp)


async def test_the_conversation_leg_answers_nothing_under_a_bounded_scope(monkeypatch) -> None:
    from server.knowledge.app import conversations
    from server.memory.app.loom_search import fanout

    async def _boom(_input: Any) -> Any:
        raise AssertionError("the conversation search ran under a bounded scope")

    monkeypatch.setattr(conversations, "handle_conversation_search", _boom)
    out = await fanout._conversation_leg(SimpleNamespace(), U1)
    assert out.hits == []


def test_a_conflict_verdict_names_only_peers_inside_the_scope() -> None:
    from server.memory.app.set_commit import set_peers

    _bind(PINNED, U1)

    def peer(mid: str, uid: str) -> Any:
        return SimpleNamespace(
            memory_id=mid, memory_key=f"k{mid}", value="v", similarity=0.9, user_id=uid
        )

    result = SimpleNamespace(peers=[peer("1", "u1"), peer("2", "u2")], refined_peers=[])
    assert [p.memory_id for p in set_peers(result)] == ["1"]
    # A write whose scope cannot be resolved names no peer at all.
    _bind(PINNED, AuthInsufficientError("pinned to u1"))
    assert set_peers(result) == []


# --------------------------------------------------------------------------
# Caches never carry an answer across scopes
# --------------------------------------------------------------------------


def test_two_asks_filtered_to_different_users_never_share_a_precedent() -> None:
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask.handler import ask_shape_sha256
    from server.settings import settings

    set_current_auth(None)

    def shape(**kw: Any) -> str:
        return ask_shape_sha256(
            MemoryAskInput(org="o", namespace="n", agent="a", query="who holds the key?", **kw),
            settings,
        )

    assert shape(user_id="u1") != shape(user_id="u2")
    assert shape(user_id="u1") != shape()
    assert shape() == shape(user_id="-")


async def test_the_conflict_badge_count_is_cached_per_scope() -> None:
    from server.memory.app import conflicts_ops

    conflicts_ops.reset_conflicts_count_cache()
    seen: list[ReadFilters] = []

    async def count_conflicted(org: str, namespace: str, *, scope: ReadFilters) -> int:
        seen.append(scope)
        return 7 if scope is WHOLE_NAMESPACE else 1

    repo = SimpleNamespace(count_conflicted=count_conflicted)
    assert await conflicts_ops._conflicted_count_cached(repo, "o", "n", WHOLE_NAMESPACE) == 7
    assert await conflicts_ops._conflicted_count_cached(repo, "o", "n", U1) == 1
    assert seen == [WHOLE_NAMESPACE, U1]
    conflicts_ops.reset_conflicts_count_cache()


# --------------------------------------------------------------------------
# memory.ask's raw-turn legs, write verdicts and conflict peers stay in scope
# --------------------------------------------------------------------------


async def test_the_literal_completion_reads_only_the_scopes_turns() -> None:
    from server.memory.app.ask_literal_evidence import fetch_turns_with_literal

    _bind(PINNED, U1)
    ch = _CH()
    await fetch_turns_with_literal(org="o", namespace="n", literal="Alice", limit=5, ch=ch)
    _bound_to_u1(ch)
    _bind(None, WHOLE_NAMESPACE)
    ch = _CH()
    await fetch_turns_with_literal(org="o", namespace="n", literal="Alice", limit=5, ch=ch)
    assert "scope_uid" not in ch.calls[0][0]


async def test_deep_evidence_opens_only_the_scopes_links_turns_and_sessions() -> None:
    from server.memory.app import deep_evidence

    _bind(PINNED, U1)
    ch = _CH([{"memory_key": "k", "evidence_id": "00000000-0000-0000-0000-000000000001"}])
    await deep_evidence.fetch_raw_evidence(
        org="o",
        namespace="n",
        memory_keys=["k"],
        max_chars_per_memory=99,
        max_chars_total=99,
        ch=ch,
    )
    assert len(ch.calls) == 2
    # The links come only from the memory rows the scope reaches.
    assert "memory_id IN (" in ch.calls[0][0]
    _bound_to_u1(ch)
    ch = _CH()
    await deep_evidence.fetch_session_turns(
        org="o",
        namespace="n",
        session_ids=["sess-1"],
        question="q",
        max_chars_per_session=99,
        max_chars_total=99,
        ch=ch,
    )
    _bound_to_u1(ch)


def _peer(mid: str, uid: str, *, pinned: bool = False) -> Any:
    return SimpleNamespace(
        memory_id=mid, memory_key=f"k{mid}", value="v", similarity=0.9, user_id=uid, pinned=pinned
    )


def _set_write(**body: Any) -> Any:
    from server.memory.api.models_crud import MemorySetInput

    inp = MemorySetInput(org="o", namespace="n", agent="a", user_id="u1", value="v", **body)
    return SimpleNamespace(input=inp, retyped_from="", version=1, new_sha="new", memory_id="mine")


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_merge_into_another_users_row_does_not_name_it(monkeypatch, dry_run: bool) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import conflicts as cf
    from server.memory.app import set_commit

    _bind(PINNED, U1)
    theirs = SimpleNamespace(
        memory_id="theirs",
        memory_key="v/secret",
        value="vault 1234",
        agent="a",
        user_id="u2",
        session_id="-",
        evidence_count=3,
        reinforcement_count=3,
        valid_at=None,
    )

    async def _get_by_id(*a: Any, **k: Any) -> Any:
        return theirs

    reinforced: list[str] = []

    async def _reinforce(_input: Any, memory_id: str, **k: Any) -> None:
        reinforced.append(memory_id)

    monkeypatch.setattr(memory_repo, "get_by_id", _get_by_id)
    monkeypatch.setattr(cf, "auto_reinforce_peer", _reinforce)
    monkeypatch.setattr(set_commit, "record_drop", lambda *a, **k: None)
    result = SimpleNamespace(existing_memory_id="theirs", merge_scope="", peers=[], refines=[])
    w = _set_write(memory_key="u/guess", dry_run=dry_run)
    out = await set_commit._set_merge(w, None, None, result, [])
    assert out.conflict_verdict == "merge"
    assert (out.memory_id, out.merged_into_key, out.peers) == ("", "", [])
    assert out.content_sha256 == "new" and reinforced == []
    # The caller's own row is named (and, on a real write, reinforced) as before.
    theirs.user_id = "u1"
    out = await set_commit._set_merge(w, None, None, result, [])
    assert (out.memory_id, out.merged_into_key) == ("theirs", "v/secret")
    assert reinforced == ([] if dry_run else ["theirs"])


async def test_a_pinned_blocker_outside_the_scope_blocks_but_is_not_named(monkeypatch) -> None:
    from fastapi import HTTPException

    from server.memory.app import conflicts as cf
    from server.memory.app import set_commit

    _bind(PINNED, U1)
    result = SimpleNamespace(
        verdict="rejected",
        peers=[_peer("theirs", "u2", pinned=True), _peer("mine-old", "u1")],
        refined_peers=[],
        pinned_blocker_id="theirs",
    )

    async def _check(**k: Any) -> Any:
        return result

    async def _cfg() -> Any:
        return SimpleNamespace(check_kwargs=lambda: {})

    monkeypatch.setattr(cf, "check_on_commit", _check)

    def _w(dry_run: bool) -> Any:
        w = _set_write(memory_key="k", dry_run=dry_run)
        w.serialized_value, w.embedding = "v", [0.1]
        return w

    with pytest.raises(HTTPException) as e:
        await set_commit._set_conflict_verdict(_w(False), _cfg())
    detail = e.value.detail
    assert detail["code"] == "PINNED_CONFLICT"
    assert detail["pinned_blocker_id"] == "" and detail["peer_ids"] == ["mine-old"]
    assert "theirs" not in detail["message"]
    _, _, peers, out = await set_commit._set_conflict_verdict(_w(True), _cfg())
    assert out is not None and out.conflict_verdict == "rejected"
    assert [p.memory_id for p in peers] == ["mine-old"]


def test_a_duplicate_of_another_users_row_does_not_name_it() -> None:
    from server.memory.app.set_commit import _set_dry_run_answer

    _bind(PINNED, U1)
    w = _set_write(memory_key="k", dry_run=True)
    result = SimpleNamespace(
        verdict="duplicate", peers=[_peer("theirs", "u2"), _peer("mine", "u1")], refined_peers=[]
    )
    assert _set_dry_run_answer(w, result, [], "theirs").duplicate_of == ""
    assert _set_dry_run_answer(w, result, [], "mine").duplicate_of == "mine"
    _bind(None, WHOLE_NAMESPACE)
    assert _set_dry_run_answer(w, result, [], "theirs").duplicate_of == "theirs"


async def test_conflicts_list_drops_peers_outside_the_scope() -> None:
    from server.memory.store import conflicts

    located = [
        {
            "memory_id": "m1",
            "memory_key": "k",
            "memory_type": "semantic",
            "version": 2,
            "pinned": 0,
            "created_at": None,
            "conflict_with": ["p-in", "p-out"],
            "sensitivity": "",
        }
    ]
    ch = _CH(located, [{"mid": "p-in"}], [])
    rows = await conflicts.list_conflicted("o", "n", scope=U1, ch=ch)
    assert rows[0]["conflict_with"] == ["p-in"]
    _bound_to_u1(ch, every=False)
    assert "{scope_uid:String}" in ch.calls[1][0]
    # Unbounded, no lookup: every peer stays.
    ch = _CH([dict(located[0])], [])
    rows = await conflicts.list_conflicted("o", "n", scope=WHOLE_NAMESPACE, ch=ch)
    assert rows[0]["conflict_with"] == ["p-in", "p-out"] and len(ch.calls) == 2


def test_code_symbol_attaches_no_lessons_under_a_narrowed_scope() -> None:
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import request_read_narrowed

    _bind(PINNED, U1)
    assert request_read_narrowed(current_auth())
    _bind(PINNED, AuthInsufficientError("pinned to u1"))
    assert request_read_narrowed(current_auth())
    _bind(None, WHOLE_NAMESPACE)
    assert not request_read_narrowed(current_auth())


def _candidate(mid: str, uid: str, value: str, *, pinned: bool = False) -> dict[str, Any]:
    return {
        "memory_id": mid,
        "memory_key": f"k/{mid}",
        "dist": 0.01,
        "pinned": pinned,
        "value": value,
        "agent": "a",
        "user_id": uid,
        "session_id": "-",
    }


async def _verdict(rows: list[dict[str, Any]], value: str) -> Any:
    from server.memory.app.conflicts import check_on_commit

    return await check_on_commit(
        org="o",
        namespace="n",
        agent="a",
        memory_type="semantic",
        memory_key="k/new",
        new_value=value,
        new_embedding=[0.1],
        candidate_rows=rows,
        resolved_threshold=0.85,
        resolved_duplicate_threshold=0.9,
        resolved_policy="flag",
        allowlisted_keys=set(),
    )


async def test_a_bounded_write_treats_another_users_row_as_absent(monkeypatch) -> None:
    """set and bulk_set (and set_from_messages through it) take their verdict
    here: another user's restatement is neither merged into, reinforced nor
    duplicated or marked; only their pinned row still blocks."""
    from server.memory.app import conflicts

    class _NoPinned:
        async def query_all(self, sql, params=None):
            return []  # the bounded writer's separate pinned-row statement

    monkeypatch.setattr(conflicts, "get_client", lambda: _NoPinned())
    _bind(PINNED, U1)
    same = await _verdict([_candidate("theirs", "u2", "vault 1234")], "vault 1234")
    assert same.verdict not in ("merge", "duplicate", "conflict", "rejected"), same
    assert not same.existing_memory_id and not same.duplicate_of_id
    assert all(p.user_id == "u1" for p in same.peers)
    blocked = await _verdict([_candidate("theirs", "u2", "vault 9", pinned=True)], "vault 1234")
    assert blocked.verdict == "rejected" and blocked.peers == []
    # The writer's own restatement still merges, and unbounded writes see every row.
    mine = await _verdict([_candidate("mine", "u1", "vault 1234")], "vault 1234")
    assert (mine.verdict, mine.existing_memory_id) == ("merge", "mine")
    _bind(None, WHOLE_NAMESPACE)
    wide = await _verdict([_candidate("theirs", "u2", "vault 1234")], "vault 1234")
    assert (wide.verdict, wide.existing_memory_id) == ("merge", "theirs")


def test_a_bounded_token_may_lift_its_own_lock_and_pin() -> None:
    # Their lookups are scoped, so another user's row is not found; authz still
    # asks for the op's own role.
    for name in ("memory.lock", "memory.unlock", "memory.unpin", "ops.health"):
        gates.read_scope(OPERATIONS_BY_NAME[name], PINNED, SimpleNamespace(user_id="-"))


def test_every_code_op_is_classified_by_name() -> None:
    # Some code ops read memory, so a new one must be classified on its own.
    assert "code." not in NARROW_SAFE_OPS
    code_ops = {op.name for op in ALL_OPERATIONS if op.name.startswith("code.")}
    unclassified = sorted(n for n in code_ops if n not in NARROW_SAFE_OPS and n not in SCOPED_OPS)
    assert not unclassified, unclassified


def test_a_narrowed_namespace_admin_cannot_start_an_erasure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure's target is a namespace or a user id; its handler checks admin
    authority on it, not the token's user/session pin. A narrowed token is refused at
    admission, before the handler (and its saga) runs."""
    from server.erasure.app import handlers

    started: list[Any] = []

    async def _saga(**kw: Any) -> Any:
        started.append(kw)

    monkeypatch.setattr(handlers, "run_saga", _saga)
    narrowed = _auth(read_scope="own_user", read_user_id="u1", ns_roles=(("ns", "admin"),))
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(OPERATIONS_BY_NAME["erasure.request"], narrowed, SimpleNamespace())
    assert started == []


def test_the_identity_sweep_covers_the_writes_that_reached_user_nobody() -> None:
    assert {"memory.bulk_set", "observe.record", "run.begin", "cost.summary"} <= set(
        _ACTS_AS_A_USER
    )


def _code_ops() -> list[str]:
    from server.identity.domain.read_scope import honours_read_scope

    return sorted(
        op.name
        for op in ALL_OPERATIONS
        if op.name.startswith("code.") and honours_read_scope(op.name)
    )


@pytest.mark.parametrize("name", _code_ops())
def test_code_ops_under_an_unresolved_scope_are_admitted_and_their_memory_reads_refused(
    name: str,
) -> None:
    """A code-index op has a user field and no session field, so under an own_session
    ceiling its scope never resolves; it is admitted (it reaches no user's rows) and a
    memory read inside it raises the bound refusal."""
    token = _auth(read_scope="own_session", read_user_id="u1")
    gates.read_scope(OPERATIONS_BY_NAME[name], token, SimpleNamespace(user_id="u1"))
    with pytest.raises(IdentityIncompleteError):
        request_read_filters(token)


def test_a_scoped_read_under_an_unresolved_scope_is_refused() -> None:
    token = _auth(read_scope="own_session", read_user_id="u1")
    with pytest.raises(IdentityIncompleteError):
        gates.read_scope(OPERATIONS_BY_NAME["memory.get"], token, SimpleNamespace(user_id="u1"))


def test_policy_check_is_refused_to_a_narrowed_token() -> None:
    """It evaluates caller-supplied rules over every user's agent_events."""
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(OPERATIONS_BY_NAME["policy.check"], PINNED, SimpleNamespace(user_id="u1"))


@pytest.mark.parametrize(
    "name", ["policy.set", "policy.delete", "memory_schema.register", "memory_schema.approve"]
)
def test_a_narrowed_token_cannot_set_a_namespace_wide_rule(name: str) -> None:
    with pytest.raises(AuthInsufficientError):
        gates.read_scope(OPERATIONS_BY_NAME[name], PINNED, SimpleNamespace(user_id="u1"))
