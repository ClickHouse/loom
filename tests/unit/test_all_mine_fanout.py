"""What a namespace_scope=all_mine search reads: the single user's own
namespaces when auth is off, at most `memory_search_fanout_max_namespaces`
of the caller's namespaces (memory search names the rest), and
always the namespace the call addressed."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator

import pytest

import server.auth.context as ctx
import server.auth.fanout as fanout_mod
import server.identity.app.namespace_kinds as nk
import server.identity.app.namespace_visibility as nv
from server.auth.fanout import Fanout, namespaces_to_search
from server.auth.jwt import AuthInfo
from server.core.vectors.private_first import private_first_passes
from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app import knowledge_search
from server.knowledge.app.search import ChunkHit
from server.operations.base import Role
from server.settings import settings
from server.transport import mcp_call, mcp_tools
from tests.unit.test_mcp_lean_results import PAYLOAD as SEARCH_PAYLOAD

ORG = "dev"


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[str]]]:
    """Grant rows by subject, served by a fake ClickHouse client."""
    by_subject: dict[str, list[str]] = {}

    class _Client:
        async def query_all(self, sql, params=None):
            if "namespace_grants" not in sql:
                return []
            subjects = (params or {}).get("subs", [])
            return [{"namespace": ns} for s in subjects for ns in by_subject.get(s, [])]

        async def query_one(self, sql, params=None):
            return None

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    nk.reset_cache()
    yield by_subject
    ctx.set_current_auth(None)


def _flat(*namespaces: str) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=False,
    )


def _fanout() -> Fanout:
    return asyncio.run(fanout_mod.resolve_fanout(ORG))


def test_auth_off_fans_out_over_the_single_users_namespaces(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_required", False)
    grants["you"] = ["local", "work"]
    assert _fanout().namespaces == ("local", "work")


def test_auth_on_without_a_token_reads_no_ones_grants(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    grants["you"] = ["local", "work"]
    assert _fanout().namespaces == ()


def test_the_cap_keeps_private_namespaces_and_names_the_rest(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"a-team": "public", "b-team": "public"}

    monkeypatch.setattr(nv, "visibility_map", _visibility)
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 2)
    ctx.set_current_auth(_flat("a-team", "alice", "b-team", "notes"))
    fanout = _fanout()
    assert fanout.split("zed") == (["zed", "alice", "notes"], ["a-team", "b-team"])
    # The addressed namespace takes no slot: the cap bounds the others.
    assert fanout.split("alice") == (["alice", "a-team", "notes"], ["b-team"])


def test_a_failed_visibility_lookup_cuts_in_plain_order(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _unreadable(org: str, namespaces: list[str]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(nv, "visibility_map", _unreadable)
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 2)
    ctx.set_current_auth(_flat("a-team", "alice", "b-team"))
    assert _fanout().split("zed") == (["zed", "a-team", "alice"], ["b-team"])


def test_auth_off_applies_the_cap_too(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"alpha-team": "public", "local": "private"}

    monkeypatch.setattr(nv, "visibility_map", _visibility)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 1)
    grants["you"] = ["alpha-team", "local", "work"]
    assert _fanout().split("work") == (["work", "local"], ["alpha-team"])


def test_a_namespace_a_claim_repeats_is_read_once(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 2)
    scoped = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("team", "team", "alice", "wiki"),
        roles=(),
        is_service_account=True,
        ns_roles=(("team", "viewer"), ("alice", "writer"), ("wiki", "viewer")),
    )
    ctx.set_current_auth(scoped)
    assert _fanout().split("zed") == (["zed", "team", "alice"], ["wiki"])


def test_the_cap_keeps_a_scoped_tokens_own_order(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 1)
    scoped = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("team", "alice"),
        roles=(),
        is_service_account=True,
        ns_roles=(("team", "viewer"), ("alice", "writer")),
    )
    ctx.set_current_auth(scoped)
    fanout = _fanout()
    assert fanout.ordered
    assert fanout.split("zed") == (["zed", "team"], ["alice"])


def test_the_addressed_namespace_always_leads() -> None:
    fanout = Fanout(org=ORG, namespaces=("team", "mine", "wiki"), cap=32)
    assert fanout.split("mine") == (["mine", "team", "wiki"], [])
    assert fanout.split("other") == (["other", "team", "mine", "wiki"], [])
    assert Fanout(org=ORG, namespaces=(), cap=32).split("mine") == (["mine"], [])


def test_private_first_passes_put_the_addressed_namespace_first() -> None:
    def _passes(namespace: str, answer: dict[str, str]) -> tuple[list[str], list[str]] | None:
        return private_first_passes(namespace, ["team", "home", "wiki"], answer)

    visibility = {"home": "private", "team": "public", "wiki": "public"}
    assert _passes("team", visibility) == (["team", "home"], ["wiki"])
    assert _passes("team", {"team": "public", "wiki": "public"}) == (
        ["team", "home"],
        ["wiki"],
    ), "a namespace without an answer counts as private"
    assert _passes("team", {}) is None, "no answer: one flat pass"
    everyone = {"team": "public", "home": "public", "wiki": "public"}
    assert _passes("team", everyone) is None, "nothing private: one flat pass"


def test_namespaces_to_search_leads_with_the_addressed_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("team", "mine", "wiki"), cap=1)

    monkeypatch.setattr(fanout_mod, "resolve_fanout", _fanout)
    assert asyncio.run(namespaces_to_search(ORG, "mine", "all_mine")) == (
        ["mine", "team"],
        ["wiki"],
    )
    assert asyncio.run(namespaces_to_search(ORG, "wiki", "all_mine")) == (
        ["wiki", "team"],
        ["mine"],
    )
    assert asyncio.run(namespaces_to_search(ORG, "mine", "this")) == (["mine"], [])
    assert asyncio.run(namespaces_to_search(ORG, "mine", None)) == (["mine"], [])


def test_knowledge_reads_an_addressed_public_namespace_in_the_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"home": "private", "team": "public", "wiki": "public"}

    async def _search_one(ns: str) -> tuple[str, list[ChunkHit]]:
        read.append(ns)
        hit = ChunkHit(source_uri=f"{ns}.md", chunk_idx=0, content="", tags=[], distance=0.2)
        return ns, [hit]

    read: list[str] = []
    monkeypatch.setattr(nv, "visibility_map", _visibility)
    search = KnowledgeSearchInput(org=ORG, namespace="team", query="q", namespace_scope="all_mine")
    asyncio.run(
        knowledge_search._search_private_first(search, ["team", "home", "wiki"], _search_one)
    )
    assert read == ["team", "home"]


def test_the_loom_search_text_names_the_skipped_namespaces() -> None:
    payload = copy.deepcopy(SEARCH_PAYLOAD)
    payload["memory"]["namespaces_skipped"] = ["work", "home"]
    answer = mcp_tools.search_answer(payload, {"query": "q"}, "local", "session-1")
    text = answer["content"][0]["text"]
    assert "Not read, over the fan-out cap: work, home." in text.splitlines()

    payload["memory"]["namespaces_skipped"] = [f"ns-{i}" for i in range(8)]
    text = mcp_tools.search_answer(payload, {"query": "q"}, "local", "session-1")["content"][0][
        "text"
    ]
    assert "ns-0, ns-1, ns-2, ns-3, ns-4 and 3 more (memory.namespaces_skipped)." in text


def test_an_unaddressed_search_starts_at_home_even_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("aaa-team", "alice"), cap=1)

    monkeypatch.setattr(fanout_mod, "resolve_fanout", _fanout)
    arguments = {"org": ORG, "namespace": "aaa-team", "namespace_scope": "all_mine"}
    asyncio.run(mcp_call._address_home(arguments, _flat("*")))
    assert arguments["namespace"] == "alice"


def test_the_conversation_fallback_reads_the_same_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as conversation_repo
    from server.knowledge.store.conversations import MessageRow

    class _NoEmbedder:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            raise RuntimeError("embedder down")

    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("work", "home"), cap=1)

    async def _substring(org: str, namespace: str, *_a: object, **_k: object) -> list[MessageRow]:
        day = {"local": 1, "work": 2}[namespace]
        return [
            MessageRow(
                org=org,
                namespace=namespace,
                agent="a",
                thread_id="t",
                msg_id=namespace,
                role="user",
                content=f"in {namespace}",
                ts=datetime(2026, 1, day),
            )
        ]

    monkeypatch.setattr(conversations, "get_embedder", lambda: _NoEmbedder())
    monkeypatch.setattr(fanout_mod, "resolve_fanout", _fanout)
    monkeypatch.setattr(conversation_repo, "search_substring", _substring)
    search = ConversationSearchInput(org=ORG, namespace="local", agent="a", query="in")
    out = asyncio.run(conversations.handle_conversation_search(search))
    assert [(h.namespace, h.msg_id) for h in out.hits] == [("work", "work"), ("local", "local")]
    assert out.namespaces_skipped == ["home"]
