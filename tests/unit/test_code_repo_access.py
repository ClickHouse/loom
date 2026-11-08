"""A maintainer sets a repository's access and its tracker source.

The access row wins over the deployment's entry (`_with_registry`), the
handler writes registry + directory and drops the classification cache, a
widening needs `confirm`, a restricted index is never open; the QA binding
override is read before the config map and "" clears it."""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth import grant_claims
from server.code.app.index import repo_registry as reg
from server.code.domain.index import repos
from server.core.errors import GrantBusyError, ValidationError
from server.tracker.app import binding

ORG = "clickhouse"


def _cfg(
    ns: str, *, classification: str = "restricted", public: bool = False, source: str = "config"
) -> Any:
    return repos.RepoConfig(
        org=ORG,
        namespace=ns,
        repo="ClickHouse/weave",
        git_dir="var/x.git",
        git_remote="https://github.com/ClickHouse/weave.git",
        git_branch="main",
        path_prefixes=("src/",),
        github_token="",
        org_readable=True,
        classification=classification,
        public=public,
        source=source,
    )


def test_an_access_row_wins_over_the_entry_but_keeps_its_other_fields(monkeypatch) -> None:
    entry = _cfg("code-weave", classification="restricted", public=False)
    row = dataclasses.replace(
        _cfg("code-weave", classification="standard", public=True, source="registry"),
        path_prefixes=("other/",),
        github_token="tok",
    )
    other = _cfg("code-added", classification="standard", public=True, source="registry")
    monkeypatch.setattr(repos, "registry_repos", lambda: [row, other])
    out = repos._with_registry([entry])
    weave = next(c for c in out if c.namespace == "code-weave")
    assert (
        weave.classification == "standard"
        and weave.public is True
        and weave.access_source == "registry"
    )
    assert (
        weave.path_prefixes == ("src/",) and weave.github_token == "" and weave.source == "config"
    )
    assert weave.org_readable is True
    assert [c.namespace for c in out] == ["code-weave", "code-added"]
    # narrowing the other way: restricted row over a standard entry forces org_readable off
    monkeypatch.setattr(
        repos,
        "registry_repos",
        lambda: [dataclasses.replace(row, classification="restricted", public=False)],
    )
    weave2 = repos._with_registry([_cfg("code-weave", classification="standard", public=True)])[0]
    assert (
        weave2.classification == "restricted"
        and weave2.public is False
        and weave2.org_readable is False
    )
    # no row: the entry as it is
    monkeypatch.setattr(repos, "registry_repos", lambda: [])
    assert repos._with_registry([entry])[0].access_source == "config"


class _CH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[str] = []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.queries.append(sql)
        return dict(self.row) if self.row else None

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # The caller holds an admin grant given there, unless a test says otherwise.
        return [
            {
                "namespace": params["ns"][0],
                "subject": params["subs"][0],
                "role": "admin",
                "status": "active",
                "revoked_at": None,
                "granted_by": "ops",
            }
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))


