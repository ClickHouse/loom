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
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl
from tests.unit.erasure_saga_stub import saga_runner

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
        assert found == {OLD_MIRROR: "team", MIRROR: "team"}
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

    async def _mirrors_of(org: str, namespaces: list[str]) -> dict[str, str]:
        calls.append(("mirrors_of", namespaces))
        return {MIRROR: "team"} if "team" in namespaces else {}

    async def _forget(org: str, found: dict[str, str]) -> None:
        calls.append(("forget", found))

    monkeypatch.setattr(app_mirrors, "mirrors_of", _mirrors_of)
    monkeypatch.setattr(app_mirrors, "forget_mirrors", _forget)
    return calls


@pytest.mark.parametrize("purge", [False, True])
def test_a_step_runs_over_the_mirrors_as_a_purge(registry: list[Any], purge: bool) -> None:
    seen: list[Any] = []

    async def step(org: str, namespace: Any) -> int:
        seen.append((namespace, purge_mode()))
        return 1

    with purge_scope(purge):
        assert asyncio.run(saga_steps.with_mirrors(step)(ORG, "team")) == 2
    assert seen == [("team", purge), ([MIRROR], True)]


def test_a_mirror_is_never_looked_up_for_a_mirror(registry: list[Any]) -> None:
    async def step(org: str, namespace: Any) -> int:
        return 0

    asyncio.run(saga_steps.with_mirrors(step)(ORG, ["other", MIRROR]))
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
    assert censused == [["team", MIRROR]]
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
