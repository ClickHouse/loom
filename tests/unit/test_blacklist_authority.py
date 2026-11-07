"""Blacklist authority follows a pattern's reach.

`blacklist.add scope=org|global` needs authority over the pattern's whole
reach — the admin of one namespace must not write a global pattern refusing
every tenant's writes — and `blacklist.remove` never falls back from an
org-scoped lookup to a global one. Here: the 3-scope × 4-role add/remove
matrix, the no-fall-through rule, and the regex bounds. CH-free.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import HTTPException

import server.auth.authz as authz
import server.memory.app.blacklist as h
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError, ValidationError
from server.memory.api.models_blacklist import BlacklistAddInput, BlacklistRemoveInput
from server.memory.app.blacklist_guard import REGEX_MAX_LENGTH, compile_regex
from server.operations.base import Role

ORG = "acme"
NS = "ns-a"
ROLES = ("WRITER", "ADMIN", "ORG_ADMIN", "PLATFORM_ADMIN")
SCOPES = ("namespace", "org", "global")

# Who may ADD a pattern of each scope. The namespace column is the op's own
# WRITER gate (the transport applies it); every listed role passes it.
ADD_ALLOWED = {
    "namespace": {"WRITER", "ADMIN", "ORG_ADMIN", "PLATFORM_ADMIN"},
    "org": {"ORG_ADMIN", "PLATFORM_ADMIN"},
    "global": {"PLATFORM_ADMIN"},
}
# Who may REMOVE a pattern of each scope. The op's gate is ADMIN, so WRITER
# never reaches the handler; a namespace pattern additionally needs ADMIN on
# that namespace (hidden, not refused, from anyone else).
REMOVE_ALLOWED = {
    "namespace": {"ADMIN", "ORG_ADMIN", "PLATFORM_ADMIN"},
    "org": {"ORG_ADMIN", "PLATFORM_ADMIN"},
    "global": {"PLATFORM_ADMIN"},
}


def _auth(role: str, org: str = ORG, namespaces=(NS,)) -> AuthInfo:
    return AuthInfo(
        subject=f"{role.lower()}@x",
        org=org,
        namespaces=tuple(namespaces),
        roles=(Role[role],),
        is_service_account=False,
    )


class _Repo:
    """Records inserts and tombstones; `rows` is what find_active_pattern sees."""

    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.tombstoned: list[str] = []
        self.rows: dict[str, dict] = {}
        self.lookups: list[tuple[str, str | None]] = []

    async def insert_row(self, row, **_):
        self.inserted.append(row)

    async def find_active_pattern(self, pattern_id, *, org=None, **_):
        self.lookups.append((pattern_id, org))
        row = self.rows.get(pattern_id)
        if row is None:
            return None
        if org is not None and row["org"] != org:
            return None
        return dict(row)

    async def tombstone_pattern(self, pattern_id, when, **_):
        self.tombstoned.append(pattern_id)


@pytest.fixture(autouse=True)
def _principal_left_as_found():
    """Every test here sets the caller's principal; put it back so the tests
    after this module (in any order) do not inherit an `admin@x`."""
    yield
    set_current_auth(None)


@pytest.fixture
def repo(monkeypatch) -> _Repo:
    r = _Repo()
    monkeypatch.setattr(h.blacklist_repo, "insert_row", r.insert_row)
    monkeypatch.setattr(h.blacklist_repo, "find_active_pattern", r.find_active_pattern)
    monkeypatch.setattr(h.blacklist_repo, "tombstone_pattern", r.tombstone_pattern)

    async def _bump(**_):
        return None

    monkeypatch.setattr(h, "_bump_version", _bump)
    monkeypatch.setattr(h, "invalidate_cache", lambda: None)

    async def _none_stored(scope, org, namespace):
        return 0

    monkeypatch.setattr(h, "scope_program", _none_stored)

    async def _no_grant(org, namespace, subject):
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    return r


def _add(scope: str, pattern: str = "medical", **kw) -> BlacklistAddInput:
    return BlacklistAddInput(org=ORG, namespace=NS, pattern=pattern, scope=scope, **kw)


def _row(pattern_id: str, scope: str, org: str = ORG, namespace: str = NS) -> dict:
    stored_org, stored_ns = h._scope_args(scope, org, namespace)
    return {
        "pattern_id": pattern_id,
        "org": stored_org,
        "namespace": stored_ns,
        "scope": scope,
        "pattern": "medical",
        "pattern_type": "substring",
    }


# ---------------------------------------------------------------- add matrix


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("scope", SCOPES)
def test_add_matrix(repo: _Repo, scope: str, role: str) -> None:
    set_current_auth(_auth(role))
    if role in ADD_ALLOWED[scope]:
        out = asyncio.run(h.handle_blacklist_add(_add(scope)))
        assert out.pattern_id and len(repo.inserted) == 1
        assert repo.inserted[0]["scope"] == scope
        assert (repo.inserted[0]["org"], repo.inserted[0]["namespace"]) == h._scope_args(
            scope, ORG, NS
        )
    else:
        with pytest.raises(AuthInsufficientError) as ei:
            asyncio.run(h.handle_blacklist_add(_add(scope)))
        assert h.required_role_for_scope(scope).name in str(ei.value)
        assert repo.inserted == [], "a refused add must not insert"


def test_add_org_scope_is_bound_to_the_callers_org(repo: _Repo) -> None:
    """An ORG_ADMIN of another org is nothing here; a PLATFORM_ADMIN is."""
    set_current_auth(_auth("ORG_ADMIN", org="other", namespaces=("*",)))
    with pytest.raises(AuthInsufficientError):
        asyncio.run(h.handle_blacklist_add(_add("org")))
    set_current_auth(_auth("PLATFORM_ADMIN", org="other", namespaces=("*",)))
    asyncio.run(h.handle_blacklist_add(_add("org")))
    assert len(repo.inserted) == 1


def test_add_with_auth_off_enforces_nothing(repo: _Repo) -> None:
    set_current_auth(None)
    asyncio.run(h.handle_blacklist_add(_add("global")))
    assert len(repo.inserted) == 1


# ------------------------------------------------------------- remove matrix


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("scope", SCOPES)
def test_remove_matrix(repo: _Repo, scope: str, role: str) -> None:
    repo.rows["p1"] = _row("p1", scope)
    set_current_auth(_auth(role))
    inp = BlacklistRemoveInput(org=ORG, pattern_id="p1")
    if role in REMOVE_ALLOWED[scope]:
        out = asyncio.run(h.handle_blacklist_remove(inp))
        assert out.removed is True and repo.tombstoned == ["p1"]
    elif scope == "namespace":
        # WRITER is stopped by the op's ADMIN gate before the handler; from
        # the handler's side the row is simply not theirs to see.
        out = asyncio.run(h.handle_blacklist_remove(inp))
        assert out.removed is False and repo.tombstoned == []
    else:
        with pytest.raises(AuthInsufficientError) as ei:
            asyncio.run(h.handle_blacklist_remove(inp))
        assert h.required_role_for_scope(scope).name in str(ei.value)
        assert repo.tombstoned == []


def test_remove_never_falls_back_across_scopes(repo: _Repo) -> None:
    """One lookup by id, no org filter, no second lookup: the row's own
    scope decides (never org-scoped miss → global lookup → tombstone)."""
    repo.rows["g1"] = _row("g1", "global")
    set_current_auth(_auth("ADMIN"))
    with pytest.raises(AuthInsufficientError):
        asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org=ORG, pattern_id="g1")))
    assert repo.lookups == [("g1", None)]
    assert repo.tombstoned == []


def test_remove_hides_another_orgs_namespace_pattern(repo: _Repo) -> None:
    repo.rows["p2"] = _row("p2", "namespace", org="other", namespace="ns-x")
    set_current_auth(_auth("ORG_ADMIN"))
    out = asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org=ORG, pattern_id="p2")))
    assert out.removed is False and repo.tombstoned == []


def test_remove_hides_a_namespace_pattern_the_admin_does_not_administer(repo: _Repo) -> None:
    """ADMIN of ns-b in the same org: not a 403 (that confirms the id), a miss."""
    repo.rows["p3"] = _row("p3", "namespace", namespace="ns-a")
    set_current_auth(_auth("ADMIN", namespaces=("ns-b",)))
    out = asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org=ORG, pattern_id="p3")))
    assert out.removed is False and repo.tombstoned == []


def test_remove_refuses_another_orgs_org_pattern_even_to_its_org_admin(repo: _Repo) -> None:
    repo.rows["o1"] = _row("o1", "org", org="other")
    set_current_auth(_auth("ORG_ADMIN"))
    out = asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org=ORG, pattern_id="o1")))
    assert out.removed is False and repo.tombstoned == []
    # Addressed by its own org, the caller's ORG_ADMIN is still of the wrong org.
    set_current_auth(_auth("ORG_ADMIN"))
    with pytest.raises(AuthInsufficientError):
        asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org="other", pattern_id="o1")))
    assert repo.tombstoned == []


def test_remove_unknown_id_is_a_miss(repo: _Repo) -> None:
    set_current_auth(_auth("PLATFORM_ADMIN"))
    out = asyncio.run(h.handle_blacklist_remove(BlacklistRemoveInput(org=ORG, pattern_id="nope")))
    assert out.removed is False and out.removed_at is None


# ------------------------------------------------- a token narrowed to one user


def _pinned(role: str, user: str) -> AuthInfo:
    """A token pinned to `user`; its author stamp is `human:<role>@x`."""
    return AuthInfo(
        subject=f"{role.lower()}@x",
        org=ORG,
        namespaces=(NS,),
        roles=(Role[role],),
        is_service_account=False,
        read_scope="own_user",
        read_user_id=user,
    )


@pytest.mark.parametrize(
    "name", ["blacklist.add", "blacklist.list", "blacklist.remove", "blacklist.sweep"]
)
def test_a_narrowed_token_cannot_reach_any_blacklist_op(name: str) -> None:
    """A backend mints one token per end user under one subject, so `created_by`
    (the subject) cannot tell alice's pattern from bob's: list, remove and sweep
    would reach the other's patterns, and an add would be a namespace-wide Refuse
    its author could never list or remove. Even an ADMIN is refused."""
    from types import SimpleNamespace

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    alice, bob = (
        AuthInfo(
            subject="sa:backend",
            org=ORG,
            namespaces=(NS,),
            roles=(Role.ADMIN,),
            is_service_account=True,
            read_user_id=user,
        )
        for user in ("alice", "bob")
    )
    body = SimpleNamespace(user_id="-", session_id="-")
    for tok in (alice, bob):
        with pytest.raises(AuthInsufficientError):
            gates.read_scope(OPERATIONS_BY_NAME[name], tok, body)
    # A namespace-wide token keeps them.
    wide = AuthInfo(
        subject="sa:backend",
        org=ORG,
        namespaces=(NS,),
        roles=(Role.ADMIN,),
        is_service_account=True,
    )
    gates.read_scope(OPERATIONS_BY_NAME[name], wide, body)


def test_a_narrowed_writers_refusal_names_no_pattern_id_or_reason(monkeypatch) -> None:
    """The matching pattern may be another user's: its id and reason are theirs."""
    import server.memory.app.blacklist_guard as bl

    hit = bl.BlacklistPattern("pid-v", ORG, NS, "x", "substring", "namespace", "V-PRIVATE")

    async def _match(**_):
        return hit

    async def _hit(_pid):
        return None

    monkeypatch.setattr(bl, "find_match", _match)
    monkeypatch.setattr(bl, "note_hit", _hit)
    bodies = []
    for auth in (_auth("WRITER"), _pinned("WRITER", "w")):
        set_current_auth(auth)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(bl.enforce_on_insert(org=ORG, namespace=NS, value="x"))
        assert refused.value.status_code == 422
        assert refused.value.detail["code"] == "BLACKLISTED"
        bodies.append(str(refused.value.detail))
    wide, narrow = bodies
    assert "pid-v" in wide and "V-PRIVATE" in wide
    assert "pid-v" not in narrow and "V-PRIVATE" not in narrow


