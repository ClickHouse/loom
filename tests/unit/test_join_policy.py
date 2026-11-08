"""A namespace says how it is joined; code namespaces are
read-only and joined, not inherited."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.domain import join_policy as jp
from server.identity.domain.grants import GrantState

# --- resolution -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "approval"),
        ({}, "approval"),
        ({"visibility": "public"}, "open"),  # legacy public row: viewer joined at once
        ({"visibility": "public", "requires_approval": True}, "approval"),
        ({"visibility": "private"}, "approval"),
        ({"visibility": "private", "join_policy": "inherit"}, "approval"),
        ({"visibility": "private", "join_policy": "open"}, "open"),  # explicit wins over private
        ({"visibility": "public", "join_policy": "approval"}, "approval"),
        ({"visibility": "public", "join_policy": "closed"}, "closed"),
        ({"visibility": "public", "join_policy": "nonsense"}, "open"),  # unknown word = inherit
    ],
)
def test_effective_join_policy_folds_inherit_into_the_old_rule(
    row: dict[str, Any] | None, expected: str
) -> None:
    assert jp.effective_join_policy(row) == expected


@pytest.mark.parametrize(
    ("policy", "role", "decision"),
    [
        ("open", "viewer", "active"),
        ("open", "writer", "pending"),
        ("open", "admin", "pending"),
        ("approval", "viewer", "pending"),
        ("approval", "writer", "pending"),
        ("closed", "viewer", "closed"),
        ("closed", "admin", "closed"),
    ],
)
def test_join_gate(policy: str, role: str, decision: str) -> None:
    assert jp.join_gate(policy=policy, role=role) == decision


def test_code_roles_are_reader_and_maintainer_only() -> None:
    """Viewer (reader) and admin (maintainer) both exist on a
    repository and may be requested; writer never does."""
    assert jp.code_role_ceiling_violation("code", "viewer") is None
    assert jp.code_role_ceiling_violation("code", "admin") is None
    assert jp.code_role_ceiling_violation("memory", "writer") is None
    msg = jp.code_role_ceiling_violation("code", "writer")
    assert msg and "readers and maintainers" in msg and "'writer'" in msg


@pytest.mark.asyncio
async def test_maintainer_request_on_an_open_code_namespace_pends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for maintainer is a request, never instant, even where readers
    join at once; a current maintainer decides."""
    import server.identity.app.namespace_membership as ns

    ch = _wire(monkeypatch, _row(join_policy="open"))
    out = await ns.handle_namespace_join(_join("admin"))
    assert out.status == "pending" and out.requested_role == "admin"
    assert ch.inserted[0]["status"] == "pending" and ch.inserted[0]["role"] == "admin"


# --- handler: namespace.join ----------------------------------------------------------


class _CH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserted: list[dict[str, Any]] = []

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return self.row

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []


def _wire(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any] | None) -> _CH:
    import server.identity.app.namespace as registry
    import server.identity.app.namespace_membership as ns
    from server.identity.store import grants as grants_repo

    ch = _CH(row)
    monkeypatch.setattr(ns, "get_client", lambda: ch)
    monkeypatch.setattr(registry, "resolve_subject", lambda *_a, **_k: "alice")

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _insert(row_: dict[str, Any], **_: Any) -> None:
        ch.inserted.append(row_)

    monkeypatch.setattr(grants_repo, "find_any_grant", _none)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    return ch


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "visibility": "private",
        "active": True,
        "requires_approval": False,
        "kind": "code",
        "join_policy": "open",
    }
    base.update(kw)
    return base


def _join(role: str = "viewer"):
    from server.identity.api.models import NamespaceJoinInput

    return NamespaceJoinInput(org="clickhouse", namespace="code-clickhouse", role=role)


@pytest.mark.asyncio
async def test_open_code_namespace_joins_at_once_as_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.identity.app.namespace_membership as ns

    ch = _wire(monkeypatch, _row())
    out = await ns.handle_namespace_join(_join("viewer"))
    assert out.joined is True and out.status == "active" and out.role == "viewer"
    assert (
        ch.inserted and ch.inserted[0]["status"] == "active" and ch.inserted[0]["role"] == "viewer"
    )