@pytest.mark.asyncio
async def test_set_access_writes_registry_and_directory_and_needs_confirm_to_widen(
    monkeypatch,
) -> None:
    from server.code.app import repo_admin as h
    from server.identity.app import namespace_classification as nc

    cfg = _cfg("code-weave", classification="restricted")
    ch = _CH(
        {
            "org": ORG,
            "namespace": "code-weave",
            "classification": "restricted",
            "join_policy": "approval",
            "visibility": "private",
            "kind": "code",
        }
    )
    written: list[dict[str, Any]] = []

    async def refresh(*a, **k):
        return []

    async def upsert(c, *, classification, join_policy, created_by, ch=None):
        written.append(
            {
                "ns": c.namespace,
                "classification": classification,
                "join_policy": join_policy,
                "by": created_by,
            }
        )

    resets: list[str] = []
    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    monkeypatch.setattr(reg, "upsert_access", upsert)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: cfg if ns == "code-weave" else None,
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(nc, "reset", lambda: resets.append("reset"))
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))
    inp = SimpleNamespace(
        org=ORG, namespace="code-weave", classification="standard", join_policy="open", confirm=""
    )
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_access(inp)
    assert e.value.to_detail()["code"] == "CONFIRM_REQUIRED" and written == []
    inp.confirm = "code-weave"
    out = await h.handle_code_repo_set_access(inp)
    assert written == [
        {"ns": "code-weave", "classification": "standard", "join_policy": "open", "by": "alice"}
    ]
    assert [t for t, _ in ch.inserts] == ["loom.namespaces"]
    new_row = ch.inserts[0][1][0]
    assert (
        new_row["classification"] == "standard"
        and new_row["join_policy"] == "open"
        and new_row["visibility"] == "private"
    )
    assert resets == ["reset"]
    assert out.previous_classification == "restricted" and out.previous_join_policy == "approval"
    assert (
        out.classification == "standard"
        and out.directory_updated is True
        and out.effective_within_s > 0
    )
    # a restricted index is never open; unknown values are refused; not a repo
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org=ORG,
                namespace="code-weave",
                classification="restricted",
                join_policy="open",
                confirm="",
            )
        )
    assert e.value.to_detail()["code"] == "RESTRICTED_NOT_OPEN"
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org=ORG,
                namespace="code-weave",
                classification="secret",
                join_policy="open",
                confirm="",
            )
        )
    assert e.value.to_detail()["code"] == "BAD_CLASSIFICATION"
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org=ORG,
                namespace="code-nope",
                classification="standard",
                join_policy="open",
                confirm="code-nope",
            )
        )
    assert e.value.to_detail()["code"] == "NOT_A_REPO"
    # narrowing needs no confirm and notes the restriction
    noted: list[str] = []
    monkeypatch.setattr(nc, "note_restricted", lambda org, ns: noted.append(ns))
    out = await h.handle_code_repo_set_access(
        SimpleNamespace(
            org=ORG,
            namespace="code-weave",
            classification="restricted",
            join_policy="approval",
            confirm="",
        )
    )
    assert out.classification == "restricted" and noted == ["code-weave"]


@pytest.mark.asyncio
async def test_set_source_writes_the_binding_state_and_the_override_is_read_first(
    monkeypatch,
) -> None:
    from server.code.app import repo_admin as h

    cfg = _cfg("code-weave", classification="standard", public=True)
    states: list[tuple[str, str, str, str]] = []

    class _Repo:
        async def get_state(self, org, ns, key):
            return next((v for o, n, k, v in reversed(states) if (o, n, k) == (org, ns, key)), "")

        async def set_state(self, org, ns, key, value, detail=""):
            states.append((org, ns, key, value))

    async def refresh(*a, **k):
        return []

    async def refresh_over(*, force=False, ch=None):
        # the "state" after a write: what set_state recorded last
        binding.reset_overrides_for_tests({(o, ns): v for o, ns, _k, v in states})
        return None

    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: cfg if ns == "code-weave" else None,
    )
    # the operator's entries, the only ones the config glob links
    monkeypatch.setattr(
        "server.code.domain.index.repos.configured_repos",
        lambda: [cfg, _cfg("code-other", classification="standard")],
    )
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda: _Repo())
    monkeypatch.setattr(binding, "refresh_overrides", refresh_over)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace",
        lambda org, ns: object() if ns in ("tracker-weave", "tracker-clickhouse") else None,
    )
    monkeypatch.setattr(
        binding.settings,
        "tracker_code_namespace_bindings_json",
        '{"code-*": "tracker-clickhouse"}',
        raising=False,
    )
    binding.reset_overrides_for_tests()
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))
    assert (
        binding.tracker_namespace_for(ORG, "code-weave") == "tracker-clickhouse"
    )  # the config glob
    out = await h.handle_code_repo_set_source(
        SimpleNamespace(org=ORG, namespace="code-weave", tracker_namespaces=["tracker-weave"])
    )
    assert states == [(ORG, "code-weave", "qa_binding", "tracker-weave")]
    assert out.previous_tracker_namespaces == ["tracker-clickhouse"] and out.tracker_namespaces == [
        "tracker-weave"
    ]
    assert (
        binding.tracker_namespace_for(ORG, "code-weave") == "tracker-weave"
    )  # the override wins over the glob
    # [] clears the links: the code namespace has no tracker record at all
    out = await h.handle_code_repo_set_source(
        SimpleNamespace(org=ORG, namespace="code-weave", tracker_namespaces=[])
    )
    assert (
        out.previous_tracker_namespaces == ["tracker-weave"]
        and binding.tracker_namespace_for(ORG, "code-weave") == ""
    )
    assert (
        binding.tracker_namespace_for(ORG, "code-other") == "tracker-clickhouse"
    )  # others still follow the config
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_source(
            SimpleNamespace(org=ORG, namespace="code-weave", tracker_namespaces=["tracker-nope"])
        )
    assert e.value.to_detail()["code"] == "NOT_A_TRACKER_NAMESPACE"
    binding.reset_overrides_for_tests()