# -------------------------------------------------------------- sweep gate


def test_sweep_reads_the_rows_scope_and_falls_back_to_global_rows_only(
    repo: _Repo, monkeypatch
) -> None:
    """The row carries `scope`, so the sweep's wider-scope gate can fire; a
    SELECT of only the id would never trigger it."""
    repo.rows["g2"] = _row("g2", "global")
    swept = _record_sweeps(monkeypatch)

    set_current_auth(_auth("WRITER"))
    with pytest.raises(AuthInsufficientError):
        asyncio.run(h.handle_blacklist_sweep(_sweep("g2")))
    assert repo.lookups == [("g2", ORG), ("g2", "*")]
    assert swept == []


def _sweep(pattern_id: str, namespace: str = NS):  # type: ignore[no-untyped-def]
    from server.memory.api.models_blacklist import BlacklistSweepInput

    return BlacklistSweepInput(org=ORG, namespace=namespace, pattern_id=pattern_id)


def _record_sweeps(monkeypatch) -> list[dict]:  # type: ignore[no-untyped-def]
    """Replace the sweep itself; return the list of calls it received."""
    from server.memory.app import blacklist_sweep as bs

    calls: list[dict] = []

    async def fake(**kw):
        calls.append(kw)
        return bs.SweepResult(kw["pattern_id"], 1, 1, False)

    monkeypatch.setattr(bs, "sweep_pattern", fake)
    return calls


