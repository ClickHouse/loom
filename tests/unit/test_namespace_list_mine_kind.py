"""list_mine: claim-only (JWT) namespaces carry the directory's kind/visibility
instead of hard-coded memory/private, so code namespaces named in a token's
claim never masquerade as a memory identity; a collection's name with no
directory row takes its reserved prefix's kind; and rows carry the directory's
title through the handler."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.app import namespace_membership as ns_mod


class _CH:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append(params or {})
        return self.rows


@pytest.mark.asyncio
async def test_namespace_meta_maps_kind_and_visibility() -> None:
    ch = _CH(
        [
            {
                "namespace": "code-clickhouse",
                "kind": "code",
                "visibility": "public",
                "description": "Code index",
            },
            {"namespace": "team-kb", "kind": "memory", "visibility": "private", "description": ""},
        ]
    )
    meta = await ns_mod._namespace_meta(ch, "clickhouse", ["code-clickhouse", "team-kb", "ghost"])
    assert meta["code-clickhouse"] == {
        "kind": "code",
        "visibility": "public",
        "title": "",
        "description": "Code index",
    }
    assert meta["team-kb"]["kind"] == "memory"
    assert "ghost" not in meta  # no row → caller falls back to memory/private
    assert ch.calls[0]["ns"] == ["code-clickhouse", "team-kb", "ghost"]


@pytest.mark.asyncio
async def test_namespace_meta_fails_open() -> None:
    class _Boom:
        async def query_all(self, sql, params=None):
            raise RuntimeError("ch down")

    assert await ns_mod._namespace_meta(_Boom(), "o", ["x"]) == {}


def test_a_name_with_no_directory_row_takes_the_kind_its_reserved_prefix_gives() -> None:
    """A collection whose create stopped before its directory row is still a collection."""
    configured = {"code-acme": "code"}
    assert ns_mod._kind(configured, "col-research", "") == "collection"
    assert ns_mod._kind(configured, "col-me-0123456789abcdef", None) == "personal"
    assert ns_mod._kind(configured, "code-acme", "") == "code"
    assert ns_mod._kind(configured, "team", "") == "memory"
    # Only the collection prefixes are read from the name: a bench-shaped one stays memory.
    assert ns_mod._kind(configured, "bench-run-1", "") == "memory"
    # The directory row's kind wins over the name.
    assert ns_mod._kind(configured, "col-research", "collection") == "collection"
    assert ns_mod._kind(configured, "team", "tracker") == "tracker"


@pytest.mark.asyncio
async def test_namespace_meta_carries_the_title_and_leaves_a_missing_kind_to_the_name() -> None:
    ch = _CH(
        [
            {
                "namespace": "col-research",
                "kind": "",
                "visibility": "private",
                "title": "Research",
                "description": "",
            }
        ]
    )
    meta = await ns_mod._namespace_meta(ch, "acme", ["col-research"])
    assert meta["col-research"]["title"] == "Research"
    assert meta["col-research"]["kind"] == ""
    assert ns_mod._kind({}, "col-research", meta["col-research"]["kind"]) == "collection"


@pytest.mark.asyncio
async def test_list_mine_rows_carry_the_collection_title_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.auth.aliases as aliases
    import server.auth.context as ctx
    import server.identity.app.namespace as registry
    from server.auth.jwt import AuthInfo
    from server.identity.api.models import NamespaceListMineInput
    from server.operations.base import Role

    def grant(ns: str, kind: str, title: str) -> dict[str, Any]:
        return {
            "namespace": ns,
            "role": "viewer",
            "status": "active",
            "pending_role": None,
            "pending_since": None,
            "joined_at": datetime(2026, 9, 18),
            "visibility": "private",
            "kind": kind,
            "title": title,
            "description": "",
            "is_new": 0,
        }

    class _Grants:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [
                grant("col-research", "collection", "Research"),
                grant("col-unfinished", "", ""),  # a create cut short: no directory row
                grant("team", "memory", ""),
            ]

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            return None  # not a client token: nothing inherited

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    auth = AuthInfo(
        subject="kim",
        org="acme",
        namespaces=("team",),
        roles=(Role.ADMIN,),
        is_service_account=False,
    )
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(registry, "resolve_subject", lambda s: s or "kim")
    monkeypatch.setattr(ctx, "current_auth", lambda: auth)
    monkeypatch.setattr(ns_mod, "get_client", lambda: _Grants())
    out = await ns_mod.handle_namespace_list_mine(NamespaceListMineInput(org="acme", subject="kim"))
    rows = {r.namespace: r for r in out.rows}
    assert (rows["col-research"].kind, rows["col-research"].title) == ("collection", "Research")
    assert rows["col-unfinished"].kind == "collection"
    assert (rows["team"].kind, rows["team"].title) == ("memory", "")
