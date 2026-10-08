"""A client token's membership list shows what it reads
through its creator, minus restricted namespaces; the doctor counts a
restricted repository the token names as readable."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.cli.doctor_client as dc
import server.identity.app.namespace as ns_mod
import server.identity.app.namespace_membership as membership
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "clickhouse"
SA = "sa:claude-code-2be2ef"


def _grant(ns: str, subject: str, role: str = "viewer", kind: str = "memory") -> dict[str, Any]:
    return {
        "namespace": ns,
        "role": role,
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "joined_at": datetime(2026, 9, 18),
        "visibility": "private",
        "kind": kind,
        "description": "",
        "is_new": 0,
        "_subject": subject,
    }


class _FakeCH:
    def __init__(self, grants: list[dict[str, Any]], sa: dict[str, Any] | None):
        self.grants, self.sa, self.calls = grants, sa, []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append(sql.split()[0] if sql.strip() else "")
        subs = set((params or {}).get("subs") or [])
        return [dict(g) for g in self.grants if g["_subject"] in subs]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        assert "service_accounts" in sql
        return self.sa


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    import server.auth.aliases as aliases
    import server.identity.app.namespace_classification as nc

    state: dict[str, Any] = {"restricted": frozenset({"code-private"})}

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject, subject + "@x"] if subject == "jdoe" else [subject]

    async def _restricted(org: str) -> frozenset[str]:
        return state["restricted"]

    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(nc, "restricted_set", _restricted)
    monkeypatch.setattr(ns_mod, "resolve_subject", lambda s: s or SA)
    return state


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeCH, auth: AuthInfo | None) -> None:
    import server.auth.context as ctx

    monkeypatch.setattr(membership, "get_client", lambda: fake)
    monkeypatch.setattr(ctx, "current_auth", lambda: auth)


@pytest.mark.asyncio
async def test_a_client_token_lists_its_creators_namespaces_as_inherited(
    wired, monkeypatch
) -> None:
    from server.identity.api.models import NamespaceListMineInput

    fake = _FakeCH(
        [
            _grant("code-private", SA, "viewer", "code"),  # its own grant
            _grant("jdoe", "jdoe", "admin"),  # creator, login spelling
            _grant("personal-lz", "jdoe@x", "admin"),  # creator, email spelling
            _grant("code-clickhouse", "jdoe", "admin", "code"),
            _grant("code-private", "jdoe", "admin", "code"),  # restricted: never inherited
            _grant("core-tracker", "jdoe", "admin", "qa"),
        ],
        {"created_by": "jdoe", "inherit_creator_grants": True},
    )
    auth = AuthInfo(
        subject=SA,
        org=ORG,
        namespaces=("jdoe", "code-private"),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    _install(monkeypatch, fake, auth)
    out = await membership.handle_namespace_list_mine(NamespaceListMineInput(org=ORG, subject=SA))
    rows = {r.namespace: r for r in out.rows}
    assert rows["code-private"].role == "viewer" and rows["code-private"].inherited is False
    assert rows["jdoe"].role == "jwt" and rows["jdoe"].inherited is False
    assert rows["personal-lz"].inherited is True and rows["personal-lz"].role == "admin"
    assert rows["code-clickhouse"].inherited is True and rows["code-clickhouse"].kind == "code"
    assert rows["core-tracker"].inherited is True
    assert set(rows) == {
        "code-private",
        "jdoe",
        "personal-lz",
        "code-clickhouse",
        "core-tracker",
    }


@pytest.mark.asyncio
async def test_no_inheritance_when_opted_out_revoked_or_not_a_service_account(
    wired, monkeypatch
) -> None:
    from server.identity.api.models import NamespaceListMineInput

    grants = [_grant("jdoe", "jdoe", "admin"), _grant("own", SA, "writer")]
    for sa in ({"created_by": "jdoe", "inherit_creator_grants": False}, None):
        fake = _FakeCH(grants, sa)
        _install(
            monkeypatch,
            fake,
            AuthInfo(
                subject=SA,
                org=ORG,
                namespaces=("own",),
                roles=(Role.WRITER,),
                is_service_account=True,
            ),
        )
        out = await membership.handle_namespace_list_mine(
            NamespaceListMineInput(org=ORG, subject=SA)
        )
        assert [r.namespace for r in out.rows] == ["own"]
    # a person never inherits anything
    monkeypatch.setattr(ns_mod, "resolve_subject", lambda s: "jdoe")
    fake = _FakeCH(grants, {"created_by": "someone", "inherit_creator_grants": True})
    _install(
        monkeypatch,
        fake,
        AuthInfo(
            subject="jdoe",
            org=ORG,
            namespaces=("jdoe",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        ),
    )
    out = await membership.handle_namespace_list_mine(
        NamespaceListMineInput(org=ORG, subject="jdoe")
    )
    assert [r.namespace for r in out.rows] == ["jdoe"] and not any(r.inherited for r in out.rows)


def test_doctor_counts_a_named_restricted_repository_as_added_not_excluded() -> None:
    rows = [
        {"namespace": "jdoe", "role": "jwt", "kind": "memory"},
        {"namespace": "code-private", "role": "viewer", "kind": "code"},
        {"namespace": "code-private-26-8", "role": "viewer", "kind": "code"},
        {"namespace": "code-lumen", "role": "admin", "kind": "code", "inherited": True},
    ]
    repos = {
        "repos": [
            {
                "namespace": "code-private",
                "repo": "org/private",
                "branch": "master",
                "classification": "restricted",
            },
            {
                "namespace": "code-private-26-8",
                "repo": "org/private",
                "branch": "26.8",
                "classification": "restricted",
            },
            {
                "namespace": "code-lumen",
                "repo": "org/lumen",
                "branch": "master",
                "classification": "restricted",
            },
        ]
    }

    def post_json(*_a: Any, **_k: Any):
        return 200, repos

    claims = {"org": ORG, "namespaces": ["jdoe", "code-private", "code-private-26-8"]}
    _, grade, detail = dc.reads_check("https://loom.test", "tok", claims, rows, post_json, True)
    assert grade == "PASS"
    assert "restricted, added to this token: org/private (2 branches)" in detail
    assert "not included: org/lumen" in detail
    assert "org/private" not in detail.split("not included:")[1]


@pytest.mark.asyncio
async def test_an_unreadable_restricted_set_lists_nothing_inherited(wired, monkeypatch) -> None:
    """Inheritance never reads a restricted namespace. When the
    restricted set cannot be read, which namespaces are restricted is unknown,
    so nothing is listed as inherited — the token's own rows still are."""
    import server.identity.app.namespace_classification as nc
    from server.identity.api.models import NamespaceListMineInput

    async def _unreadable(org: str) -> frozenset[str]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(nc, "restricted_set", _unreadable)
    fake = _FakeCH(
        [
            _grant("code-private", SA, "viewer", "code"),  # its own grant
            _grant("code-clickhouse", "jdoe", "admin", "code"),
            _grant("code-private", "jdoe", "admin", "code"),  # restricted in truth
        ],
        {"created_by": "jdoe", "inherit_creator_grants": True},
    )
    auth = AuthInfo(
        subject=SA,
        org=ORG,
        namespaces=("code-private",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    _install(monkeypatch, fake, auth)
    out = await membership.handle_namespace_list_mine(NamespaceListMineInput(org=ORG, subject=SA))
    rows = {r.namespace: r for r in out.rows}
    assert set(rows) == {"code-private"}
    assert rows["code-private"].inherited is False
