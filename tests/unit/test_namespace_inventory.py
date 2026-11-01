"""namespace.inventory: each plane and population answers on its own, a narrowed read
scope bounds every memory statement and leaves out what it cannot bound, and members
and observed identities stay separate populations."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.identity.api.models_inventory import NamespaceInventoryInput
from server.identity.app import namespace_inventory
from server.identity.domain.read_scope import ReadFilters, read_filters_bound
from tests.unit.ch_fake import RecordingCH

AT = datetime(2026, 1, 2, 3, 4, 5)


def _ch(*first: tuple[str, Any]) -> RecordingCH:
    """Routes for every inventory read; `first` routes are tried before them."""
    ch = RecordingCH()
    for needle, answer in first:
        ch.on(needle, answer)
    return (
        ch.on("latest FROM loom.memory FINAL", [{"n": 7, "latest": AT}])
        .on("FROM loom.memory_records", [{"n": 0, "latest": datetime(1970, 1, 1)}])
        .on("FROM loom.agent_runs", [{"n": 2, "latest": AT}])
        .on("FROM loom.documents", [{"n": 3, "latest": AT}])
        .on("FROM loom.memory_webhooks", [{"n": 1, "latest": AT}])
        .on("loom.namespace_grants", [{"n": 2}])
        .on("SELECT agent AS name", [{"name": "bot", "n": 7, "total": 1}])
        .on("SELECT actor AS name", [{"name": "u1@x", "n": 5, "total": 2}])
        .on("SELECT user_id AS name", [{"name": "u1", "n": 4, "total": 3}])
    )


def _run(scope: ReadFilters | None = None, **kw: Any) -> Any:
    inp = NamespaceInventoryInput(org="o", namespace="n", **kw)
    if scope is None:
        return asyncio.run(namespace_inventory.handle_namespace_inventory(inp))
    with read_filters_bound(None, scope):
        return asyncio.run(namespace_inventory.handle_namespace_inventory(inp))


def test_every_plane_and_population_is_counted_namespace_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ch().install(monkeypatch)
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


def test_the_unset_identity_is_never_a_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch)
    _run()
    (subjects,) = ch.statements("SELECT user_id AS name")
    assert "user_id NOT IN ('', '-')" in subjects.sql


def test_a_narrowed_scope_bounds_every_read_and_leaves_out_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ch().install(monkeypatch)
    out = _run(ReadFilters(scope="own_user", user_id="u1", session_id=None))
    assert ch.calls
    for st in ch.calls:
        assert "{scope_uid:String}" in st.sql and st.params["scope_uid"] == "u1", st.sql
    assert not ch.statements("loom.documents") and not ch.statements("loom.namespace_grants")
    assert not ch.statements("loom.memory_webhooks")
    missing = {s.plane for s in out.sections if not s.complete}
    assert missing == {"documents", "integrations"}
    pops = {p.population: p for p in out.populations}
    assert not pops["members"].complete and pops["members"].total is None
    assert (out.scope.read_scope, out.scope.user_id, out.scope.session_id) == (
        "own_user",
        "u1",
        None,
    )


def test_an_own_session_scope_names_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch)
    out = _run(ReadFilters(scope="own_session", user_id="u1", session_id="s1"))
    assert all(st.params["scope_sid"] == "s1" for st in ch.calls)
    assert (out.scope.user_id, out.scope.session_id) == ("u1", "s1")


def test_a_failed_read_is_labeled_and_the_rest_still_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(sql: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("table missing")

    _ch(("FROM loom.memory_records", boom), ("SELECT actor AS name", boom)).install(monkeypatch)
    out = _run()
    sections = {s.plane: s for s in out.sections}
    assert not sections["records"].complete and sections["records"].count is None
    assert sections["records"].reason == "read failed: RuntimeError"
    assert all(s.complete for p, s in sections.items() if p != "records")
    pops = {p.population: p for p in out.populations}
    assert not pops["authors"].complete and pops["subjects"].complete


def test_integrations_are_counted_for_admins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("not an admin")

    ch = _ch().install(monkeypatch)
    monkeypatch.setattr(namespace_inventory, "check_authorized", refuse)
    out = _run()
    (integrations,) = [s for s in out.sections if s.plane == "integrations"]
    assert not integrations.complete and integrations.count is None
    assert not ch.statements("loom.memory_webhooks")


def test_a_zero_population_limit_keeps_the_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _ch().install(monkeypatch)
    out = _run(population_limit=0)
    observed = [p for p in out.populations if p.population != "members"]
    assert all(p.top == [] and p.total for p in observed)
    assert all(st.params["lim"] == 1 for st in ch.statements(" AS name"))