# Who may SWEEP with a pattern of each scope. The op's gate is WRITER on the
# body's namespace; the handler then asks for the sweep's own reach: WRITER on
# a namespace pattern's namespace, ORG_ADMIN of this org for an org pattern and
# for a global one (a global pattern sweeps this org only).
SWEEP_ALLOWED = {
    "namespace": {"WRITER", "ADMIN", "ORG_ADMIN", "PLATFORM_ADMIN"},
    "org": {"ORG_ADMIN", "PLATFORM_ADMIN"},
    "global": {"ORG_ADMIN", "PLATFORM_ADMIN"},
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("scope", SCOPES)
def test_sweep_matrix(repo: _Repo, monkeypatch, scope: str, role: str) -> None:
    repo.rows["p"] = _row("p", scope)
    swept = _record_sweeps(monkeypatch)
    set_current_auth(_auth(role))
    if role in SWEEP_ALLOWED[scope]:
        out = asyncio.run(h.handle_blacklist_sweep(_sweep("p")))
        assert out.tombstoned == 1 and len(swept) == 1
    else:
        with pytest.raises(AuthInsufficientError):
            asyncio.run(h.handle_blacklist_sweep(_sweep("p")))
        assert swept == []


@pytest.mark.parametrize("role", ROLES)
def test_sweep_hides_a_namespace_pattern_outside_the_callers_namespaces(
    repo: _Repo, monkeypatch, role: str
) -> None:
    """A principal of ns-a names ns-a in the body (the transport's WRITER gate
    passes) and ns-b's pattern id: the sweep would tombstone ns-b's rows. It
    answers the unknown-id 404 instead, and sweeps nothing."""
    repo.rows["pb"] = _row("pb", "namespace", namespace="ns-b")
    swept = _record_sweeps(monkeypatch)
    set_current_auth(_auth(role, namespaces=("ns-a",)))
    with pytest.raises(HTTPException) as foreign:
        asyncio.run(h.handle_blacklist_sweep(_sweep("pb", namespace="ns-a")))
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(h.handle_blacklist_sweep(_sweep("nope", namespace="ns-a")))
    assert foreign.value.status_code == unknown.value.status_code == 404
    assert foreign.value.detail["code"] == unknown.value.detail["code"] == "NOT_FOUND"
    assert "ns-b" not in str(foreign.value.detail)
    assert swept == []


def test_sweep_of_an_own_namespace_pattern_sweeps_that_namespace(repo: _Repo, monkeypatch) -> None:
    repo.rows["pa"] = _row("pa", "namespace", namespace="ns-b")
    swept = _record_sweeps(monkeypatch)
    set_current_auth(_auth("WRITER", namespaces=("ns-a", "ns-b")))
    asyncio.run(h.handle_blacklist_sweep(_sweep("pa", namespace="ns-a")))
    assert [(c["org"], c["namespace"], c["scope"]) for c in swept] == [(ORG, "ns-b", "namespace")]


def test_sweep_by_another_orgs_org_admin_is_refused(repo: _Repo, monkeypatch) -> None:
    """A global pattern is visible from every org; the ORG_ADMIN of `other`
    naming this org is still nothing here."""
    repo.rows["g3"] = _row("g3", "global")
    swept = _record_sweeps(monkeypatch)
    set_current_auth(_auth("ORG_ADMIN", org="other"))
    with pytest.raises(AuthInsufficientError):
        asyncio.run(h.handle_blacklist_sweep(_sweep("g3")))
    assert swept == []


# ----------------------------------------------------------- regex bounds


def _refusal(pattern: str) -> str | None:
    """Why `compile_regex` refuses the pattern, or None."""
    try:
        compile_regex(pattern)
    except ValidationError as e:
        return str(e)
    return None


@pytest.mark.parametrize(
    "pattern",
    [
        "medical",
        r"\bssn\b",
        r"\d{3}-\d{2}-\d{4}",
        r"[a-z]+@[a-z]+\.com",
        "(ab)+",
        "(?:a|b)+",
        r"(?:[a-z0-9-]+\.)+[a-z]{2,}",
        r"(?i)password\s*[:=]",
        r"[+*]+",
        r"\(+x",
    ],
)
def test_ordinary_regexes_are_accepted(pattern: str) -> None:
    assert _refusal(pattern) is None


def _search_seconds(compiled: Any, text: str) -> float:
    """The fastest of three searches, so one stall on a busy runner does not count."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        compiled.search(text)
        times.append(time.perf_counter() - started)
    return min(times)


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+$", "(a*)*b", "(.*)*x", "(?:x+)*y", "(a|aa)+$", "(a|a)*c", r"q?(\d|\d)*z"],
)
def test_a_backtracking_shape_runs_in_linear_time(pattern: str) -> None:
    """Every regex runs on RE2, so a shape that backtracks exponentially in
    Python's engine searches a hostile value in time linear in it: no shape check
    or probe to slip past (`q?(\\d|\\d)*z` passed both)."""
    assert _refusal(pattern) is None
    compiled = compile_regex(pattern)
    for unit in ("a", "1", "x"):
        n = 20_000
        # From n to 4n a linear search takes about 4 times as long, an exponential one forever.
        assert (
            _search_seconds(compiled, unit * 4 * n + "!")
            < 8 * _search_seconds(compiled, unit * n + "!") + 0.01
        )


@pytest.mark.parametrize("pattern", [r"(a)\1", r"foo(?=bar)", r"(?<!x)y", r"end\Z"])
def test_a_regex_re2_cannot_run_is_refused(pattern: str) -> None:
    reason = _refusal(pattern)
    assert reason is not None and "RE2" in reason


def test_a_regex_whose_program_outgrows_the_cap_is_refused() -> None:
    """A counted class keeps RE2's DFA from caching, so each character costs a step
    per instruction: the program is capped."""
    reason = _refusal("a[ab]{400}c")
    assert reason is not None and "RE2 instructions" in reason
    assert _refusal("x" * REGEX_MAX_LENGTH) is None  # a long literal stays under it


def test_length_and_syntax_bounds() -> None:
    assert _refusal("a" * (REGEX_MAX_LENGTH + 1)) is not None
    assert _refusal("a" * REGEX_MAX_LENGTH) is None
    assert "RE2" in (_refusal("(") or "")


def test_add_refuses_a_bad_regex_with_422_before_it_is_stored(repo: _Repo) -> None:
    set_current_auth(_auth("WRITER"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            h.handle_blacklist_add(_add("namespace", pattern_type="regex", pattern="(?=a)b"))
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "BLACKLIST_PATTERN_INVALID"
    assert repo.inserted == []
    out = asyncio.run(
        h.handle_blacklist_add(_add("namespace", pattern_type="regex", pattern=r"\bssn\b"))
    )
    assert out.pattern_id and len(repo.inserted) == 1


def test_substring_patterns_are_not_regex_validated(repo: _Repo) -> None:
    set_current_auth(_auth("WRITER"))
    out = asyncio.run(h.handle_blacklist_add(_add("namespace", pattern="(a+)+$")))
    assert out.pattern_id and repo.inserted[0]["pattern_type"] == "substring"


async def _loaded(policy: Any = None) -> None:
    return None


def test_cache_refresh_drops_a_stored_regex_re2_cannot_run(monkeypatch) -> None:
    """A row with or without a `scope` column never reaches the hot path."""
    import server.memory.app.blacklist_guard as bl

    class _CH:
        async def query_all(self, *a, **k):
            return [
                {
                    "pattern_id": "bad",
                    "org": "*",
                    "namespace": "*",
                    "pattern": "(?=a)b",
                    "pattern_type": "regex",
                    "scope": "global",
                    "reason": "",
                },
                {
                    "pattern_id": "ok",
                    "org": "*",
                    "namespace": "*",
                    "pattern": r"\bssn\b",
                    "pattern_type": "regex",
                    "scope": "global",
                    "reason": "",
                },
            ]

        async def query_one(self, *a, **k):
            return {"n": 1, "s": 1}

    monkeypatch.setattr(bl, "get_client", lambda: _CH())
    asyncio.run(bl._refresh_cache())
    assert [p.pattern_id for p in bl._cache] == ["ok"]
    # the kept pattern refuses on RE2, through the write path's lookup
    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    hit = asyncio.run(bl.find_match(org="o", namespace="n", value='"my ssn is 123"'))
    assert hit is not None and hit.pattern_id == "ok"
    assert asyncio.run(bl.find_match(org="o", namespace="n", value='"assn"')) is None


def test_a_scopes_regexes_hold_a_bounded_program(repo: _Repo, monkeypatch) -> None:
    """Every regex a write's scopes hold runs on that write: a scope's regexes together
    compile to a bounded program."""
    from server.memory.app.blacklist_guard import REGEX_PROGRAM_PER_SCOPE

    async def _full(scope, org, namespace):
        return REGEX_PROGRAM_PER_SCOPE - 2

    monkeypatch.setattr(h, "scope_program", _full)
    set_current_auth(_auth("WRITER"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(h.handle_blacklist_add(_add("namespace", pattern_type="regex", pattern="xyz+")))
    assert ei.value.detail["code"] == "BLACKLIST_PATTERN_INVALID"
    assert "RE2 instructions" in ei.value.detail["message"]
    assert repo.inserted == []
    # a substring pattern is not a regex and is not counted
    asyncio.run(h.handle_blacklist_add(_add("namespace", pattern="plain")))


def _pattern(pattern_id: str, scope: str, pattern: str, pattern_type: str) -> Any:
    import server.memory.app.blacklist_guard as bl

    compiled = compile_regex(pattern) if pattern_type == "regex" else None
    org, ns = h._scope_args(scope, ORG, NS)
    return bl.BlacklistPattern(pattern_id, org, ns, pattern, pattern_type, scope, "", compiled)


def test_the_matcher_thread_keeps_scope_precedence_and_reads_a_lone_surrogate(
    monkeypatch,
) -> None:
    """With a regex applicable the patterns run on the matcher threads, in the
    cache's order: the namespace pattern wins over a global one that also matches.
    A value with an unpaired surrogate is read, not refused with an error."""
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(
        bl,
        "_cache",
        [
            _pattern("ns", "namespace", r"\bssn\b", "regex"),
            _pattern("global", "global", "ssn", "substring"),
        ],
    )
    hit = asyncio.run(bl.find_match(org=ORG, namespace=NS, value="my ssn \ud800"))
    assert hit is not None and hit.pattern_id == "ns"
    hit = asyncio.run(bl.find_match(org=ORG, namespace="other", value="assn"))
    assert hit is not None and hit.pattern_id == "global"


def test_scope_program_sums_the_regexes_stored_at_exactly_the_scope(monkeypatch) -> None:
    """Read from ClickHouse at add; a stored regex RE2 refuses counts nothing, as at
    cache load."""
    import server.memory.app.blacklist_guard as bl

    stored = {
        ("namespace", ORG, NS): ["x+", "(?=a)b"],
        ("global", *h._scope_args("global", ORG, NS)): ["z+", "w+"],
    }

    async def _scope_regexes(scope, org, namespace, *, ch=None):
        return stored.get((scope, org, namespace), [])

    monkeypatch.setattr(bl._repo, "scope_regexes", _scope_regexes)
    monkeypatch.setattr(bl, "get_client", lambda: None)
    size = compile_regex("x+").programsize
    assert asyncio.run(bl.scope_program("namespace", ORG, NS)) == size
    assert asyncio.run(bl.scope_program("global", *h._scope_args("global", ORG, NS))) == 2 * size
    assert asyncio.run(bl.scope_program("namespace", ORG, "other")) == 0


def test_the_cache_keeps_a_scopes_oldest_regexes_within_its_program(monkeypatch) -> None:
    """Racing adds or an older build can store past a scope's program; every replica
    keeps the oldest that fit, and a row RE2 refuses takes no room."""
    import server.memory.app.blacklist_guard as bl

    size = compile_regex("c+").programsize
    monkeypatch.setattr(bl, "REGEX_PROGRAM_PER_SCOPE", 2 * size)
    rows = [  # as `active_patterns` returns them: oldest first
        {
            "pattern_id": pid,
            "org": ORG,
            "namespace": NS,
            "pattern": pattern,
            "pattern_type": "regex",
            "scope": "namespace",
            "reason": "",
        }
        for pid, pattern in (("c", "c+"), ("bad", "(?=a)b"), ("a", "a+"), ("b", "b+"))
    ]

    class _CH:
        async def query_all(self, *a, **k):
            return rows

        async def query_one(self, *a, **k):
            return {"n": 1, "s": 1}

    monkeypatch.setattr(bl, "get_client", lambda: _CH())
    asyncio.run(bl._refresh_cache())
    assert [p.pattern_id for p in bl._cache] == ["c", "a"]


def test_a_value_past_the_regex_work_budget_is_refused(monkeypatch) -> None:
    """Bytes x instructions bound what one write's regexes scan; a value past it
    is refused, as a matched pattern is, without running them."""
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 90_000)
    pattern = _pattern("r", "namespace", r"\bssn\b", "regex")
    monkeypatch.setattr(bl, "_cache", [pattern])
    budget = bl.REGEX_WORK_BUDGET // pattern.compiled.programsize
    assert asyncio.run(bl.find_match(org=ORG, namespace=NS, value="x" * budget)) is None
    hit = asyncio.run(bl.find_match(org=ORG, namespace=NS, value="x" * (budget + 1)))
    assert hit is not None and hit.pattern_id == "" and "too long" in hit.reason
    assert hit.scope == "namespace"
    asyncio.run(bl.note_hit(""))  # no stored pattern to count against
    # the budget is in bytes: RE2 scans UTF-8
    wide = "\u00e9" * (budget // 2 + 1)
    assert asyncio.run(bl.find_match(org=ORG, namespace=NS, value=wide)) is not None


def test_regexes_match_across_newlines_on_the_matcher_threads(monkeypatch) -> None:
    """`.` matches a newline, as ClickHouse's `match()` does in the sweep, and the
    search runs on the blacklist's threads, never the event loop."""
    import threading

    import server.memory.app.blacklist_guard as bl

    ran_on: list[str] = []
    first_match = bl._first_match

    def _recording(*args: Any) -> Any:
        ran_on.append(threading.current_thread().name)
        return first_match(*args)

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "_first_match", _recording)
    monkeypatch.setattr(bl, "_cache", [_pattern("r", "namespace", "secret.code", "regex")])
    hit = asyncio.run(bl.find_match(org=ORG, namespace=NS, value="secret\ncode"))
    assert hit is not None and hit.pattern_id == "r"
    assert ran_on and all(name.startswith("loom-blacklist") for name in ran_on)


