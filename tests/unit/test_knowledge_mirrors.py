"""The mirror registry (`loom.knowledge_mirrors`) and its erasure: the store's statements
on clickhouse-local, the namespace erasure step, and the census that keeps a name with a
live mirror from being taken (docs/architecture/document-sources.md)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.naming import is_mirror_name
from server.erasure.app import handlers, saga_steps
from server.identity.store import namespace_census
from server.knowledge.app import mirrors as app_mirrors
from server.knowledge.domain.sources import KnowledgeSource, id_list_config
from server.knowledge.store import mirrors
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, LocalCH, ddl

ORG = "o"

needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse not installed")


@needs_clickhouse
def test_a_namespace_keeps_one_mirror_until_it_is_forgotten(tmp_path: Path) -> None:
    ch = KeptCH(tmp_path, ddl("knowledge_mirrors"))

    async def scenario() -> None:
        first = await mirrors.ensure_mirror(ORG, "team", ch=ch)
        assert is_mirror_name(first)
        assert await mirrors.ensure_mirror(ORG, "team", ch=ch) == first
        assert await mirrors.mirrors_of(ORG, ["team", "other"], ch=ch) == {"team": first}
        assert await mirrors.mirrors_of("other-org", ["team"], ch=ch) == {}
        await mirrors.forget_mirrors(ORG, {"team": first}, ch=ch)
        assert await mirrors.mirror_of(ORG, "team", ch=ch) == ""
        # The name registered again gets a new mirror, never the erased one.
        again = await mirrors.ensure_mirror(ORG, "team", ch=ch)
        assert is_mirror_name(again) and again != first

    asyncio.run(scenario())


def test_no_namespaces_read_nothing() -> None:
    class _NoReads:
        async def query_all(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("no statement for no namespaces")

    assert asyncio.run(mirrors.mirrors_of(ORG, [], ch=_NoReads())) == {}  # type: ignore[arg-type]


def _erasures(monkeypatch: pytest.MonkeyPatch, status: str) -> list[Any]:
    """Stub the registry and the purging saga; the calls in order."""
    calls: list[Any] = []

    async def _mirrors_of(org: str, namespaces: list[str]) -> dict[str, str]:
        calls.append(("mirrors_of", namespaces))
        return {"team": "acl-" + "a" * 24}

    async def _erase(org: str, namespace: str, *, requester: str) -> Any:
        calls.append(("erase", namespace, requester))
        return SimpleNamespace(status=status, erasure_id=str(uuid.uuid4()))

    async def _forget(org: str, found: dict[str, str]) -> None:
        calls.append(("forget", found))

    monkeypatch.setattr(app_mirrors, "mirrors_of", _mirrors_of)
    monkeypatch.setattr(app_mirrors, "forget_mirrors", _forget)
    monkeypatch.setattr(handlers, "erase_namespace", _erase)
    return calls


def test_a_namespace_erasure_purges_its_mirror_then_forgets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _erasures(monkeypatch, "completed")
    assert asyncio.run(saga_steps._step_erase_mirrors_by_ns(ORG, "team")) == 1
    mirror = "acl-" + "a" * 24
    assert calls == [
        ("mirrors_of", ["team"]),
        ("erase", mirror, saga_steps.MIRROR_ERASURE_REQUESTER),
        ("forget", {"team": mirror}),
    ]
    assert saga_steps.NAMESPACE_STEPS[-1][0] == "knowledge_mirrors.erase"


def test_a_mirror_erasure_that_does_not_complete_keeps_the_registry_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _erasures(monkeypatch, "failed")
    with pytest.raises(RuntimeError, match="ended failed"):
        asyncio.run(saga_steps._step_erase_mirrors_by_ns(ORG, "team"))
    assert [c[0] for c in calls] == ["mirrors_of", "erase"]


@needs_clickhouse
@pytest.mark.parametrize(("deleted", "expected"), [(False, True), (True, False)])
def test_a_live_mirror_keeps_its_name_in_use(deleted: bool, expected: bool) -> None:
    setup = [ddl(t.removeprefix("loom.")) for t in namespace_census.STATE_TABLES]
    setup.append(
        "INSERT INTO loom.knowledge_mirrors (org, namespace, mirror, deleted) "
        f"VALUES ('{ORG}', 'team', 'acl-{'b' * 24}', {str(deleted).lower()})"
    )
    found = asyncio.run(namespace_census.in_use(ORG, "team", [], ch=LocalCH(*setup)))  # type: ignore[arg-type]
    assert found is expected


@pytest.mark.parametrize(
    ("access", "credential_mode"), [("mirrored", "shared"), ("namespace", "connected")]
)
def test_a_build_without_items_never_reads_a_source_beyond_its_model(
    access: str, credential_mode: str
) -> None:
    src = KnowledgeSource(
        org=ORG, namespace="team", name="d", kind="gdrive", source_id=str(uuid.uuid4()),
        config={"folder_ids": ["f"]}, access=access, credential_mode=credential_mode,
    )  # fmt: skip
    # Such a source is stored with `kind` empty, so build N-1 finds no probe for it.
    assert id_list_config(src) is None


def test_a_mirror_s_own_erasure_reads_no_registry_row(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _erasures(monkeypatch, "completed")
    asyncio.run(saga_steps._step_erase_mirrors_by_ns(ORG, "acl-" + "c" * 24))
    # mirrors_of([]) reads nothing, as test_no_namespaces_read_nothing pins.
    assert calls[0] == ("mirrors_of", [])
