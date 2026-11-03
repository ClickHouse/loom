"""A page-minted token's grants on restricted namespaces —
written at mint, capped, revoked with the token, and revoked when the
person who minted it loses their own grant."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.identity.app.grants as grants_mod
import server.identity.app.token_grants as tg
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo

ORG = "clickhouse"


class _FakeCH:
    def __init__(self, grants: list[dict[str, Any]], sas: list[dict[str, Any]]):
        self.grants, self.sas, self.sql = grants, sas, []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append((sql, params))
        if "service_accounts" in sql:
            return self.sas
        ns = (params or {}).get("ns")
        sub = (params or {}).get("sub")
        return [
            g
            for g in self.grants
            if g["subject"] == sub
            and (ns is None or g["namespace"] == ns)
            and g.get("revoked_at") is None
        ]


@pytest.fixture
def inserted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _insert(batch: list[dict[str, Any]], **_kw: Any) -> None:
        rows.extend(batch)

    monkeypatch.setattr(grants_repo, "insert_rows", _insert)
    return rows


def test_restricted_grant_role_is_viewer_on_code_and_capped_at_writer_elsewhere() -> None:
    assert tg.restricted_grant_role(True, "admin") == "viewer"
    assert tg.restricted_grant_role(False, "admin") == "writer"
    assert tg.restricted_grant_role(False, "writer") == "writer"
    assert tg.restricted_grant_role(False, "viewer") == "viewer"
    assert tg.restricted_grant_role(False, "jwt") == "viewer"


@pytest.mark.asyncio
async def test_only_restricted_namespaces_are_granted_and_code_ones_as_viewer(inserted) -> None:
    async def _isr(org: str, ns: str) -> bool:
        return ns in {"code-private", "secret-notes"}

    granted = await tg.grant_restricted_to_token(
        ORG,
        "sa:claude-code-abc",
        "alice@x",
        {"owner-home": "admin", "code-private": "admin", "secret-notes": "admin"},
        is_restricted=_isr,
        is_code_namespace=lambda o, n: n.startswith("code-"),
    )
    assert granted == ["code-private", "secret-notes"]
    by_ns = {r["namespace"]: r for r in inserted}
    assert set(by_ns) == {"code-private", "secret-notes"}
    assert by_ns["code-private"]["role"] == "viewer"
    assert by_ns["secret-notes"]["role"] == "writer"
    for r in inserted:
        assert r["subject"] == "sa:claude-code-abc" and r["granted_by"] == "alice@x"
        assert r["status"] == "active" and r["revoked_at"] is None


@pytest.mark.asyncio
async def test_revoking_the_token_revokes_every_grant_it_holds(inserted) -> None:
    live = [
        {
            "subject": "sa:t1",
            "namespace": "code-private",
            "role": "viewer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "alice@x",
            "revoked_at": None,
        },
        {
            "subject": "sa:t1",
            "namespace": "secret-notes",
            "role": "writer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "alice@x",
            "revoked_at": None,
        },
        {
            "subject": "sa:other",
            "namespace": "code-private",
            "role": "viewer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "bob@x",
            "revoked_at": None,
        },
    ]
    n = await tg.revoke_token_grants(ORG, "sa:t1", ch=_FakeCH(live, []))
    assert n == 2
    assert {r["namespace"] for r in inserted} == {"code-private", "secret-notes"}
    for r in inserted:
        assert r["subject"] == "sa:t1" and r["revoked_at"] is not None
        assert r["granted_by"] == "alice@x" and r["role"] in {"viewer", "writer"}


@pytest.mark.asyncio
async def test_revoking_the_person_cascades_to_the_tokens_they_minted_on_that_namespace(
    inserted, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = [
        {
            "subject": "sa:t1",
            "namespace": "code-private",
            "role": "viewer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "alice@x",
            "revoked_at": None,
        },
        {
            "subject": "sa:t1",
            "namespace": "secret-notes",
            "role": "writer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "alice@x",
            "revoked_at": None,
        },
        {
            "subject": "sa:t2",
            "namespace": "code-private",
            "role": "viewer",
            "status": "active",
            "granted_at": datetime(2026, 9, 18),
            "granted_by": "alice@x",
            "revoked_at": None,
        },
    ]
    fake = _FakeCH(live, [{"sa_subject": "sa:t1"}, {"sa_subject": "sa:t2"}])
    import server.auth.aliases as aliases

    async def _spellings(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject, "alice"]

    # Every spelling a revocation reaches, an unverified email too.
    monkeypatch.setattr(aliases, "spellings", _spellings)
    touched = await tg.cascade_revoke_to_created_tokens(ORG, "code-private", "alice@x", ch=fake)
    assert touched == 2
    # only the revoked namespace's grants go; the token keeps secret-notes
    assert [(r["subject"], r["namespace"]) for r in inserted] == [
        ("sa:t1", "code-private"),
        ("sa:t2", "code-private"),
    ]
    sa_sql, sa_params = next(x for x in fake.sql if "service_accounts" in x[0])
    assert sa_params["subs"] == ["alice@x", "alice"]


@pytest.mark.asyncio
async def test_grants_revoke_handler_calls_the_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models_grants import GrantsRevokeInput

    async def _find(org: str, namespace: str, subject: str, **_kw: Any) -> GrantState:
        return GrantState.from_row({"role": "viewer", "status": "active", "granted_by": "maint@x"})

    calls: list[tuple[str, str, str]] = []

    async def _cascade(org: str, namespace: str, subject: str, **_kw: Any) -> int:
        calls.append((org, namespace, subject))
        return 1

    inserted: list[dict[str, Any]] = []

    async def _insert(row: dict[str, Any], **_kw: Any) -> None:
        inserted.append(row)

    monkeypatch.setattr(grants_repo, "find_any_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)

    async def _same(org: str, subject: str) -> str:
        return subject

    monkeypatch.setattr(tg, "cascade_revoke_to_created_tokens", _cascade)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _same)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org=ORG, namespace="code-private", subject="alice@x")
    )
    assert out.revoked is True
    assert calls == [(ORG, "code-private", "alice@x")]
    assert inserted and inserted[0]["revoked_at"] is not None


def test_two_mints_in_one_minute_get_distinct_token_names() -> None:
    """service_accounts is keyed on (org, name); a per-minute name would let
    the second mint replace the first's row."""
    from datetime import datetime

    from server.identity.app.me_setup import _mcp_token_name

    a = _mcp_token_name("Claude Desktop", datetime(2026, 9, 18, 14, 41, 12), "33c8e3")
    b = _mcp_token_name("Claude Desktop", datetime(2026, 9, 18, 14, 41, 49), "27d1f2")
    assert a != b
    assert a == "Claude Desktop · 2026-09-18 14:41:12 · 33c8e3"
    # same second, different subject: still distinct
    assert _mcp_token_name("Claude Desktop", datetime(2026, 9, 18, 14, 41, 49), "aaaaaa") != b


@pytest.mark.asyncio
async def test_many_restricted_namespaces_cost_one_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """One synchronous insert per namespace makes the Setup page spin for
    seconds; the grants go in one statement."""
    calls: list[int] = []

    async def _insert(batch: list[dict[str, Any]], **_kw: Any) -> None:
        calls.append(len(batch))

    monkeypatch.setattr(grants_repo, "insert_rows", _insert)

    async def _isr(org: str, ns: str) -> bool:
        return True

    ns_roles = {f"code-private-26-{i}": "admin" for i in range(11)}
    granted = await tg.grant_restricted_to_token(
        ORG,
        "sa:t",
        "alice",
        ns_roles,
        is_restricted=_isr,
        is_code_namespace=lambda o, n: True,
    )
    assert len(granted) == 11 and calls == [11]