def test_a_value_too_long_to_check_is_refused_saying_so(monkeypatch) -> None:
    """The write path's 422 for a value over the regex budget says it is too long,
    names no pattern, and says the same under a token narrowed to one user."""
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 1_000)
    monkeypatch.setattr(bl, "_cache", [_pattern("r", "namespace", "x+", "regex")])
    for narrowed in (False, True):
        monkeypatch.setattr(
            "server.identity.app.request_scope.request_narrowed", lambda n=narrowed: n
        )
        with pytest.raises(HTTPException) as ei:
            asyncio.run(bl.enforce_on_insert(org=ORG, namespace=NS, value="y" * 5_000))
        detail = ei.value.detail
        assert detail["code"] == "BLACKLISTED" and "too long" in detail["message"]
        assert detail.get("pattern_id", "") == ""
        # the value alone is past the budget: a smaller request would not check it
        assert "shorten the value" in detail["message"]
        # the instruction count sums other principals' patterns
        assert ("RE2 instructions" in detail["message"]) is not narrowed


def test_an_entry_that_fits_alone_is_told_to_go_in_a_smaller_request(monkeypatch) -> None:
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 90_000)
    pattern = _pattern("r", "namespace", r"\bssn\b", "regex")
    monkeypatch.setattr(bl, "_cache", [pattern])
    size = bl.REGEX_WORK_BUDGET // pattern.compiled.programsize * 3 // 5  # each fits alone

    async def two() -> Any:
        bl.begin_request_budget()
        await bl.find_match(org=ORG, namespace=NS, value="x" * size)
        return await bl.find_match(org=ORG, namespace=NS, value="x" * size)

    hit = asyncio.run(two())
    assert hit is not None and hit.pattern_id == ""
    assert "send less in one request" in hit.reason


