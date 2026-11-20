"""The default a call with no namespace lands in is a memory-kind namespace
the token can write, the owner's own first — not
the newest viewer grant on a code index."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from server.auth.jwt import AuthInfo
from server.core.errors import BackendUnavailableError
from server.identity.app import caller_home as home
from server.operations.base import Role

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


@pytest.fixture()
def _inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The home's saved pick and ranked grants (empty unless a test sets
    them), the kinds above, one spelling per subject."""
    import server.auth.aliases as aliases
    import server.core.ch as chmod
    import server.identity.app.caller_defaults as caller_defaults
    import server.identity.app.namespace_classification as nc
    import server.identity.app.namespace_kinds as nk
    from server.settings import settings

    state: dict[str, Any] = {"saved": "", "rows": [], "kinds": dict(KINDS)}

    async def _saved(org: str, subject: str, *, ch: Any = None) -> str:
        return str(state["saved"])

    async def _ranked(org: str, subs: list[str], *, ch: Any = None) -> list[dict[str, str]]:
        return [{"namespace": ns, "role": "writer"} for ns in state["rows"]]

    async def _kinds(org: str) -> dict[str, str]:
        return dict(state["kinds"])

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(caller_defaults, "saved_default", _saved)
    monkeypatch.setattr(caller_defaults, "ranked_grants", _ranked)
    monkeypatch.setattr(nk, "kind_map", _kinds)
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(nc, "restricted_set", _none_restricted)
    monkeypatch.setattr(chmod, "get_client", lambda: None)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")
    return state


def _pick(claimed: list[str], writable: set[str], who: str = "alice", readable: Any = None) -> str:
    """`_pick_default` for a token the gate lets write `writable` and read
    `readable` (everything when not given)."""

    async def _admits(ns: str, role: Role) -> bool:
        if role is Role.WRITER:
            return ns in writable
        return readable is None or ns in readable

    return asyncio.run(home._pick_default(None, "o", who, claimed, _admits, []))


def test_the_owners_writable_memory_namespace_wins_over_index_claims(_inputs) -> None:
    everything = set(CLAIMED) | {"core-tracker", "team-x"}
    assert _pick(CLAIMED, everything) == "alice"
    # The claim order does not matter: a code namespace first still loses.
    assert _pick(list(reversed(CLAIMED)), everything) == "alice"
    assert _pick(["core-tracker", "alice"], everything) == "alice"
    # No namespace named after the owner: the saved pick, then the first memory-kind one.
    _inputs["kinds"].update({"team-x": "memory"})
    assert _pick(["core-tracker", "team-x"], everything, "someone") == "core-tracker"
    _inputs["saved"] = "team-x"
    assert _pick(["core-tracker", "team-x"], everything, "someone") == "team-x"


def test_viewer_only_memory_claims_still_pick_a_memory_namespace_and_indexes_never_win(
    _inputs,
) -> None:
    assert _pick(["core-tracker", "alice"], {"alice"}, "nobody") == "alice", "the writable one"
    assert _pick(["core-tracker"], set(), "nobody") == "core-tracker", "nothing writable: read"
    assert _pick(["code-loom", "tracker-example-private"], set(CLAIMED)) == "", "only indexes"
    assert _pick([], set()) == ""
    # An unknown kind (legacy namespace without a row) counts as memory.
    assert _pick(["legacy-ns"], {"legacy-ns"}, "x") == "legacy-ns"


def test_a_writable_grant_beats_a_claim_the_token_only_reads(_inputs) -> None:
    # Writable anywhere comes before readable anywhere, across every step.
    _inputs["rows"] = ["team-x"]
    assert _pick(["core-tracker"], {"team-x"}, "nobody") == "team-x"


