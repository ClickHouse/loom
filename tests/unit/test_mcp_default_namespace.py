"""The default a call with no namespace lands in is a memory-kind namespace
the token can write, the owner's own first — not
the newest viewer grant on a code index."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

from server.identity.app import caller_home as home

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
    assert home.pick_default_namespace(CLAIMED, KINDS, lambda ns: True, "alice") == "alice"
    # The claim order does not matter: a code namespace first still loses.
    assert (
        home.pick_default_namespace(list(reversed(CLAIMED)), KINDS, lambda ns: True, "alice")
        == "alice"
    )
    # A service account: the owner's login (created_by) names the home.
    assert (
        home.pick_default_namespace(["core-tracker", "alice"], KINDS, lambda ns: True, "alice")
        == "alice"
    )
    # No namespace named after the owner: the owner's saved default, then the first memory-kind one.
    assert (
        home.pick_default_namespace(
            ["core-tracker", "team-x"],
            {"core-tracker": "memory", "team-x": "memory"},
            lambda ns: True,
            "someone",
            "team-x",
        )
        == "team-x"
    )
    assert (
        home.pick_default_namespace(
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
        home.pick_default_namespace(
            ["core-tracker", "alice"], KINDS, lambda ns: ns in writable, "nobody"
        )
        == "alice"
    ), "the writable one"
    assert (
        home.pick_default_namespace(["core-tracker"], KINDS, lambda ns: False, "nobody")
        == "core-tracker"
    ), "nothing writable: still memory-kind, the grant ranking may override"
    assert (
        home.pick_default_namespace(
            ["code-loom", "tracker-example-private"], KINDS, lambda ns: True, "alice"
        )
        == ""
    ), "only indexes claimed: nothing qualifies"
    assert home.pick_default_namespace([], KINDS, lambda ns: True, "alice") == ""
    # An unknown kind (legacy namespace without a row) counts as memory.
    assert home.pick_default_namespace(["legacy-ns"], {}, lambda ns: True, "x") == "legacy-ns"


def _auth(subject: str, *, is_service_account: bool = False, creator: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        subject=subject,
        creator=creator,
        org="clickhouse",
        namespaces=(),
        ns_roles=(),
        is_service_account=is_service_account,
        can_write=lambda org, ns: True,
    )


def test_a_claim_capped_below_writer_is_not_the_write_home(monkeypatch) -> None:
    from server.core import grant_cache

    async def _viewer_on_alice(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={"alice": 1})

    monkeypatch.setattr(grant_cache, "_read", _viewer_on_alice)
    auth = _auth("alice")
    auth.namespaces = ("alice", "core-tracker")
    claimed = ["alice", "core-tracker"]
    assert asyncio.run(home.claimed_home_default(auth, "clickhouse", claimed, KINDS, "alice")) == (
        "core-tracker"
    )


def test_the_claimed_default_follows_the_home_subject_and_fails_open(monkeypatch) -> None:
    # A delegated token: its signed creator is the home.
    auth = _auth("sa:claude-desktop-3b9fb8", is_service_account=True, creator="alice")
    assert home.home_subject(auth) == "alice"
    # No creator (a bot, an operator-minted or self-signup token): its own subject.
    assert home.home_subject(_auth("sa:bot", is_service_account=True)) == "sa:bot"

    # No claim named after the home: the saved default it was handed wins.
    claimed = ["alice", "core-tracker"]
    pick = home.claimed_home_default(auth, "clickhouse", claimed, KINDS, "bob", "core-tracker")
    assert asyncio.run(pick) == "core-tracker"

    # Fail-open: a claim check that breaks answers "" and the grant ranking decides.
    async def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("down")

    monkeypatch.setattr(home, "claim_cap", _boom)
    monkeypatch.setattr(home, "delegated_writable_claims", _boom)
    for token in (auth, _auth("alice")):
        pick = home.claimed_home_default(token, "clickhouse", CLAIMED, KINDS, "alice")
        assert asyncio.run(pick) == ""


def test_the_resolver_asks_the_claims_before_it_ranks_grants() -> None:
    src = inspect.getsource(home._multi_user_default)
    assert src.index("claimed_home_default(") < src.index("ranked_grants(")