def test_one_org_holds_at_most_its_share_of_the_matcher_threads(monkeypatch) -> None:
    """A busy org's searches wait their turn, so another org's write finds a thread."""
    import threading

    import server.memory.app.blacklist_guard as bl

    release = threading.Event()
    running: dict[str, int] = {"a": 0, "b": 0}
    most = {"a": 0}
    lock = threading.Lock()

    def _blocking(patterns: Any, value: str, data: Any) -> Any:
        org = value[0]
        with lock:
            running[org] += 1
            most["a"] = max(most["a"], running["a"])
        if org == "a":
            release.wait(10)
        with lock:
            running[org] -= 1
        return None

    def _applies(p: Any, org: str, namespace: str) -> bool:
        return True

    _fresh_slots(monkeypatch)
    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "_first_match", _blocking)
    monkeypatch.setattr(bl, "_applies_to", _applies)
    monkeypatch.setattr(bl, "_cache", [_pattern("r", "namespace", "x+", "regex")])

    async def scene() -> None:
        busy = [
            asyncio.create_task(bl.find_match(org="org-a", namespace=NS, value="a" * 10))
            for _ in range(6)
        ]
        await asyncio.sleep(0.2)
        # org-b's search finishes while org-a's hold their share
        await asyncio.wait_for(bl.find_match(org="org-b", namespace=NS, value="b"), 5)
        release.set()
        await asyncio.gather(*busy)

    asyncio.run(scene())
    assert most["a"] == bl.MATCHERS_PER_ORG
    # an org keeps its one semaphore, every turn back
    assert set(bl._org_slots) == {"org-a", "org-b"}
    assert all(not slots.locked() for slots in bl._org_slots.values())


