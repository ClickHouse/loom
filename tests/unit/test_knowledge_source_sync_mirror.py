"""Generic mirror allocation and namespace boundaries."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from server.knowledge.app import mirrors, source_sync
from server.knowledge.domain.sources import MIRRORED_ACCESS, home
from tests.unit.test_knowledge_source_sync import _source

MIRROR = "acl-" + "a" * 24


def _mirrored():
    return replace(_source(), access=MIRRORED_ACCESS)


async def _current() -> None:
    return None


def test_a_mirror_never_takes_a_name_already_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    names = iter(["acl-" + "1" * 24, "acl-" + "2" * 24])
    taken = {"acl-" + "1" * 24}
    added: list[str] = []

    class _Directory:
        async def in_use(self, org: str, namespace: str, **kw: Any) -> bool:
            return namespace in taken

    async def _none(*a: Any, **kw: Any) -> dict[str, str]:
        return {}

    async def _add(org: str, namespace: str, mirror: str, **kw: Any) -> None:
        added.append(mirror)

    from server.core import leases

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield True

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(mirrors, "new_mirror_name", lambda: next(names))
    monkeypatch.setattr(mirrors, "mirrors_of", _none)
    monkeypatch.setattr(mirrors, "add_mirror", _add)
    monkeypatch.setattr(mirrors, "directory", lambda: _Directory())
    assert asyncio.run(mirrors.ensure_mirror("o", "team", still_current=_current)) == (
        "acl-" + "2" * 24
    )
    assert added == ["acl-" + "2" * 24]


def test_a_new_mirror_name_has_the_form_no_token_may_name() -> None:
    from server.core.naming import is_mirror_name, new_mirror_name

    assert is_mirror_name(new_mirror_name())


def test_a_namespace_s_live_mirror_is_kept_and_none_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[str] = []

    async def _live(org: str, namespace: str) -> str:
        return MIRROR

    async def _add(org: str, namespace: str, mirror: str, **kw: Any) -> None:
        added.append(mirror)

    monkeypatch.setattr(mirrors, "live_mirror", _live)
    monkeypatch.setattr(mirrors, "add_mirror", _add)
    assert asyncio.run(mirrors.ensure_mirror("o", "team", still_current=_current)) == MIRROR
    assert added == []


def test_a_source_removed_before_its_turn_makes_no_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import leases

    added: list[str] = []

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield True

    async def _none(org: str, namespace: str) -> str:
        return ""

    async def _add(org: str, namespace: str, mirror: str, **kw: Any) -> None:
        added.append(mirror)

    async def _gone() -> None:
        raise source_sync.SourceGone("o/team/decks")  # the namespace's erasure removed it

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(mirrors, "live_mirror", _none)
    monkeypatch.setattr(mirrors, "add_mirror", _add)
    with pytest.raises(source_sync.SourceGone):
        asyncio.run(mirrors.ensure_mirror("o", "team", still_current=_gone))
    assert added == []


def test_two_turns_at_once_make_one_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import leases

    registry: dict[str, str] = {}
    lock = asyncio.Lock()
    names = iter(["acl-" + "1" * 24, "acl-" + "2" * 24])

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        async with lock:
            yield True

    async def _mirrors_of(org: str, namespaces: list[str], **kw: Any) -> dict[str, str]:
        await asyncio.sleep(0)  # the other turn runs between the read and the write
        return {m: ns for m, ns in registry.items() if ns in namespaces}

    async def _add(org: str, namespace: str, mirror: str, **kw: Any) -> None:
        await asyncio.sleep(0)
        registry[mirror] = namespace

    class _Directory:
        async def in_use(self, org: str, namespace: str, **kw: Any) -> bool:
            return False

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(mirrors, "mirrors_of", _mirrors_of)
    monkeypatch.setattr(mirrors, "add_mirror", _add)
    monkeypatch.setattr(mirrors, "new_mirror_name", lambda: next(names))
    monkeypatch.setattr(mirrors, "directory", lambda: _Directory())

    async def _both() -> list[str]:
        made = (mirrors.ensure_mirror("o", "team", still_current=_current) for _ in range(2))
        return list(await asyncio.gather(*made))

    made = asyncio.run(_both())
    assert made[0] == made[1] and len(registry) == 1


def test_home_refuses_an_access_this_build_does_not_know() -> None:
    assert home(_source()) == ("code-acme-app", "source:decks")
    with pytest.raises(ValueError, match="access"):
        home(_source(access="shared"))
    with pytest.raises(ValueError, match="mirror"):
        home(_mirrored())
