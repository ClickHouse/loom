"""What a namespace_scope=all_mine search reads: the single user's own
namespaces when auth is off, at most `memory_search_fanout_max_namespaces`
of the caller's namespaces (the rest named, never dropped silently), and
always the namespace the call addressed."""

from __future__ import annotations

import asyncio

import pytest

import server.auth.context as ctx
import server.identity.app.namespace_kinds as nk
from server.auth.context import Fanout, with_addressed
from server.auth.jwt import AuthInfo
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import scope
from server.memory.app.search.evidence import _merge_namespace_scope
from server.operations.base import Role
from server.settings import settings
from server.transport import mcp_call

ORG = "dev"


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
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


def _fanout(org: str = ORG) -> Fanout:
    return asyncio.run(ctx.resolve_fanout(org))


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


def test_the_cap_keeps_the_first_namespaces_and_names_the_rest(
    grants: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_search_fanout_max_namespaces", 2)
    ctx.set_current_auth(
        AuthInfo(
            subject="alice",
            org=ORG,
            namespaces=("a", "b", "c", "d"),
            roles=(Role.WRITER,),
            is_service_account=False,
        )
    )
    fanout = _fanout()
    assert fanout.namespaces == ("a", "b")
    assert fanout.skipped == ("c", "d")
    assert asyncio.run(ctx.resolve_fanout_namespaces(ORG)) == ["a", "b"]


def test_the_addressed_namespace_is_always_read() -> None:
    assert with_addressed("mine", ["team"]) == ["mine", "team"]
    assert with_addressed("mine", ["team", "mine"]) == ["team", "mine"]
    assert with_addressed("mine", []) == ["mine"]
    assert with_addressed("", ["team"]) == ["team"]
    # A scoped token keeps its order; the addressed namespace it lacks is read last.
    assert _merge_namespace_scope("mine", ["t1", "t2"], ordered=True) == ["t1", "t2", "mine"]
    assert _merge_namespace_scope("t2", ["t1", "t2"], ordered=True) == ["t1", "t2"]
    assert _merge_namespace_scope("mine", [], ordered=True) == ["mine"]


def test_memory_search_names_what_the_cap_skipped_except_the_addressed_one() -> None:
    async def _run() -> tuple[list[str], scope._SearchRun]:
        async def _fanout() -> Fanout:
            return Fanout(org=ORG, namespaces=("a", "b"), skipped=("c", "mine"))

        async def _nothing() -> None:
            return None

        run = scope._SearchRun(
            rerank_override="",
            rerank_cfg_task=None,
            calibration_task=asyncio.create_task(_nothing()),
            my_namespaces_task=asyncio.create_task(_fanout()),
            order_authoritative=False,
        )
        search = MemorySearchInput(org=ORG, namespace="mine", query="q", namespace_scope="all_mine")
        _, namespaces = await scope._scope_inputs(search, run, [], {})
        await run.calibration_task
        return namespaces, run

    namespaces, run = asyncio.run(_run())
    assert namespaces == ["mine", "a", "b"]
    assert run.namespaces_skipped == ["c"]


def test_an_unaddressed_search_starts_at_home_even_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("aaa-team",), skipped=("alice",))

    monkeypatch.setattr(ctx, "resolve_fanout", _fanout)
    auth = AuthInfo(
        subject="alice@example.com",
        org=ORG,
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    arguments = {"org": ORG, "namespace": "aaa-team", "namespace_scope": "all_mine"}
    asyncio.run(mcp_call._address_home(arguments, auth))
    assert arguments["namespace"] == "alice"