def _fresh_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns of this test's own: a semaphore a test waited on is bound to its loop."""
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_org_slots", {})
    monkeypatch.setattr(bl, "_thread_slots", asyncio.Semaphore(bl.MATCHER_THREADS))


def _blocking_search(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, int]]:
    """`_first_match` replaced by a search that holds its thread until the returned
    event is set, counting the searches running at once."""
    import threading

    import server.memory.app.blacklist_guard as bl

    release = threading.Event()
    counts = {"running": 0, "most": 0}
    lock = threading.Lock()

    def _blocking(patterns: Any, value: str, data: Any) -> Any:
        with lock:
            counts["running"] += 1
            counts["most"] = max(counts["most"], counts["running"])
        release.wait(10)
        with lock:
            counts["running"] -= 1
        return None

    _fresh_slots(monkeypatch)
    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "_first_match", _blocking)
    monkeypatch.setattr(bl, "_cache", [_pattern("r", "namespace", "x+", "regex")])
    return release, counts


def test_a_cancelled_search_keeps_its_turn_until_its_thread_is_done(monkeypatch) -> None:
    """RE2 cannot be stopped: a cancelled request's search still runs, so its turn is
    given back when the thread is done, and the org never runs more than its share."""
    import server.memory.app.blacklist_guard as bl

    release, counts = _blocking_search(monkeypatch)

    async def scene() -> None:
        first = [
            asyncio.create_task(bl.find_match(org=ORG, namespace=NS, value="x"))
            for _ in range(bl.MATCHERS_PER_ORG)
        ]
        await asyncio.sleep(0.2)
        for task in first:
            task.cancel()
        later = [
            asyncio.create_task(bl.find_match(org=ORG, namespace=NS, value="x"))
            for _ in range(bl.MATCHERS_PER_ORG)
        ]
        await asyncio.sleep(0.2)
        assert counts["running"] == bl.MATCHERS_PER_ORG  # the later ones still wait
        release.set()
        await asyncio.gather(*first, *later, return_exceptions=True)

    asyncio.run(scene())
    assert counts["most"] == bl.MATCHERS_PER_ORG