@pytest.mark.asyncio
async def test_upsert_access_copies_an_existing_registry_row_and_builds_one_for_an_entry() -> None:
    """The access row is a new version of the registry row: an existing row
    keeps every other column (a later migration's too); an entry that had
    none gets one from the repository as configured."""

    class _CH2(_CH):
        pass

    existing = {
        "org": ORG,
        "namespace": "code-x",
        "repo": "ClickHouse/x",
        "git_remote": "r",
        "git_branch": "main",
        "path_prefixes": ["a/"],
        "exclude": [],
        "classification": "restricted",
        "join_policy": "approval",
        "github_token_enc": "enc",
        "created_by": "bob",
        "active": True,
        "created_at": "2026-09-01 00:00:00",
        "updated_at": "2026-09-01 00:00:00",
        "version": 1,
        "exclude_generated": True,
        "scip_producer": "go",
    }
    ch = _CH2(existing)
    await reg.upsert_access(
        _cfg("code-x"), classification="standard", join_policy="open", created_by="alice", ch=ch
    )
    ((table, rows),) = ch.inserts
    row = rows[0]
    assert (
        table == "loom.code_repos"
        and row["classification"] == "standard"
        and row["join_policy"] == "open"
    )
    assert row["created_by"] == "alice" and row["version"] > 1 and row["exclude_generated"] is True
    assert (
        row["scip_producer"] == "go"
        and row["github_token_enc"] == "enc"
        and row["path_prefixes"] == ["a/"]
    )
    ch2 = _CH2(None)
    await reg.upsert_access(
        _cfg("code-weave", classification="restricted"),
        classification="standard",
        join_policy="open",
        created_by="alice",
        ch=ch2,
    )
    row = ch2.inserts[0][1][0]
    assert (
        row["repo"] == "ClickHouse/weave"
        and row["git_branch"] == "main"
        and row["path_prefixes"] == ["src/"]
    )
    assert row["classification"] == "standard" and row["active"] is True and "created_at" in row


