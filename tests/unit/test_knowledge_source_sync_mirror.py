"""A mirrored folder-store source's turn (server/knowledge/app/source_sync_mirror.py): its
files land in the namespace's mirror with the lists the store gives them, a changed list
is written without embedding again, an absent file reads `unread` before it is retired,
and the listing row lands last."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import httpx
import pytest

from server.clients import gdrive
from server.core.secrets_at_rest import seal
from server.knowledge.app import mirrors, source_sync
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import MIRRORED_ACCESS, mirror_uri
from server.knowledge.store import documents
from tests.unit.test_knowledge_source_sync import DOCS, KEY, _file, _source
from tests.unit.test_knowledge_source_sync import world as world  # the fixture

MIRROR = "acl-" + "a" * 24
READERS = [{"type": "user", "role": "reader", "emailAddress": "Ann@example.com"}]


def _mirrored(**kw: Any) -> Any:
    src = _source(access=MIRRORED_ACCESS, credential_enc="", **kw)
    return dataclasses.replace(src, secret_enc=seal("knowledge_sources.secret", KEY))


@pytest.fixture
def mirror(world: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The namespace's mirror, the listing rows recorded and the list-only rewrites."""
    seen: dict[str, Any] = {"listings": [], "rewrites": [], "permission_reads": []}

    async def _ensure(org: str, namespace: str) -> str:
        return MIRROR

    async def _listing(org: str, namespace: str, source_id: str, name: str, started: Any) -> None:
        seen["listings"].append((namespace, source_id, name))

    async def _insert_one(row: Any, *, over: int, **kw: Any) -> None:
        seen["rewrites"].append(row)
        world.docs[row["source_uri"]]["access_json"] = row["access_json"]

    async def _permissions(token: str, file_id: str, **kw: Any) -> Any:
        seen["permission_reads"].append(file_id)
        return None  # the store will not say

    monkeypatch.setattr(mirrors, "ensure_mirror", _ensure)
    monkeypatch.setattr(mirrors, "record_listing", _listing)
    monkeypatch.setattr(documents, "insert_one", _insert_one)
    monkeypatch.setattr(gdrive, "file_permissions", _permissions)
    return seen


def _turn(world: Any, src: Any) -> Any:
    world.sources = [src]
    return asyncio.run(source_sync.run_sync())


def test_a_mirrored_file_lands_in_the_mirror_with_its_list(world: Any, mirror: Any) -> None:
    src = _mirrored()
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    result = _turn(world, src)
    assert result.synced == 1 and result.changed == 1
    [(written, _)] = world.upserts
    address = source_sync.source_uri_of(world.files[0])
    assert written.namespace == MIRROR
    assert written.source_uri == mirror_uri(address, src.source_id)
    assert f"source-id:{src.source_id}" in written.tags
    stored = access_list.from_json(world.docs[written.source_uri]["access_json"])
    assert stored.emails == ("ann@example.com",)
    assert mirror["listings"] == [(MIRROR, src.source_id, src.name)]


def test_a_file_listed_without_permissions_is_read_and_unread_when_the_store_will_not_say(
    world: Any, mirror: Any
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    _turn(world, _mirrored())
    assert mirror["permission_reads"] == ["doc1"]
    [(written, _)] = world.upserts
    assert world.docs[written.source_uri]["access_json"] == ""  # unread


def test_a_narrowed_list_rewrites_the_row_and_the_same_list_writes_nothing(
    world: Any, mirror: Any
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    assert len(world.upserts) == 1
    _turn(world, _mirrored())  # same revision, same list
    assert len(world.upserts) == 1 and mirror["rewrites"] == []
    world.files[0]["permissions"] = []  # everyone removed, same revision
    _turn(world, _mirrored())
    assert len(world.upserts) == 1  # no content fetched, nothing embedded
    [row] = mirror["rewrites"]
    assert access_list.from_json(row["access_json"]) == access_list.AccessList()


def test_an_absent_file_reads_unread_before_it_is_retired(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    [(written, _)] = world.upserts
    world.files = []
    world.file_answers["doc1"] = httpx.Response(404)
    result = _turn(world, _mirrored())
    assert [r["access_json"] for r in mirror["rewrites"]] == [""]
    assert result.removed == 1 and written.source_uri not in world.docs


def test_an_incomplete_listing_records_no_listing_row(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    world.incomplete_search = True
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and mirror["listings"] == []


def test_a_mirrored_source_is_never_mined(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    assert world.mined == []


def test_a_mirror_never_takes_a_name_already_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import directory

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

    monkeypatch.setattr(mirrors, "new_mirror_name", lambda: next(names))
    monkeypatch.setattr(mirrors, "mirrors_of", _none)
    monkeypatch.setattr(mirrors, "add_mirror", _add)
    monkeypatch.setattr(directory, "directory", lambda: _Directory())
    monkeypatch.setattr(mirrors, "directory", lambda: _Directory())
    assert asyncio.run(mirrors.ensure_mirror("o", "team")) == "acl-" + "2" * 24
    assert added == ["acl-" + "2" * 24]


def test_permissions_are_read_page_by_page_and_a_refusal_reads_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "": {"permissions": [READERS[0]], "nextPageToken": "p2"},
        "p2": {"permissions": [{"type": "anyone", "role": "reader"}]},
    }

    def answer(request: httpx.Request) -> httpx.Response:
        if "/files/locked/" in request.url.path:
            return httpx.Response(403, json={"error": {"code": 403}})
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    got = asyncio.run(gdrive.file_permissions("tok", "doc1", pace_s=0))
    assert got == [READERS[0], {"type": "anyone", "role": "reader"}]
    assert asyncio.run(gdrive.file_permissions("tok", "locked", pace_s=0)) is None