@pytest.mark.asyncio
async def test_writer_join_on_a_code_namespace_hits_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.identity.app.namespace_membership as ns
    from server.core.errors import ValidationError

    ch = _wire(monkeypatch, _row())
    with pytest.raises(ValidationError) as ei:
        await ns.handle_namespace_join(_join("writer"))
    assert ei.value.to_detail()["code"] == "ROLE_CEILING"
    assert ch.inserted == []


@pytest.mark.asyncio
async def test_approval_code_namespace_pends_a_viewer_join(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.identity.app.namespace_membership as ns

    ch = _wire(monkeypatch, _row(join_policy="approval"))
    out = await ns.handle_namespace_join(_join("viewer"))
    assert out.status == "pending" and out.requested_role == "viewer"
    assert ch.inserted[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_closed_namespace_answers_a_stranger_as_an_absent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a grant, a closed namespace's refusal would confirm the name exists."""
    import server.identity.app.namespace_membership as ns
    from server.core.errors import NotFoundError

    ch = _wire(monkeypatch, _row(join_policy="closed", kind="code"))
    with pytest.raises(NotFoundError) as closed:
        await ns.handle_namespace_join(_join("viewer"))
    ch_absent = _wire(monkeypatch, None)
    with pytest.raises(NotFoundError) as absent:
        await ns.handle_namespace_join(_join("viewer"))
    assert str(closed.value) == str(absent.value)
    assert ch.inserted == [] and ch_absent.inserted == []


@pytest.mark.asyncio
async def test_a_restricted_namespace_answers_a_stranger_as_an_absent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restricted row is kept out of the directory; a stranger's join must neither
    pend a request nor list its admins, which would confirm the name. The one listed
    shape, a restricted repository whose rule is approval, still takes a request."""
    import server.identity.app.namespace_membership as ns
    from server.core.errors import NotFoundError

    ch = _wire(
        monkeypatch, _row(kind="memory", join_policy="approval", classification="restricted")
    )
    with pytest.raises(NotFoundError):
        await ns.handle_namespace_join(_join("viewer"))
    assert ch.inserted == []

    ch = _wire(monkeypatch, _row(kind="code", join_policy="approval", classification="restricted"))
    out = await ns.handle_namespace_join(_join("viewer"))
    assert out.status == "pending" and ch.inserted[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_member_is_told_the_namespace_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.identity.app.namespace_membership as ns
    from server.core.errors import AuthInsufficientError
    from server.identity.store import grants as grants_repo

    ch = _wire(monkeypatch, _row(join_policy="closed", kind="memory"))

    async def _member(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row({"role": "viewer", "status": "active", "pending_role": None})

    monkeypatch.setattr(grants_repo, "find_any_grant", _member)
    with pytest.raises(AuthInsufficientError) as ei:
        await ns.handle_namespace_join(_join("writer"))
    assert ei.value.to_detail()["join_policy"] == "closed"
    assert ch.inserted == []


@pytest.mark.asyncio
async def test_legacy_public_memory_namespace_keeps_its_instant_viewer_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.identity.app.namespace_membership as ns

    ch = _wire(monkeypatch, _row(kind="memory", visibility="public", join_policy="inherit"))
    out = await ns.handle_namespace_join(_join("viewer"))
    assert out.status == "active"
    out2 = await ns.handle_namespace_join(_join("writer"))
    assert out2.status == "pending", "writer self-joins still pend on a memory namespace"
    assert len(ch.inserted) == 2


@pytest.mark.asyncio
async def test_legacy_private_memory_namespace_still_pends(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.identity.app.namespace_membership as ns

    _wire(monkeypatch, _row(kind="memory", visibility="private", join_policy="inherit"))
    out = await ns.handle_namespace_join(_join("viewer"))
    assert out.status == "pending"


@pytest.mark.asyncio
async def test_a_second_request_stays_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending writer request followed by an admin request is still only a
    request: the first one never becomes an active base role."""
    import server.identity.app.namespace_membership as ns
    from server.identity.domain.grants import GrantState
    from server.identity.store import grants as grants_repo

    ch = _wire(monkeypatch, _row(kind="memory", join_policy="approval"))

    async def _pending_writer(*_: Any, **__: Any) -> GrantState:
        return GrantState.from_row({"role": "writer", "status": "pending", "granted_by": "alice"})

    monkeypatch.setattr(grants_repo, "find_any_grant", _pending_writer)
    out = await ns.handle_namespace_join(_join("admin"))
    assert out.status == "pending" and out.requested_role == "admin"
    (row,) = ch.inserted
    assert (row["status"], row["role"], row["pending_role"]) == ("pending", "admin", None)


# --- registry writers carry the column ----------------------------------------------


def test_registry_row_carries_join_policy_forward() -> None:
    import server.operations  # noqa: F401 — wires the handler modules
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    now = datetime(2026, 9, 16, 12, 0, 0)
    inp = NamespaceRegisterInput(org="clickhouse", namespace="team-x", owner_email="o@x")
    fresh = registry_row(inp, None, now)
    assert fresh["join_policy"] == "inherit"
    kept = registry_row(inp, {"join_policy": "open", "conversation_logging_enabled": False}, now)
    assert kept["join_policy"] == "open", (
        "a settings write must not reset how a namespace is joined"
    )
    explicit = registry_row(
        NamespaceRegisterInput(
            org="clickhouse", namespace="team-x", owner_email="o@x", join_policy="closed"
        ),
        {"join_policy": "open", "conversation_logging_enabled": False},
        now,
    )
    assert explicit["join_policy"] == "closed"


def test_repo_config_public_flag_parses_and_never_applies_to_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.index.repos import configured_repos
    from server.settings import settings

    monkeypatch.setattr(settings, "code_org", "clickhouse")
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_repo", "ClickHouse/ClickHouse")
    monkeypatch.setattr(settings, "code_git_dir", "var/code_repos/clickhouse.git")
    monkeypatch.setattr(settings, "code_repo_public", False)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse", "public": true},'
        ' {"namespace": "code-example-private", "repo": "ClickHouse/example-private",'
        '  "classification": "restricted", "public": true},'
        ' {"namespace": "code-other"}]',
    )
    repos = {r.namespace: r for r in configured_repos()}
    assert repos["code-clickhouse"].public is True
    assert repos["code-example-private"].public is False, "restricted is never public"
    assert repos["code-other"].public is False


@pytest.mark.asyncio
async def test_code_registry_writes_open_for_a_public_repo_and_approval_otherwise() -> None:
    from types import SimpleNamespace

    from server.code.app.index import registry

    class _Client:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def query_one(self, *_: Any, **__: Any) -> None:
            return None

        async def insert(self, _table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            self.rows.extend(rows)

    registry.reset_for_tests()
    c = _Client()
    pub = SimpleNamespace(
        org="clickhouse",
        namespace="code-pub",
        repo="o/r",
        git_branch="master",
        classification="standard",
        public=True,
        org_readable=False,
    )
    priv = SimpleNamespace(
        org="clickhouse",
        namespace="code-priv",
        repo="o/p",
        git_branch="master",
        classification="restricted",
        public=True,
        org_readable=False,
        git_dir="",
    )
    assert await registry.ensure_registered(pub, ch=c) is True
    assert await registry.ensure_registered(priv, ch=c) is True
    by = {r["namespace"]: r for r in c.rows}
    # Open means joinable; the row stays private so authz
    # never reads it as org-readable.
    assert by["code-pub"]["join_policy"] == "open" and by["code-pub"]["visibility"] == "private"
    assert (
        by["code-priv"]["join_policy"] == "approval" and by["code-priv"]["visibility"] == "private"
    )
    assert by["code-priv"]["classification"] == "restricted"