def test_a_claim_capped_below_writer_is_not_the_write_home(
    _inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import grant_cache

    async def _viewer_on_alice(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={"alice": 1})

    monkeypatch.setattr(grant_cache, "_read", _viewer_on_alice)
    token = AuthInfo(
        subject="alice",
        org="o",
        namespaces=("alice", "core-tracker"),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    assert asyncio.run(home.default_namespace(token, "o")) == "core-tracker"


def test_the_home_is_the_signed_creator_and_a_failure_names_no_default(
    _inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _token(subject: str, creator: str = "") -> AuthInfo:
        return AuthInfo(
            subject=subject,
            org="o",
            namespaces=("alice",),
            roles=(Role.WRITER,),
            is_service_account=subject.startswith("sa:"),
            creator=creator,
        )

    # A delegated token: its signed creator is the home.
    assert home.home_subject(_token("sa:claude-desktop-3b9fb8", "alice")) == "alice"
    # No creator (a bot, an operator-minted or self-signup token): its own subject.
    assert home.home_subject(_token("sa:bot")) == "sa:bot"

    reached: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> Any:
        reached.append("boom")
        raise RuntimeError("down")

    async def _none(*_a: Any, **_k: Any) -> str:
        return ""

    # Provisioning is not what fails here: each case reaches the gate's view.
    monkeypatch.setattr(home, "_provisioned", _none)
    # The gate's answer cannot be worked out: no default and nothing readable.
    monkeypatch.setattr(home, "_gate_view", _boom)
    assert asyncio.run(home.default_and_readable(_token("alice"), "o")) == ("", [])

    async def _down(*_a: Any, **_k: Any) -> Any:
        reached.append("down")
        raise BackendUnavailableError("grants could not be read", operation="grant_map")

    # ClickHouse is down: the call is answered 503, to retry.
    monkeypatch.setattr(home, "_gate_view", _down)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(home.default_and_readable(_token("alice"), "o"))
    assert reached == ["boom", "down"]


def test_the_resolver_asks_the_claims_before_it_ranks_grants() -> None:
    src = inspect.getsource(home._pick_default)
    assert src.index("_owner_first(claimed") < src.index("ranked_grants(")


def _list_token(saves_to: str, namespaces: tuple[str, ...]) -> AuthInfo:
    return AuthInfo(
        subject="sa:ci-ab12cd",
        org="o",
        namespaces=namespaces,
        roles=(),
        ns_roles=tuple((ns, "writer") for ns in namespaces),
        is_service_account=True,
        creator="someone",
        saves_to=saves_to,
    )


def _pick_for(token: AuthInfo, writable: set[str], readable: set[str]) -> str:
    async def _admits(ns: str, role: Role) -> bool:
        return ns in (writable if role is Role.WRITER else readable)

    return asyncio.run(home._pick_default(token, "o", "someone", [], _admits, []))


def test_a_recorded_saves_to_wins_over_the_owners_saved_pick(_inputs) -> None:
    _inputs["kinds"].update({"team-x": "memory", "team-y": "memory"})
    _inputs["saved"] = "team-x"
    token = _list_token("team-y", ("team-x", "team-y"))
    both = {"team-x", "team-y"}
    assert _pick_for(token, both, both) == "team-y"
    # A token minted before the claim (or by build N-1) follows the saved pick.
    assert _pick_for(_list_token("", ("team-x", "team-y")), both, both) == "team-x"


def test_a_downgraded_target_falls_to_its_list_never_to_the_saved_pick(_inputs) -> None:
    _inputs["kinds"].update({"team-x": "memory", "team-y": "memory", "team-z": "memory"})
    _inputs["saved"] = "team-z"  # writable, and not the token's
    _inputs["rows"] = ["team-z"]
    token = _list_token("team-y", ("team-x", "team-y"))
    # team-y lowered to viewer, team-x still written: the list's next writable entry.
    assert _pick_for(token, {"team-x", "team-z"}, {"team-x", "team-y", "team-z"}) == "team-x"
    # Nothing in the token writes any more: the target at viewer, in the list's order.
    assert _pick_for(token, {"team-z"}, {"team-x", "team-y", "team-z"}) == "team-y"
    # Revoked outright: the rest of its list, then nothing.
    assert _pick_for(token, {"team-z"}, {"team-x", "team-z"}) == "team-x"
    assert _pick_for(token, {"team-z"}, {"team-z"}) == ""


def test_a_downgraded_target_never_falls_into_a_collection(_inputs) -> None:
    from server.core.principals import COLLECTION_PREFIX

    col = f"{COLLECTION_PREFIX}team"
    _inputs["kinds"].update({"team-y": "memory", col: "memory"})
    token = _list_token("team-y", ("team-y", col))
    # team-y lowered to viewer while the collection is still written: the target at viewer.
    assert _pick_for(token, {col}, {"team-y", col}) == "team-y"


def test_a_collection_is_no_delegated_tokens_home_at_any_step(_inputs) -> None:
    """A `*` token whose claim names a collection before `*` (as build N-1 mints one) never
    saves there unasked, whatever the gate admits; a person's own sign-in still may."""
    from server.core.principals import COLLECTION_PREFIX

    col = f"{COLLECTION_PREFIX}team"
    _inputs["kinds"].update({"team-x": "memory", col: "memory"})
    token = AuthInfo(
        subject="sa:ci-ab12cd",
        org="o",
        namespaces=(col, "*"),
        roles=("writer",),
        is_service_account=True,
        creator="someone",
    )
    both = {"team-x", col}
    # No saved pick and the collection writable: not the collection.
    assert asyncio.run(home._pick_default(token, "o", "someone", [col], _admits_in(both), [])) == ""
    _inputs["saved"] = "team-x"
    assert _pick_for(token, both, both) == "team-x"
    _inputs["saved"] = ""
    person = AuthInfo(
        subject="alice", org="o", namespaces=(col,), roles=("writer",), is_service_account=False
    )
    assert asyncio.run(home._pick_default(person, "o", "alice", [col], _admits_in(both), [])) == col


def test_a_read_only_collection_is_still_a_tokens_default_to_read(_inputs) -> None:
    """Reading a shared handbook by default is fine; only a default it could write is not."""
    from server.core.principals import COLLECTION_PREFIX

    col = f"{COLLECTION_PREFIX}handbook"
    _inputs["kinds"].update({col: "memory"})
    token = _list_token("", (col,))

    def pick(writable: set[str]) -> str:
        async def _admits(ns: str, role: Role) -> bool:
            return ns == col and (role is not Role.WRITER or ns in writable)

        return asyncio.run(home._pick_default(token, "o", "someone", [col], _admits, []))

    assert pick(set()) == col
    assert pick({col}) == ""
    # A list token whose recorded target was revoked reads the collection it may only read.
    _inputs["kinds"].update({"team-y": "memory"})
    listed = _list_token("team-y", ("team-y", col))
    assert _pick_for(listed, set(), {col}) == col


def _admits_in(names: set[str]):
    async def _admits(ns: str, role: Role) -> bool:
        return ns in names

    return _admits
