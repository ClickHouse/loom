"""A maintainer retires a repository index — registry and
directory rows inactive, every code_* table keyed by the namespace purged
(through a detached erasure saga), the mirror removed
when orphaned; the ingest worker skips deleted rows."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.index.repo_registry as reg
import server.code.app.repo_admin as admin
import server.code.domain.index.repos as repos
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.api.models_repos import CodeRepoRemoveInput
from server.code.app.index import retire
from server.core.errors import ValidationError
from server.operations.base import Role

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ORG = "clickhouse"


class _CH:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        self.registry_active = True
        self.columns = {
            "code_files": ["org", "namespace", "path"],
            "code_chunks": ["org", "namespace", "chunk_id"],
            "code_search_trace": ["org", "namespace", "query"],
            "code_repos": ["org", "namespace", "repo"],
            "code_scip_batches": ["batch_id"],  # no namespace axis → never purged
        }

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "system.columns" in sql:
            return [{"table": t, "cols": c} for t, c in self.columns.items()]
        return []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        if "loom.code_repos" in sql:
            return {"n": 1 if self.registry_active else 0}
        if "loom.namespaces" in sql:
            return {
                "org": ORG,
                "namespace": (params or {}).get("ns"),
                "active": True,
                "kind": "code",
            }
        return None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append(" ".join(sql.split()))
        if "INSERT INTO loom.code_repos" in sql:
            self.registry_active = False

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_the_scan_names_every_code_table_with_a_namespace_axis_except_telemetry() -> None:
    ch = _CH()
    out = await retire.code_tables_keyed_by_namespace(ch)
    # telemetry, the registry row and a table without a namespace axis stay out
    assert sorted(out) == ["loom.code_chunks", "loom.code_files"]
    assert ch.executed == []  # the scan reads system.columns; the saga mutates


def test_mirror_is_kept_while_another_branch_uses_it() -> None:
    a = SimpleNamespace(org=ORG, namespace="code-w", git_dir="/x/w.git")
    b = SimpleNamespace(org=ORG, namespace="code-w-1-2", git_dir="/x/w.git")
    assert retire.mirror_still_used("/x/w.git", ORG, "code-w", [a, b]) is True
    assert retire.mirror_still_used("/x/w.git", ORG, "code-w", [a]) is False


@pytest.mark.asyncio
async def test_mirror_outside_the_self_service_root_is_never_deleted(tmp_path, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_root", str(tmp_path / "self"))
    operator = tmp_path / "operator.git"
    operator.mkdir()
    cfg = SimpleNamespace(org=ORG, namespace="code-op", git_dir=str(operator), label="op")
    assert await retire.remove_mirror_if_orphaned(cfg, [cfg]) is False and operator.exists()
    mine = tmp_path / "self" / "acme__w.git"
    mine.mkdir(parents=True)
    cfg2 = SimpleNamespace(org=ORG, namespace="code-w", git_dir=str(mine), label="w")
    assert await retire.remove_mirror_if_orphaned(cfg2, [cfg2]) is True and not mine.exists()


@pytest.fixture
def plane(monkeypatch):
    import server.core.ch as ch_mod
    import server.identity.app.namespace as ns_mod
    from server.settings import settings

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(ns_mod, "get_client", lambda: ch)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse", "repo": "ClickHouse/ClickHouse"}]',
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "jwt_secret", "unit")
    reg.reset_for_tests()
    row = {
        "org": ORG,
        "namespace": "code-widgets",
        "repo": "acme/widgets",
        "git_remote": "",
        "git_branch": "main",
        "path_prefixes": [],
        "exclude": [],
        "classification": "standard",
        "join_policy": "open",
        "github_token_enc": "",
    }
    state = {"rows": [row]}

    async def _refresh(*, force: bool = False, ch: Any = None):
        repos.set_registry_repos(
            [reg.row_to_config(r) for r in state["rows"]]
            if ch_mod.get_client().registry_active
            else []
        )
        return repos.registry_repos()

    monkeypatch.setattr(reg, "refresh_db_repos", _refresh)
    monkeypatch.setattr(repos, "_registry_namespaces", repos._registry_namespaces)
    monkeypatch.setattr(repos, "_registry_repos", repos._registry_repos)
    repos.set_registry_repos([reg.row_to_config(row)])

    import server.code.store.index as code_repo_mod

    ch.states = []
    ch.stored = {}  # (org, namespace, key) -> value, what get_state_or_none answers

    class _CodeRepo(code_repo_mod.CodeRepo):
        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            ch.states.append((org, ns, key, value))
            ch.stored[(org, ns, key)] = value

        async def get_state_or_none(self, org: str, ns: str, key: str) -> str | None:
            return ch.stored.get((org, ns, key))

    monkeypatch.setattr(code_repo_mod, "CodeRepo", _CodeRepo)
    import server.erasure.app.saga as erasure_mod

    ch.sagas = []

    async def _detached(req):
        ch.sagas.append(req)
        return "era-1"

    monkeypatch.setattr(erasure_mod, "run_erasure_detached", _detached)
    import contextlib

    from server.core import leases

    ch.leases = []
    ch.registry_active_under_lease = []

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any):  # the document-source lease
        ch.leases.append(name)
        yield True
        # Both retirements ran while it was held.
        ch.registry_active_under_lease.append(ch.registry_active)

    monkeypatch.setattr(leases, "holding", _holding)
    set_current_auth(
        AuthInfo(
            subject="alice@x",
            org=ORG,
            namespaces=("code-widgets",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    yield ch
    set_current_auth(None)
    reg.reset_for_tests()


@pytest.mark.asyncio
async def test_remove_retires_registry_directory_and_rows(plane) -> None:
    out = await admin.handle_code_repo_remove(
        CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
    )
    assert out.registry_deactivated and out.directory_deactivated
    # The rows leave through a detached, loom-only purge saga
    assert out.purged_tables == 2 and out.erasure_id == "era-1"
    (req,) = plane.sagas
    assert req.scope_type == "namespace_list" and req.purge and not req.external
    assert json.loads(req.scope_value) == {
        "namespaces": ["code-widgets"],
        "purge": True,
        "external": False,
    }
    assert req.requester == "alice@x"
    assert (
        plane.inserted
        and plane.inserted[0][0] == "loom.namespaces"
        and plane.inserted[0][1][0]["active"] is False
    )
    assert any(
        "INSERT INTO loom.code_repos SELECT * REPLACE (false AS active" in s for s in plane.executed
    )


@pytest.mark.asyncio
async def test_remove_refusals(plane) -> None:
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_remove(
            CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="nope")
        )
    assert ei.value.to_detail()["code"] == "CONFIRM_REQUIRED"
    with pytest.raises(ValidationError) as ei:  # the operator's entry
        await admin.handle_code_repo_remove(
            CodeRepoRemoveInput(org=ORG, namespace="code-clickhouse", confirm="code-clickhouse")
        )
    assert ei.value.to_detail()["code"] == "REPO_FROM_CONFIG"
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_remove(
            CodeRepoRemoveInput(org=ORG, namespace="nothing", confirm="nothing")
        )
    assert ei.value.to_detail()["code"] == "NOT_A_REPO"


@pytest.mark.asyncio
async def test_ingest_skips_a_namespace_whose_directory_row_is_inactive(monkeypatch) -> None:
    import server.code.app.index.ingest_pass as ci
    import server.core.ch as ch_mod

    class _Dir:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            return [{"org": ORG, "namespace": "code-gone"}]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Dir())
    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [
            SimpleNamespace(org=ORG, namespace="code-gone", label="g"),
            SimpleNamespace(org=ORG, namespace="code-live", label="l"),
        ],
    )
    assert await ci._inactive_namespaces() == {(ORG, "code-gone")}


@pytest.mark.asyncio
async def test_registry_deactivation_filters_the_live_row_before_replacing_active() -> None:
    """ClickHouse resolves a SELECT's REPLACE alias inside that SELECT's WHERE, so the
    `active` filter must sit in a subquery or it reads `false` and the INSERT writes no
    row while the retire reports the registry row deactivated."""
    import re

    ch = _CH()
    assert await retire.deactivate_registry_row(ch, ORG, "code-widgets") is True
    (stmt,) = [s for s in ch.executed if s.startswith("INSERT INTO loom.code_repos")]
    outer, _, inner = stmt.partition("FROM (")
    assert "REPLACE (false AS active" in outer and "WHERE" not in outer
    assert re.search(r"FROM loom\.code_repos FINAL WHERE .*\bAND active\s*\)$", inner)


@pytest.mark.asyncio
async def test_the_scan_leaves_out_views_and_tables_a_refresh_replaces() -> None:
    """A mutation on a `TO` view is forwarded to its target, and one on a table a
    refreshable view replaces whole never finishes: both are left to the statement."""
    ch = _CH()
    seen: list[str] = []
    orig = ch.query_all

    async def spy(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        seen.append(sql)
        return await orig(sql, params)

    ch.query_all = spy  # type: ignore[method-assign]
    await retire.code_tables_keyed_by_namespace(ch)
    sql = seen[0]
    assert "engine NOT LIKE '%View%'" in sql
    assert "engine = 'MaterializedView'" in sql and "NOT match(create_table_query" in sql
    assert r"\\bAPPEND\\b" in sql  # an APPEND refresh target takes mutations like any table


class _RefreshCH:
    def __init__(self, views: list[str], refuse: bool = False) -> None:
        self.views = views
        self.refuse = refuse
        self.executed: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [{"view": v} for v in self.views] if "SELECT name AS view" in sql else []

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if self.refuse:
            raise RuntimeError("ACCESS_DENIED")
        self.executed.append(sql)


@pytest.mark.asyncio
async def test_refresh_code_views_starts_each_refresh_and_tolerates_a_refusal() -> None:
    ch = _RefreshCH(["code_term_df_refresh"])
    assert await retire.refresh_code_views(ch) == ["code_term_df_refresh"]
    assert ch.executed == ["SYSTEM REFRESH VIEW loom.`code_term_df_refresh`"]
    refused = _RefreshCH(["code_term_df_refresh"], refuse=True)
    assert await retire.refresh_code_views(refused) == []


@pytest.mark.asyncio
async def test_remove_retires_the_namespaces_document_sources(plane, monkeypatch) -> None:
    source = {
        "org": ORG,
        "namespace": "code-widgets",
        "name": "design-decks",
        "kind": "gdrive",
        "config_json": '{"folder_ids": ["1AbCdEfGhIjKlMnOp"]}',
        "credential_enc": "sealed",
        "created_by": "alice@x",
        "active": True,
    }
    plain = plane.query_all

    async def _query_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "FROM loom.knowledge_sources" in sql:
            assert params == {"org": ORG, "ns": "code-widgets"}
            retired = any(t == "loom.knowledge_sources" for t, _ in plane.inserted)
            return [] if retired else [source]
        return await plain(sql, params)

    monkeypatch.setattr(plane, "query_all", _query_all)
    out = await admin.handle_code_repo_remove(
        CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
    )
    assert out.sources_deactivated == ["design-decks"]
    [row] = [r for table, rows in plane.inserted if table == "loom.knowledge_sources" for r in rows]
    assert (row["name"], row["active"], row["credential_enc"]) == ("design-decks", False, "")
    assert row["kind"] == "gdrive" and row["created_by"] == "alice@x"


@pytest.mark.asyncio
async def test_a_failed_source_retirement_leaves_the_repository_to_retry(
    plane, monkeypatch
) -> None:
    import server.knowledge.app.sources as knowledge_sources

    async def _fail(org: str, namespace: str) -> list[str]:
        raise RuntimeError("clickhouse went away")

    monkeypatch.setattr(knowledge_sources, "deactivate_namespace_sources", _fail)
    with pytest.raises(RuntimeError):
        await admin.handle_code_repo_remove(
            CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
        )
    # Nothing of the repository was retired, so the same call can run again.
    assert plane.registry_active and plane.executed == [] and plane.inserted == []
    assert plane.sagas == []


@pytest.mark.asyncio
async def test_remove_retires_sources_and_registry_row_under_one_lease(plane) -> None:
    await admin.handle_code_repo_remove(
        CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
    )
    # One lease, the one every source write of the namespace takes, held across
    # both: an add runs wholly before or wholly after the retirement.
    assert plane.leases == [f"knowledge-source:{ORG}:code-widgets"]
    assert plane.registry_active_under_lease == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", ["busy", "down"])
async def test_remove_refuses_without_the_lease_and_writes_nothing(
    plane, monkeypatch, lease: str
) -> None:
    import contextlib

    from server.core import leases
    from server.core.errors import OverloadedError

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any):
        if lease == "down":
            raise leases.LeaseUnavailable("the lease store did not answer")
        yield False

    monkeypatch.setattr(leases, "holding", _holding)
    expected = OverloadedError if lease == "busy" else leases.LeaseUnavailable
    with pytest.raises(expected):
        await admin.handle_code_repo_remove(
            CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
        )
    assert plane.registry_active and plane.inserted == [] and plane.sagas == []
    assert plane.states == []  # a refused removal leaves the live repository's links


@pytest.mark.asyncio
async def test_remove_empties_the_tracker_links_before_it_deactivates_or_purges(
    plane, monkeypatch
) -> None:
    """The purge is detached: a namespace of the same name registered again before it
    runs must not follow the removed branch's source or links."""
    import server.erasure.app.saga as erasure_mod
    from server.knowledge.app import sources
    from server.tracker.app import binding

    def _links() -> list[tuple[str, str, str, str]]:
        keys = (binding.OVERRIDE_KEY, binding.BRANCH_OF_KEY)
        return sorted(r for r in plane.states if r[2] in keys)

    seen: dict[str, list[Any]] = {}
    real_sources, real_registry = (
        sources.deactivate_namespace_sources,
        retire.deactivate_registry_row,
    )
    real_saga = erasure_mod.run_erasure_detached

    async def _sources(*a: Any, **k: Any) -> Any:
        seen["sources"] = _links()
        return await real_sources(*a, **k)

    async def _registry(*a: Any, **k: Any) -> Any:
        seen["registry"] = _links()
        return await real_registry(*a, **k)

    async def _saga(req: Any) -> Any:
        seen["saga"] = _links()
        return await real_saga(req)

    monkeypatch.setattr(sources, "deactivate_namespace_sources", _sources)
    monkeypatch.setattr(retire, "deactivate_registry_row", _registry)
    monkeypatch.setattr(erasure_mod, "run_erasure_detached", _saga)
    plane.stored[(ORG, "code-widgets", binding.OVERRIDE_KEY)] = "tracker-widgets"
    plane.stored[(ORG, "code-widgets", binding.BRANCH_OF_KEY)] = "code-root"
    await admin.handle_code_repo_remove(
        CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
    )
    emptied = [
        (ORG, "code-widgets", binding.OVERRIDE_KEY, ""),
        (ORG, "code-widgets", binding.BRANCH_OF_KEY, ""),
    ]
    assert seen == {"sources": emptied, "registry": emptied, "saga": emptied}


@pytest.mark.asyncio
async def test_remove_writes_no_qa_binding_row_for_a_namespace_that_had_none(
    plane, monkeypatch
) -> None:
    """An empty `qa_binding` reads as cleared: one written for a namespace that never had
    links would hide the operator's map from a namespace of the same name until the purge."""
    from server.tracker.app import binding

    plane.stored[(ORG, "code-widgets", binding.BRANCH_OF_KEY)] = "code-root"
    await admin.handle_code_repo_remove(
        CodeRepoRemoveInput(org=ORG, namespace="code-widgets", confirm="code-widgets")
    )
    keys = (binding.OVERRIDE_KEY, binding.BRANCH_OF_KEY)
    assert [r for r in plane.states if r[2] in keys] == [
        (ORG, "code-widgets", binding.BRANCH_OF_KEY, "")
    ]