@pytest.mark.asyncio
async def test_restricting_a_page_branch_revokes_the_copy_a_sibling_gave_it(monkeypatch) -> None:
    from server.code.app import repo_admin as h
    from server.code.app.index import maintainers
    from server.identity.app import namespace_classification as nc

    mine = _cfg("code-weave-rc", classification="standard", source="registry")
    sibling = dataclasses.replace(mine, namespace="code-weave-dev", git_branch="dev")
    snapshot = [sibling, mine]
    copy = {
        "namespace": "code-weave-rc",
        "subject": "mallory",
        "role": "admin",
        "status": "active",
        "revoked_at": None,
        "granted_at": "2026-09-01 00:00:00",
        "granted_by": "repo-maintainers:code-weave-dev",
        "version": 1,
    }
    grants = [
        copy,
        copy | {"namespace": "code-weave-dev", "granted_by": "mallory", "version": 2},
        copy | {"subject": "alice", "granted_by": "ops", "version": 3},  # given there
    ]

    class _Directory(_CH):
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "loom.namespaces" in sql:  # the restriction written a moment ago
                written = [r for t, rows in self.inserts if t == "loom.namespaces" for r in rows]
                return [
                    {"namespace": r["namespace"], "classification": "restricted"} for r in written
                ]
            return [g for g in grants if g["namespace"] in params["ns"]]

        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            if "namespace_grants" in sql:
                key = (params["ns"], params["sub"])
                return next((g for g in grants if (g["namespace"], g["subject"]) == key), None)
            return await super().query_one(sql, params)

    ch = _Directory({"org": ORG, "namespace": "code-weave-rc", "classification": "standard"})
    held: list[str] = []
    real = grant_claims.admins_change

    @asynccontextmanager
    async def admins(org: str, ns: str) -> Any:
        async with real(org, ns):
            held.append(ns)
            yield
            held.remove(ns)

    async def insert(table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        # The restriction's directory row is written under the repository's admins
        # claim, which a revoke holds, then the branch's, which a copy holds.
        if table == "loom.namespaces":
            assert held == ["repo:ClickHouse/weave", "code-weave-rc"]
        ch.inserts.append((table, rows))

    monkeypatch.setattr(grant_claims, "admins_change", admins)
    monkeypatch.setattr(h, "admins_change", admins)
    monkeypatch.setattr(ch, "insert", insert)

    refreshed: list[bool] = []

    async def refresh(*a, **k):
        from server.core.ch import sequential_reads_active

        if "ch" in k:  # the one after the claims: the sync reads this snapshot
            refreshed.append(sequential_reads_active())
        return []

    async def upsert(*a, **k):
        # The directory row first: a restriction cut short after it still stops copies.
        assert ch.inserts[-1][0] == "loom.namespaces"

    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    monkeypatch.setattr(reg, "upsert_access", upsert)
    monkeypatch.setattr(reg, "registry_read", lambda: True)
    monkeypatch.setattr(repos, "configured_repos", lambda: snapshot)
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda org, ns: mine)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(nc, "note_restricted", lambda org, ns: None)
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))
    monkeypatch.setattr(h, "restrict_entry", lambda cfg: None)
    maintainers.reset_for_tests()
    inp = SimpleNamespace(
        org=ORG,
        namespace="code-weave-rc",
        classification="restricted",
        join_policy="approval",
        confirm="",
    )
    await h.handle_code_repo_set_access(inp)
    written = [r for t, rows in ch.inserts if t == "loom.namespace_grants" for r in rows]
    on_it = [r for r in written if r["namespace"] == "code-weave-rc"]
    assert [(r["subject"], r["revoked_at"] is not None) for r in on_it] == [("mallory", True)]

    # A sync that fails leaves the decision written and the call answered (so audited):
    # the ingest pass finishes it. A widening syncs too, bringing the siblings back.
    synced: list[str] = []

    async def busy(org: str, ns: str, **k: Any) -> int:
        synced.append(ns)
        raise GrantBusyError("busy")

    monkeypatch.setattr(maintainers, "sync_repo_maintainers", busy)
    ch.inserts.clear()
    await h.handle_code_repo_set_access(inp)
    assert [t for t, _ in ch.inserts] == ["loom.namespaces"]
    inp.classification, inp.confirm = "standard", "code-weave-rc"
    await h.handle_code_repo_set_access(inp)
    assert synced == ["code-weave-rc", "code-weave-rc"]
    assert refreshed and all(refreshed)