def test_a_search_that_waits_too_long_for_its_turn_refuses_the_write(monkeypatch) -> None:
    """A write never queues without end behind its org's searches, and never passes
    unchecked: past MATCHER_WAIT_S it is refused 503 OVERLOADED."""
    import server.memory.app.blacklist_guard as bl
    from server.core.errors import OverloadedError

    release, _counts = _blocking_search(monkeypatch)
    monkeypatch.setattr(bl, "MATCHER_WAIT_S", 0.2)

    async def scene() -> None:
        busy = [
            asyncio.create_task(bl.find_match(org=ORG, namespace=NS, value="x"))
            for _ in range(bl.MATCHERS_PER_ORG)
        ]
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(OverloadedError):
                await bl.enforce_on_insert(org=ORG, namespace=NS, value="x")
        finally:
            release.set()
            await asyncio.gather(*busy)
        # every turn is back: the next search runs
        assert await bl.find_match(org=ORG, namespace=NS, value="x") is None

    asyncio.run(scene())


def test_a_blacklist_regex_keeps_no_captures_and_a_small_memory() -> None:
    """A yes/no search needs no capture groups, and each compiled pattern's memory
    (RE2's DFA cache included, 8 MiB by default) is bounded, so a writer's patterns
    hold little resident on every replica."""
    from server.memory.app.blacklist_guard import REGEX_MAX_MEMORY

    compiled = compile_regex("(a)(b)+c")
    assert compiled.groups == 0 and compiled.options.never_capture
    assert compiled.options.max_mem == REGEX_MAX_MEMORY
    assert compiled.search(b"xabbc") is not None  # the write path searches UTF-8 bytes


def test_a_bulk_writes_entries_share_one_regex_budget(monkeypatch) -> None:
    """Each entry of a bulk write is checked in the same request: the budget is the
    request's, so a thousand entries cannot each spend a write's worth. A substring
    pattern that matches still names itself once the regexes are out of budget."""
    import server.memory.app.blacklist_guard as bl

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    regex = _pattern("r", "namespace", "x+", "regex")
    monkeypatch.setattr(bl, "_cache", [regex, _pattern("s", "namespace", "secret", "substring")])
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 3 * 1_000 * regex.compiled.programsize)

    async def _bulk() -> list[Any]:
        bl.begin_request_budget()  # what the transports do at each op
        return [
            await bl.find_match(org=ORG, namespace=NS, value=value)
            for value in (
                "y" * 1_000,
                "y" * 1_000,
                "y" * 1_000,
                "y" * 1_000,
                "a secret" + "y" * 990,
            )
        ]

    hits = asyncio.run(_bulk())
    assert hits[:3] == [None, None, None]
    assert hits[3] is not None and hits[3].pattern_id == "" and "too long" in hits[3].reason
    assert hits[4] is not None and hits[4].pattern_id == "s"
    # a new request has its own budget
    assert asyncio.run(bl.find_match(org=ORG, namespace=NS, value="y" * 1_000)) is None

    async def _worker_pass() -> list[Any]:
        # a worker binds no request budget: one tenant's values never spend another's
        return [await bl.find_match(org=ORG, namespace=NS, value="y" * 1_000) for _ in range(5)]

    assert asyncio.run(_worker_pass()) == [None] * 5


