"""The mirror registry (`loom.knowledge_mirrors`) and its erasure: the store's statements
on clickhouse-local, a namespace erasure's steps over the mirrors as a purge, and the
census that keeps a name with a live mirror from being taken
(docs/architecture/document-sources.md)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store.mutations import purge_mode, purge_scope
from server.identity.store import namespace_census
from server.knowledge.app import mirrors as app_mirrors
from server.knowledge.store import mirrors
from tests.unit.ch_fake import RecordingCH
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl
from tests.unit.erasure_saga_stub import saga_runner
from tests.unit.schema_ddl import column_names

ORG = "o"
MIRROR = "acl-" + "a" * 24
OLD_MIRROR = "acl-" + "b" * 24

needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse not installed")


@needs_clickhouse
def test_the_registry_keeps_every_mirror_a_namespace_had(tmp_path: Path) -> None:
    ch = KeptCH(tmp_path, *[ddl(t.removeprefix("loom.")) for t in namespace_census.STATE_TABLES])

    async def scenario() -> None:
        await ch.insert(mirrors.TABLE, [{"org": ORG, "namespace": "team", "mirror": OLD_MIRROR}])
        await mirrors.forget_mirrors(ORG, {OLD_MIRROR: "team"}, ch=ch)
        await ch.insert(mirrors.TABLE, [{"org": ORG, "namespace": "team", "mirror": MIRROR}])
        found = await mirrors.mirrors_of(ORG, ["team", "other"], ch=ch)
        # In name order, so the mirror pass binds the same list on every run.
        assert list(found.items()) == [(MIRROR, "team"), (OLD_MIRROR, "team")]
        assert await mirrors.mirrors_of("other-org", ["team"], ch=ch) == {}
        assert await namespace_census.in_use(ORG, "team", [], ch=ch)  # type: ignore[arg-type]
        await mirrors.forget_mirrors(ORG, found, ch=ch)
        # Forgotten, the mirrors still reach a later erasure, and no longer hold the name.
        assert await mirrors.mirrors_of(ORG, ["team"], ch=ch) == found
        assert not await namespace_census.in_use(ORG, "team", [], ch=ch)  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_no_namespaces_read_nothing() -> None:
    class _NoReads:
        async def query_all(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("no statement for no namespaces")

    assert asyncio.run(mirrors.mirrors_of(ORG, [], ch=_NoReads())) == {}  # type: ignore[arg-type]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """`team` has one mirror; the calls the steps make on the registry."""
    calls: list[Any] = []

    async def _mirrors_of(org: str, namespaces: list[str], ch: Any = None) -> dict[str, str]:
        calls.append(("mirrors_of", namespaces) if ch is None else ("mirrors_of", namespaces, ch))
        return {MIRROR: "team"} if "team" in namespaces else {}

    async def _forget(org: str, found: dict[str, str]) -> None:
        calls.append(("forget", found))

    monkeypatch.setattr(app_mirrors, "mirrors_of", _mirrors_of)
    monkeypatch.setattr(app_mirrors, "forget_mirrors", _forget)
    return calls


@pytest.mark.parametrize(
    ("purge", "passes"),
    [(False, [("team", False), ([MIRROR], True)]), (True, [(["team", MIRROR], True)])],
)
def test_a_step_runs_over_the_mirrors_as_a_purge(
    registry: list[Any], purge: bool, passes: list[Any]
) -> None:
    seen: list[Any] = []

    async def step(org: str, namespace: Any) -> int:
        seen.append((namespace, purge_mode()))
        return 1

    with purge_scope(purge):
        assert asyncio.run(saga_steps.with_mirrors("memory.tombstone", step)(ORG, "team")) == len(
            passes
        )
    assert seen == passes


def test_a_list_scope_covers_every_named_namespace_s_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _mirrors_of(org: str, namespaces: list[str], ch: Any = None) -> dict[str, str]:
        return {m: ns for m, ns in [(MIRROR, "a"), (OLD_MIRROR, "b")] if ns in namespaces}

    monkeypatch.setattr(app_mirrors, "mirrors_of", _mirrors_of)
    seen: list[Any] = []

    async def step(org: str, namespace: Any) -> int:
        seen.append((namespace, purge_mode()))
        return 0

    asyncio.run(saga_steps.with_mirrors("memory.tombstone", step)(ORG, ["a", "b", "c"]))
    assert seen == [(["a", "b", "c"], False), ([MIRROR, OLD_MIRROR], True)]


def test_a_postgres_step_runs_once_over_the_namespace_and_its_mirrors(
    registry: list[Any],
) -> None:
    seen: list[Any] = []

    async def step(org: str, namespace: Any) -> int:
        seen.append(namespace)
        return 1

    # One transaction, so the one mark it commits covers the mirror for a replay.
    asyncio.run(saga_steps.with_mirrors("pg_memory.delete", step)(ORG, "team"))
    assert seen == [["team", MIRROR]]


def test_a_mirror_is_never_looked_up_for_a_mirror(registry: list[Any]) -> None:
    async def step(org: str, namespace: Any) -> int:
        return 0

    asyncio.run(saga_steps.with_mirrors("memory.tombstone", step)(ORG, ["other", MIRROR]))
    assert registry == [("mirrors_of", ["other"])]


def test_the_last_step_forgets_the_mirrors(registry: list[Any]) -> None:
    assert saga_steps.NAMESPACE_STEPS[-1][0] == "knowledge_mirrors.forget"
    assert asyncio.run(saga_steps._step_forget_mirrors_by_ns(ORG, "team")) == 1
    assert registry[-1] == ("forget", {MIRROR: "team"})


def test_a_namespace_erasure_purges_the_mirror_and_counts_it(
    registry: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = saga_runner(monkeypatch)
    censused: list[Any] = []

    async def _census(org: str, scope_type: str, scope_value: Any, **_: Any) -> dict[str, int]:
        censused.append(scope_value)
        return {}

    monkeypatch.setattr(erasure_census, "residue_census", _census)
    trace = dict(asyncio.run(run("namespace", "team")))
    # The namespace's rows are tombstoned, the mirror's deleted, in the same step.
    assert trace["memory.tombstone"][0] == ("UPDATE", "loom.memory")
    assert ("DELETE", "loom.memory") in trace["memory.tombstone"]
    assert censused == ["team", [MIRROR]]
    assert ("forget", {MIRROR: "team"}) in registry


def test_a_replay_runs_each_step_over_the_mirrors_too(registry: list[Any]) -> None:
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app import replay

    seen: list[Any] = []

    async def step(org: str, namespace: Any) -> int:
        seen.append((namespace, purge_mode()))
        return 0

    body = ErasureRequestInput(org=ORG, scope_type="namespace", scope_value="team", requester="r")
    asyncio.run(replay._run_steps(body, "e1", [("memory.tombstone", step)]))
    assert seen == [("team", False), ([MIRROR], True)]


def test_a_registry_write_is_a_full_row(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = RecordingCH()
    ch.install(monkeypatch)
    asyncio.run(mirrors.forget_mirrors(ORG, {MIRROR: "team"}))
    [row] = ch.inserted("knowledge_mirrors")
    # Every column but `version`, which the server's clock sets.
    assert set(row) | {"version"} == set(column_names("knowledge_mirrors"))


def _counted(monkeypatch: pytest.MonkeyPatch, left_in_mirror: int) -> list[Any]:
    """The census answers `left_in_mirror` rows of loom.memory in the mirror, and only
    when it counts as a purge does (every row, tombstoned or not)."""
    counted: list[Any] = []

    async def _count(org: str, scope_type: str, scope: Any, **_: Any) -> dict[str, int]:
        counted.append((scope, purge_mode()))
        return {"loom.memory": left_in_mirror if scope == [MIRROR] and purge_mode() else 0}

    async def _pairs(org: str, scope_type: str, scope: Any, **_: Any) -> Any:
        return await _count(org, scope_type, scope), {}

    async def _objects(org: str, scope: Any) -> int:
        return 0

    monkeypatch.setattr(erasure_census, "residue_census", _count)
    monkeypatch.setattr(erasure_census, "evidence_census", _pairs)
    monkeypatch.setattr(erasure_census, "object_residue", _objects)
    return counted


def test_a_tombstoning_erasure_fails_on_any_row_left_in_a_mirror(
    registry: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = _counted(monkeypatch, left_in_mirror=1)
    holder: dict[str, Any] = {}
    with pytest.raises(saga_steps.ErasureResidueError):
        asyncio.run(saga_steps._step_verify_residue(ORG, "namespace", "team", holder))
    assert counted == [("team", False), ([MIRROR], True)]
    assert holder["per_table"]["loom.memory"] == 1


def test_the_evidence_census_counts_the_mirror_too(
    registry: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _counted(monkeypatch, left_in_mirror=2)
    conn = object()
    found, unread = asyncio.run(
        saga_steps.evidence_with_mirrors(ORG, "namespace", "team", spellings=None, ch=conn)  # type: ignore[arg-type]
    )
    assert found == {"loom.memory": 2} and unread == {}
    # The registry is read on the caller's connection, as the census is.
    assert ("mirrors_of", ["team"], conn) in registry


def test_a_failed_saga_s_census_counts_the_mirror(
    registry: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.erasure.app import saga

    _counted(monkeypatch, left_in_mirror=3)
    verify: dict[str, Any] = {}
    asyncio.run(saga._census_tail(ORG, "namespace", "team", verify, purge=False))
    assert verify["per_table"] == {"loom.memory": 3}


def test_erasure_re_verify_reports_what_a_mirror_still_holds(
    registry: list[Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import hygiene
    from server.erasure.store import health as erasure_health
    from server.erasure.store import ledger as erasure_ledger

    _counted(monkeypatch, left_in_mirror=4)
    row = {
        "id": "e1", "org": ORG, "scope_type": "namespace", "scope_value": "team",
        "purge": False, "attempts": 1, "steps_completed": [], "failure_reason": "x",
    }  # fmt: skip

    async def _failed(**_: Any) -> list[dict[str, Any]]:
        return [row]

    async def _gauges(**_: Any) -> dict[str, int]:
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", _failed)
    monkeypatch.setattr(erasure_health, "integrity_counts", _gauges)
    args = argparse.Namespace(
        completed_since=None, skip_external=False, org="", id=None, orphan_graph=False,
        resume=False,
    )  # fmt: skip
    asyncio.run(hygiene._re_verify_run(None, args))
    assert 'residue now: 4 {"memory": 4}' in capsys.readouterr().out