@pytest.mark.parametrize(
    ("source", "snapshot", "classification"),
    [
        ("registry", "standard", "restricted"),
        ("registry", "restricted", "standard"),
        ("config", "standard", "restricted"),
    ],
)
@pytest.mark.asyncio
async def test_a_page_branch_with_no_directory_row_gets_its_first_one(
    monkeypatch, source: str, snapshot: str, classification: str
) -> None:
    """A copy onto it reads that row under the branch's admins claim, and the ingest pass
    may not reach the branch for a long time (a pending clone, an org with model calls
    off): the access change writes the row itself. An operator entry's row stays the
    registrar's to write."""
    from server.code.app import repo_admin as h
    from server.code.app.index import maintainers

    async def refresh(*a, **k):
        return []

    async def upsert(*a, **k):
        return None

    async def sync(*a, **k):
        return None

    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    monkeypatch.setattr(reg, "upsert_access", upsert)
    monkeypatch.setattr(maintainers, "sync_quietly", sync)
    monkeypatch.setattr(h, "restrict_entry", lambda cfg: None)
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: _cfg("code-weave-rc", classification=snapshot, source=source),
    )
    ch = _CH(None)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    inp = SimpleNamespace(
        org=ORG,
        namespace="code-weave-rc",
        classification=classification,
        join_policy="approval",
        confirm="",
    )
    if snapshot == "restricted":  # a widening still needs confirm
        with pytest.raises(ValidationError):
            await h.handle_code_repo_set_access(inp)
        inp.confirm = "code-weave-rc"
    out = await h.handle_code_repo_set_access(inp)
    if source == "config":
        assert ch.inserts == [] and not out.directory_updated
        return
    ((table, (row,)),) = ch.inserts
    assert table == "loom.namespaces" and out.directory_updated
    assert (row["namespace"], row["kind"], row["classification"], row["join_policy"]) == (
        "code-weave-rc",
        "code",
        classification,
        "approval",
    )


@pytest.mark.asyncio
async def test_widening_needs_confirm_when_the_row_read_now_says_restricted(monkeypatch) -> None:
    """This replica's snapshot may still say standard after another restricted it."""
    from server.code.app import repo_admin as h

    async def refresh(*a, **k):
        return []

    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    from server.code.app.index import maintainers

    async def sync(*a, **k):
        return None

    monkeypatch.setattr(maintainers, "sync_quietly", sync)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: _cfg("code-weave", classification="standard", source="registry"),
    )
    ch = _CH({"org": ORG, "namespace": "code-weave", "classification": "restricted"})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org=ORG,
                namespace="code-weave",
                classification="standard",
                join_policy="approval",
                confirm="",
            )
        )
    assert e.value.to_detail()["code"] == "CONFIRM_REQUIRED" and ch.inserts == []

    # With confirm, the reply names the classification the directory row had.
    async def upsert(*a, **k):
        return None

    monkeypatch.setattr(reg, "upsert_access", upsert)
    monkeypatch.setattr(h, "restrict_entry", lambda cfg: None)
    out = await h.handle_code_repo_set_access(
        SimpleNamespace(
            org=ORG,
            namespace="code-weave",
            classification="standard",
            join_policy="approval",
            confirm="code-weave",
        )
    )
    assert out.previous_classification == "restricted"


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.asyncio
async def test_a_stray_copy_cannot_widen_a_restricted_branch(monkeypatch, revoked: bool) -> None:
    """Its holder could otherwise widen the branch before the next sync revokes the copy,
    and keep it for good; a copy revoked since the gate passed counts as none either."""
    from server.code.app import repo_admin as h
    from server.core.errors import AuthInsufficientError

    async def refresh(*a, **k):
        return []

    mine = _cfg("code-weave-rc", classification="restricted", source="registry")
    sibling = dataclasses.replace(mine, namespace="code-weave-dev", classification="standard")
    monkeypatch.setattr(reg, "refresh_db_repos", refresh)
    monkeypatch.setattr(repos, "configured_repos", lambda: [sibling, mine])
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda org, ns: mine)
    seen: list[list[str]] = []

    class _Stray(_CH):
        async def query_all(self, sql, params=None):
            seen.append(params["subs"])
            return [
                {
                    "namespace": "code-weave-rc",
                    "subject": "Mallory@corp.example",  # another spelling of the caller
                    "role": "admin",
                    "status": "active",
                    "revoked_at": "2026-09-01 00:00:00" if revoked else None,
                    "granted_by": "repo-maintainers:code-weave-dev",
                }
            ]

    async def aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject, "Mallory@corp.example"]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    ch = _Stray({"org": ORG, "namespace": "code-weave-rc", "classification": "restricted"})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(h, "current_caller", lambda: ("mallory", ORG))
    with pytest.raises(AuthInsufficientError):
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org=ORG,
                namespace="code-weave-rc",
                classification="standard",
                join_policy="approval",
                confirm="code-weave-rc",
            )
        )
    assert ch.inserts == [] and seen == [["mallory", "mallory@corp.example"]]
