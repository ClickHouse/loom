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
    NO_MEMORY_CONTENT,
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
    exact = {k for k in NO_MEMORY_CONTENT if not k.endswith(".")}
    assert exact <= names, sorted(exact - names)
    assert not SCOPED_OPS & exact
    for group in (k for k in NO_MEMORY_CONTENT if k.endswith(".")):
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
        "facts.related",
        "conversation.get",
        "conversation.search",
        "namespace.snapshot",
        "namespace.browse",
        "memory.backlinks",
        "memory.dedup_list",
        "decision.precedents",
        "profile.activity",
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


def test_a_write_naming_another_user_is_admitted_and_its_reads_are_refused() -> None:
    gates.read_scope(OPERATIONS_BY_NAME["memory.set"], PINNED, SimpleNamespace(user_id="u2"))
    with pytest.raises(AuthInsufficientError):
        request_read_filters(PINNED)


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

    return [
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
    assert await reads.get_by_id("00000000-0000-0000-0000-000000000001", scope=U1, ch=ch) is None
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
    from server.memory.app.set_commit import _set_peers

    _bind(PINNED, U1)

    def peer(mid: str, uid: str) -> Any:
        return SimpleNamespace(
            memory_id=mid, memory_key=f"k{mid}", value="v", similarity=0.9, user_id=uid
        )

    result = SimpleNamespace(peers=[peer("1", "u1"), peer("2", "u2")], refined_peers=[])
    assert [p.memory_id for p in _set_peers(result)] == ["1"]
    # A write whose scope cannot be resolved names no peer at all.
    _bind(PINNED, AuthInsufficientError("pinned to u1"))
    assert _set_peers(result) == []


# --------------------------------------------------------------------------
# Caches never carry an answer across scopes
# --------------------------------------------------------------------------


def test_two_asks_filtered_to_different_users_never_share_a_precedent() -> None:
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask.handler import ask_shape_sha256

    set_current_auth(None)

    def shape(**kw: Any) -> str:
        return ask_shape_sha256(
            MemoryAskInput(org="o", namespace="n", agent="a", query="who holds the key?", **kw)
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
