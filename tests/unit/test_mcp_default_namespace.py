"""The default a call with no namespace lands in is a memory-kind namespace
the token can write, the owner's own first — not
the newest viewer grant on a code index."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

from server.transport import mcp_identity as mi

CLAIMED = [
    "alice",
    "code-example-private-26-3",
    "code-example-private-26-6",
    "code-example-private-26-5",
    "code-example-private",
    "code-loom",
    "tracker-example-private",
]
KINDS = {
    "alice": "memory",
    "core-tracker": "memory",
    "code-example-private": "code",
    "code-loom": "code",
    "code-example-private-26-3": "code",
    "code-example-private-26-5": "code",
    "code-example-private-26-6": "code",
    "tracker-example-private": "qa",
}


def test_the_owners_writable_memory_namespace_wins_over_index_claims() -> None:
    assert mi.pick_default_namespace(CLAIMED, KINDS, lambda ns: True, "alice") == "alice"
    # The claim order does not matter: a code namespace first still loses.
    assert (
        mi.pick_default_namespace(list(reversed(CLAIMED)), KINDS, lambda ns: True, "alice")
        == "alice"
    )
    # A service account: the owner's login (created_by) names the home.
    assert (
        mi.pick_default_namespace(["core-tracker", "alice"], KINDS, lambda ns: True, "alice")
        == "alice"
    )
    # No namespace named after the owner: the owner's saved default, then the first memory-kind one.
    assert (
        mi.pick_default_namespace(
            ["core-tracker", "team-x"],
            {"core-tracker": "memory", "team-x": "memory"},
            lambda ns: True,
            "someone",
            "team-x",
        )
        == "team-x"
    )
    assert (
        mi.pick_default_namespace(
            ["core-tracker", "team-x"],
            {"core-tracker": "memory", "team-x": "memory"},
            lambda ns: True,
            "someone",
        )
        == "core-tracker"
    )


def test_viewer_only_memory_claims_still_pick_a_memory_namespace_and_indexes_never_win() -> None:
    writable = {"alice"}
    assert (
        mi.pick_default_namespace(
            ["core-tracker", "alice"], KINDS, lambda ns: ns in writable, "nobody"
        )
        == "alice"
    ), "the writable one"
    assert (
        mi.pick_default_namespace(["core-tracker"], KINDS, lambda ns: False, "nobody")
        == "core-tracker"
    ), "nothing writable: still memory-kind, the grant ranking may override"
    assert (
        mi.pick_default_namespace(
            ["code-loom", "tracker-example-private"], KINDS, lambda ns: True, "alice"
        )
        == ""
    ), "only indexes claimed: nothing qualifies"
    assert mi.pick_default_namespace([], KINDS, lambda ns: True, "alice") == ""
    # An unknown kind (legacy namespace without a row) counts as memory.
    assert mi.pick_default_namespace(["legacy-ns"], {}, lambda ns: True, "x") == "legacy-ns"


def _auth(subject: str, *, is_service_account: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        subject=subject,
        org="clickhouse",
        namespaces=(),
        ns_roles=(),
        is_service_account=is_service_account,
        can_write=lambda org, ns: True,
    )


def test_a_claim_capped_below_writer_is_not_the_write_home(monkeypatch) -> None:
    from server.core import grant_cache

    async def _kind_map(org: str) -> dict[str, str]:
        return KINDS

    async def _viewer_on_alice(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={"alice": 1})

    async def _no_default(*_a: Any, **_k: Any) -> None:
        return None

    import server.identity.app.namespace_kinds as nk

    monkeypatch.setattr(nk, "kind_map", _kind_map)
    monkeypatch.setattr(grant_cache, "_read", _viewer_on_alice)
    monkeypatch.setattr(mi.caller_defaults, "saved_default", _no_default)
    auth = _auth("alice")
    auth.namespaces = ("alice", "core-tracker")
    claimed = ["alice", "core-tracker"]
    assert asyncio.run(mi._claimed_home_default(auth, "clickhouse", claimed, None)) == (
        "core-tracker"
    )


def test_the_async_wrapper_reads_the_service_accounts_owner_and_saved_default(monkeypatch) -> None:
    async def _kind_map(org: str) -> dict[str, str]:
        return KINDS

    import server.identity.app.namespace_kinds as nk

    monkeypatch.setattr(nk, "kind_map", _kind_map)

    class _CH:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def query_one(self, sql: str, params: Any = None) -> Any:
            self.sql.append(sql)
            if "service_accounts" in sql:
                assert params["sub"] == "sa:claude-desktop-3b9fb8"
                return {"created_by": "alice"}
            if "user_state" in sql:
                self.owner_asked = params["sub"]
                return {"namespace": "alice"} if params["sub"] == "alice" else None
            return None

    auth = _auth("sa:claude-desktop-3b9fb8", is_service_account=True)
    ch = _CH()
    out = asyncio.run(mi._claimed_home_default(auth, "clickhouse", CLAIMED, ch))
    assert out == "alice"
    assert any("service_accounts" in q for q in ch.sql) and any("user_state" in q for q in ch.sql)
    assert ch.owner_asked == "alice", "the owner's saved default, not the SA's"
    # A human subject: no service-account read.
    ch2 = _CH()
    human = _auth("alice@example.com")
    assert (
        asyncio.run(mi._claimed_home_default(human, "clickhouse", ["core-tracker", "alice"], ch2))
        == "alice"
    )
    assert not any("service_accounts" in q for q in ch2.sql)

    # Fail-open: a broken client answers "" and the grant ranking decides.
    class _Boom:
        async def query_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    assert asyncio.run(mi._claimed_home_default(auth, "clickhouse", CLAIMED, _Boom())) == ""


def test_the_resolver_asks_the_claims_before_it_ranks_grants() -> None:
    src = inspect.getsource(mi._caller_defaults)
    assert src.index("_claimed_home_default(auth_info, org, claimed_namespaces, ch)") < src.index(
        "Priority 2 (no/invalid saved default)"
    )