def test_each_op_the_transports_begin_starts_one_regex_budget(monkeypatch) -> None:
    """The transports bind the budget at each op (`_common.begin_request`, which MCP's
    `begin_call` runs per call): two checks after one begin share it, a new begin
    starts again at zero."""
    import server.memory.app.blacklist_guard as bl
    from server.transport import _common

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    regex = _pattern("r", "namespace", "x+", "regex")
    monkeypatch.setattr(bl, "_cache", [regex])
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 1_500 * regex.compiled.programsize)

    async def _op() -> list[Any]:
        _common.begin_request()
        return [await bl.find_match(org=ORG, namespace=NS, value="y" * 1_000) for _ in range(2)]

    first, second = asyncio.run(_op())
    assert first is None and second is not None and second.pattern_id == ""
    assert asyncio.run(_op())[0] is None  # the next op starts at zero


@pytest.mark.parametrize("over_budget", [False, True])
def test_a_long_substring_scan_runs_on_the_matcher_threads(monkeypatch, over_budget: bool) -> None:
    """A substring scan holds the GIL for the length of one `in`: a long one runs on the
    matcher threads, where the event loop gets its turn between patterns, and a short
    one stays on the loop. Past the regex budget, the substring check still names a hit."""
    import threading

    import server.memory.app.blacklist_guard as bl

    ran_on: list[str] = []
    first_match = bl._first_match

    def _recording(*args: Any) -> Any:
        ran_on.append(threading.current_thread().name)
        return first_match(*args)

    _fresh_slots(monkeypatch)
    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    monkeypatch.setattr(bl, "_first_match", _recording)
    cache = [_pattern(f"s{i}", "namespace", f"<needle{i}>", "substring") for i in range(20)]
    if over_budget:
        cache.append(_pattern("r", "namespace", r"\bssn\b", "regex"))
        monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 1_000)
    monkeypatch.setattr(bl, "_cache", cache)
    value = "x" * (bl.SUBSTRINGS_ON_LOOP_BYTES // 20 + 10) + "<needle19>"
    hit = asyncio.run(bl.find_match(org=ORG, namespace=NS, value=value))
    assert hit is not None and hit.pattern_id == "s19"
    assert ran_on and all(name.startswith("loom-blacklist") for name in ran_on)
    if not over_budget:  # with a regex applicable, a short value goes to the threads anyway
        ran_on.clear()
        short = asyncio.run(bl.find_match(org=ORG, namespace=NS, value="<needle3>"))
        assert short is not None and short.pattern_id == "s3"
        assert ran_on == [threading.main_thread().name]


@pytest.mark.parametrize("op", ["set", "bulk_set"])
def test_a_pass_that_starts_over_is_not_charged_the_regex_budget_again(monkeypatch, op) -> None:
    """Under the Postgres authority a write whose head moved runs its guards again on
    the same values: the request is charged for them once, so a value that fits the
    budget is never refused as too long on a later pass."""
    import server.memory.app.blacklist_guard as bl
    import server.memory.app.bulk_set as bulk
    import server.memory.app.write as write
    from server.core import pg
    from server.memory.store.pg_memory import HeadMoved

    monkeypatch.setattr(bl, "_ensure_loaded", _loaded)
    regex = _pattern("r", "namespace", "x+", "regex")
    monkeypatch.setattr(bl, "_cache", [regex])
    # One pass spends 60% of the budget.
    monkeypatch.setattr(bl, "REGEX_WORK_BUDGET", 1_000 * regex.compiled.programsize * 10 // 6)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(bulk, "get_embedder", _Embedder)
    monkeypatch.setattr(write, "get_embedder", _Embedder)
    hits: list[Any] = []

    async def _pass(*_args: Any, **_kwargs: Any) -> str:
        hits.append(await bl.find_match(org=ORG, namespace=NS, value="y" * 1_000))
        if len(hits) < 3:
            raise HeadMoved()
        return "committed"

    async def _request() -> Any:
        bl.begin_request_budget()
        if op == "set":
            monkeypatch.setattr(write, "_memory_set_once", _pass)
            return await write._memory_set_phases(_Input())
        monkeypatch.setattr(bulk, "_bulk_set_once", _pass)
        return await bulk.handle_memory_bulk_set(_Input())

    assert asyncio.run(_request()) == "committed"
    assert hits == [None, None, None]


class _Embedder:
    model_name = "fake"
    dim = 4


class _Input:
    namespace = NS
    memory_key = "k"

    def model_copy(self) -> _Input:
        return self
