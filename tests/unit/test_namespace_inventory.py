"""namespace.inventory: each plane and population answers on its own, a narrowed read
scope bounds every memory statement and leaves out what it cannot bound, and members
and observed identities stay separate populations."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.auth import sharing
from server.core.principals import personal_name
from server.identity.api.models_inventory import NamespaceInventoryInput
from server.identity.app import namespace_inventory
from server.identity.domain.read_scope import ReadFilters, read_filters_bound
from tests.unit.ch_fake import RecordingCH

AT = datetime(2026, 1, 2, 3, 4, 5)
# Modules that imported `get_client` by name.
_GET_CLIENT = ("server.tasks.app.run",)


def _ch(*first: tuple[str, Any]) -> RecordingCH:
    """Routes for every inventory read; `first` routes are tried before them."""
    ch = RecordingCH()
    for needle, answer in first:
        ch.on(needle, answer)
    return (
        ch.on("latest FROM loom.memory FINAL", [{"n": 7, "latest": AT}])
        .on("FROM loom.memory_records", [{"n": 0, "latest": None}])
        .on("FROM loom.agent_runs", [{"n": 2, "latest": AT}])
        .on("FROM loom.documents", [{"n": 3, "latest": AT}])
        .on("FROM loom.memory_webhooks", [{"n": 1, "latest": AT}])
        .on("loom.namespace_grants", [{"people": 2, "shared": 0}])
        .on("SELECT agent AS name", [{"name": "bot", "n": 7, "total": 1}])
        .on("SELECT actor AS name", [{"name": "u1@x", "n": 5, "total": 2}])
        .on("SELECT user_id AS name", [{"name": "u1", "n": 4, "total": 3}])
    )


@pytest.fixture(autouse=True)
def _no_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(monkeypatch, {})


def _tree(monkeypatch: pytest.MonkeyPatch, parent: dict[str, str]) -> None:
    async def tree(org: str) -> sharing.Tree:
        return sharing.Tree(parent=parent)

    monkeypatch.setattr(namespace_inventory.sharing, "tree", tree)


def _run(scope: ReadFilters | None = None, namespace: str = "n", **kw: Any) -> Any:
    inp = NamespaceInventoryInput(org="o", namespace=namespace, **kw)
    if scope is None:
        return asyncio.run(namespace_inventory.handle_namespace_inventory(inp))
    with read_filters_bound(None, scope):
        return asyncio.run(namespace_inventory.handle_namespace_inventory(inp))


def test_every_plane_and_population_is_counted_namespace_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ch().install(monkeypatch, *_GET_CLIENT)
    out = _run()
    sections = {s.plane: s for s in out.sections}
    assert [s.plane for s in out.sections] == [
        "memories",
        "records",
        "documents",
        "runs",
        "integrations",
    ]
    assert all(s.complete for s in out.sections)
    assert (sections["memories"].count, sections["memories"].latest_at) == (7, AT)
    # No rows: the epoch max() is not reported as a write.
    assert (sections["records"].count, sections["records"].latest_at) == (0, None)
    assert sections["documents"].count == 3 and sections["integrations"].count == 1
    pops = {p.population: p for p in out.populations}
    assert pops["members"].total == 2 and pops["members"].top == []
    assert [(i.name, i.rows) for i in pops["subjects"].top] == [("u1", 4)]
    assert (pops["agents"].total, pops["authors"].total, pops["subjects"].total) == (1, 2, 3)
    assert out.scope.read_scope == "namespace" and out.scope.user_id is None


def test_an_unreadable_admin_check_is_the_servers_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unverified(*_: Any, **__: Any) -> None:
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]  # as marks_unverified sets it
        raise e

    noted: list[bool] = []
    _ch().install(monkeypatch, *_GET_CLIENT)
    monkeypatch.setattr(namespace_inventory, "check_authorized", unverified)
    monkeypatch.setattr(namespace_inventory, "note_answered_unverified", lambda: noted.append(True))
    sections = {s.plane: s for s in _run().sections}
    assert sections["integrations"].reason.startswith("could not be read")
    assert noted == [True]
    assert all(s.complete for p, s in sections.items() if p != "integrations")


def test_members_shared_wider_are_not_a_complete_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _ch(("loom.namespace_grants", [{"people": 3, "shared": 1}])).install(monkeypatch, *_GET_CLIENT)
    (members,) = [p for p in _run().populations if p.population == "members"]
    assert (members.total, members.complete) == (3, False)
    assert (
        members.reason == "shared with everyone, a group or an agent: those members are not counted"
    )


def test_a_collection_counts_the_grant_holders_of_its_whole_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(monkeypatch, {"col-n": "col-mid", "col-mid": "col-root", "col-root": ""})
    ch = _ch(("loom.namespace_grants", [{"people": 2, "shared": 0}])).install(
        monkeypatch, *_GET_CLIENT
    )
    (members,) = [p for p in _run(namespace="col-n").populations if p.population == "members"]
    assert (members.total, members.complete) == (2, True)
    (counted,) = ch.statements("loom.namespace_grants")
    assert counted.params["lineage"] == ["col-n", "col-mid", "col-root"]


def test_a_plain_namespace_counts_its_own_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    _run()
    (counted,) = ch.statements("loom.namespace_grants")
    assert counted.params["lineage"] == ["n"]


def test_the_lineage_statement_binds_both_grant_reads() -> None:
    from server.identity.store import grants as grants_repo

    lineage_sql = grants_repo._LINEAGE_PEOPLE
    assert lineage_sql.count("namespace IN {lineage:Array(String)}") == 2
    assert "{ns:String}" not in lineage_sql


def test_a_personal_collection_is_not_a_complete_member_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    personal = personal_name("o", "ann@example.com")
    (members,) = [p for p in _run(namespace=personal).populations if p.population == "members"]
    assert (members.total, members.complete) == (None, False)
    assert not ch.statements("loom.namespace_grants")


def test_an_org_readable_code_namespace_is_not_a_complete_member_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def readable(org: str, ns: str) -> bool:
        return True

    monkeypatch.setattr(namespace_inventory, "is_org_readable_code_namespace", readable)
    _ch().install(monkeypatch, *_GET_CLIENT)
    (members,) = [p for p in _run().populations if p.population == "members"]
    assert (members.total, members.complete) == (2, False)
    assert members.reason == "readable by everyone in the org: those members are not counted"


def test_a_plain_namespace_never_reads_the_collection_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(org: str) -> sharing.Tree:
        raise RuntimeError("tree unreadable")

    monkeypatch.setattr(namespace_inventory.sharing, "tree", broken)
    _ch().install(monkeypatch, *_GET_CLIENT)
    (members,) = [p for p in _run().populations if p.population == "members"]
    assert (members.total, members.complete) == (2, True)


def test_an_unexpected_admin_check_error_leaves_the_rest_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("directory unreadable")

    _ch().install(monkeypatch, *_GET_CLIENT)
    monkeypatch.setattr(namespace_inventory, "check_authorized", broken)
    sections = {s.plane: s for s in _run().sections}
    assert sections["integrations"].reason == "could not be read (RuntimeError)"
    assert all(s.complete for p, s in sections.items() if p != "integrations")


def test_an_outage_fails_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import BackendUnavailableError

    def down(sql: str, params: dict[str, Any]) -> Any:
        raise BackendUnavailableError("clickhouse down")

    _ch(("FROM loom.agent_runs", down)).install(monkeypatch, *_GET_CLIENT)
    with pytest.raises(BackendUnavailableError):
        _run()


def test_a_narrowed_scope_bounds_every_read_and_leaves_out_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    out = _run(ReadFilters(scope="own_user", user_id="u1", session_id=None))
    # Documents and grants are the namespace's under any scope, as knowledge.* and
    # grants.list read them.
    bounded = [
        c for c in ch.calls if "loom.documents" not in c.sql and "namespace_grants" not in c.sql
    ]
    assert bounded
    for st in bounded:
        assert "{scope_uid:String}" in st.sql and st.params["scope_uid"] == "u1", st.sql
    assert [s.count for s in out.sections if s.plane == "documents"] == [3]
    assert not ch.statements("loom.memory_webhooks")
    assert not ch.statements("loom.memory_records")
    missing = {s.plane for s in out.sections if not s.complete}
    assert missing == {"records", "integrations"}
    pops = {p.population: p for p in out.populations}
    assert (pops["members"].total, pops["members"].complete) == (2, True)
    assert (out.scope.read_scope, out.scope.user_id, out.scope.session_id) == (
        "own_user",
        "u1",
        None,
    )


def test_an_own_session_scope_names_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    out = _run(ReadFilters(scope="own_session", user_id="u1", session_id="s1"))
    bounded = [c for c in ch.calls if "documents" not in c.sql and "grants" not in c.sql]
    assert bounded
    assert all(st.params["scope_sid"] == "s1" for st in bounded)
    assert not ch.statements("loom.memory_records") and not ch.statements("loom.memory_webhooks")
    assert (out.scope.user_id, out.scope.session_id) == ("u1", "s1")


def test_a_failed_read_is_labeled_and_the_rest_still_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(sql: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("table missing")

    routes = (("FROM loom.memory_records", boom), ("SELECT actor AS name", boom))
    _ch(*routes).install(monkeypatch, *_GET_CLIENT)
    out = _run()
    sections = {s.plane: s for s in out.sections}
    assert not sections["records"].complete and sections["records"].count is None
    assert sections["records"].reason == "could not be read (RuntimeError)"
    assert all(s.complete for p, s in sections.items() if p != "records")
    pops = {p.population: p for p in out.populations}
    assert not pops["authors"].complete and pops["subjects"].complete


def test_integrations_are_counted_for_admins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("not an admin")

    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    monkeypatch.setattr(namespace_inventory, "check_authorized", refuse)
    out = _run()
    (integrations,) = [s for s in out.sections if s.plane == "integrations"]
    assert not integrations.complete and integrations.count is None
    assert integrations.reason == "counted for namespace admins only"
    assert not ch.statements("loom.memory_webhooks")


def test_a_zero_population_limit_keeps_the_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch, *_GET_CLIENT)
    out = _run(population_limit=0)
    observed = [p for p in out.populations if p.population != "members"]
    assert all(p.top == [] and p.total for p in observed)
    assert all(st.params["lim"] == 1 for st in ch.statements(" AS name"))


@pytest.mark.parametrize(("llm_fake", "embedder_fake"), [(True, False), (False, True)])
def test_capabilities_say_what_serves_the_process(
    monkeypatch: pytest.MonkeyPatch, llm_fake: bool, embedder_fake: bool
) -> None:
    _ch().install(monkeypatch, *_GET_CLIENT)
    monkeypatch.setattr(namespace_inventory, "llm_is_fake", lambda: llm_fake)
    monkeypatch.setattr(namespace_inventory, "embedder_is_fake", lambda: embedder_fake)
    enabled = {c.name: c.enabled for c in _run().capabilities}
    assert enabled == {"llm": not llm_fake, "embedder": not embedder_fake}


def test_an_outage_cancels_the_other_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import BackendUnavailableError

    cancelled: list[str] = []

    async def down(org: str, ns: str) -> Any:
        await asyncio.sleep(0)
        raise BackendUnavailableError("clickhouse down")

    async def slow(org: str, ns: str, column: str, *, limit: int) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(column)
            raise

    _ch().install(monkeypatch, *_GET_CLIENT)
    monkeypatch.setattr(namespace_inventory, "memory_tally", down)
    monkeypatch.setattr(namespace_inventory, "identity_tally", slow)
    with pytest.raises(BackendUnavailableError):
        _run()
    assert sorted(cancelled) == ["actor", "agent", "user_id"]
