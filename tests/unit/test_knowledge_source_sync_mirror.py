"""A mirrored folder-store source's turn (server/knowledge/app/source_sync_mirror.py): its
files land in the namespace's mirror with the lists the store gives them, a changed list
is written without embedding again, an absent file reads `unread` before it is retired,
and the listing row lands last."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from server.clients import gdrive
from server.code.app.index.pass_budget import PassBudget
from server.core.secrets_at_rest import seal
from server.knowledge.app import mirrors, source_sync, source_sync_mirror
from server.knowledge.app import source_gdrive_items as gdrive_items
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import MIRRORED_ACCESS, mirror_uri
from server.knowledge.store import documents
from server.settings import settings
from tests.unit.test_knowledge_source_sync import DOCS, FOLDER, KEY, PDF, _detail, _file, _source
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

    async def _live(org: str, namespace: str) -> str:
        return MIRROR

    async def _listing(org: str, namespace: str, source_id: str, name: str, started: Any) -> None:
        seen["listings"].append((namespace, source_id, name))

    async def _get_many(org: str, namespace: str, uris: list[str], **kw: Any) -> list[Any]:
        found = [await documents.get_one(org, namespace, uri) for uri in uris]
        return [d for d in found if d is not None]

    async def _insert_many(rows: list[Any], *, over: Any, **kw: Any) -> int:
        for row in rows:
            seen["rewrites"].append(row)
            world.docs[row["source_uri"]]["access_json"] = row["access_json"]
        return len(rows)

    async def _permissions(token: str, file_id: str, **kw: Any) -> Any:
        seen["permission_reads"].append(file_id)
        return None  # the store will not say

    monkeypatch.setattr(mirrors, "live_mirror", _live)
    monkeypatch.setattr(source_sync_mirror, "PERMISSIONS_PACE_S", 0)
    monkeypatch.setattr(mirrors, "record_listing", _listing)
    monkeypatch.setattr(documents, "get_many_by_uris", _get_many)
    monkeypatch.setattr(documents, "insert_many", _insert_many)
    monkeypatch.setattr(gdrive, "file_permissions", _permissions)
    return seen


async def _current() -> None:
    return None


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
    [(written, _)] = world.upserts
    assert row["content"] == written.content and row["tags"] == written.tags
    assert row["doc_id"] == "d" and row["version"] > 1 and row["superseded_at"] is None


def test_an_absent_file_reads_unread_before_it_is_retired(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    [(written, _)] = world.upserts
    world.files = []
    world.file_answers["doc1"] = httpx.Response(404)
    audits = len(world.audits)
    result = _turn(world, _mirrored())
    assert [r["access_json"] for r in mirror["rewrites"]] == [""]
    assert result.removed == 1 and written.source_uri not in world.docs
    assert len(world.audits) == audits  # the audit log outlives an erasure: no address


def test_an_incomplete_listing_records_no_listing_row(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    world.incomplete_search = True
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and mirror["listings"] == []


def test_a_mirrored_source_is_never_mined(world: Any, mirror: Any) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    result = _turn(world, _mirrored())
    # Left out before its stored copies are read, not failed while reading them.
    assert world.mined == [] and result.errors == [] and result.failed == 0


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


def test_a_new_revision_left_unwritten_still_takes_the_store_s_list(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 100)
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="10", permissions=READERS)]
    _turn(world, _mirrored())
    # The owner shares it with nobody and uploads a revision past the size bound.
    world.files = [_file("pdf1", PDF, "2026-01-02T00:00:00.000Z", size="101", permissions=[])]
    _turn(world, _mirrored())
    assert len(world.upserts) == 1
    [row] = mirror["rewrites"]
    assert access_list.from_json(row["access_json"]) == access_list.AccessList()


def test_a_given_up_file_is_tried_again_once_per_full_interval(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    world.file_answers["bad1"] = httpx.Response(503)
    for _ in range(8):
        _turn(world, _mirrored())
    fetches = [r for r in world.requests if r.url.path.endswith("/bad1/export")]
    # One fetch a pass until it is given up, then none until the full interval.
    assert len(fetches) == source_sync._WAIT_PASSES
    assert list(_detail(world)["file_waits"]) == ["bad1"]
    monkeypatch.setattr(settings, "knowledge_source_sync_full_interval_seconds", 0)
    _turn(world, _mirrored())
    fetches = [r for r in world.requests if r.url.path.endswith("/bad1/export")]
    assert len(fetches) == source_sync._WAIT_PASSES + 1  # one more try at the interval
    world.files = []
    _turn(world, _mirrored())
    assert "bad1" not in _detail(world)["file_waits"]


def test_lists_read_before_the_pass_runs_out_are_written_and_no_listing_row_lands(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    _turn(world, _mirrored())
    out = {"of_time": False}

    async def _permissions(token: str, file_id: str, **kw: Any) -> Any:
        out["of_time"] = True
        return []  # everyone removed

    monkeypatch.setattr(gdrive, "file_permissions", _permissions)
    monkeypatch.setattr(PassBudget, "exhausted", lambda self: out["of_time"])
    for f in world.files:
        del f["permissions"]  # a shared drive's files: read one at a time
    listings = len(mirror["listings"])
    result = _turn(world, _mirrored())
    [row] = mirror["rewrites"]  # the one list read before the deadline
    assert access_list.from_json(row["access_json"]) == access_list.AccessList()
    assert result.cut and len(world.upserts) == 2 and len(mirror["listings"]) == listings


def test_a_rate_limit_on_a_stored_copy_s_list_stops_the_turn_before_any_fetch(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())

    async def _limited(token: str, file_id: str, **kw: Any) -> Any:
        raise gdrive.RateLimited("rate_limited")

    monkeypatch.setattr(gdrive, "file_permissions", _limited)
    world.files = [
        _file("doc1", DOCS, "2026-01-03T00:00:00.000Z", permissions=READERS),  # new
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),  # stored, its list unread
    ]
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and _detail(world)["status"] == "rate_limited"


def test_a_new_mirror_name_has_the_form_no_token_may_name() -> None:
    from server.core.naming import is_mirror_name, new_mirror_name

    assert is_mirror_name(new_mirror_name())


def test_a_failed_permission_read_reads_unread_and_the_turn_goes_on(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing(token: str, file_id: str, **kw: Any) -> Any:
        if file_id == "doc1":
            raise gdrive.VendorError("permissions of doc1: http 500", status=500)
        return READERS

    monkeypatch.setattr(gdrive, "file_permissions", _failing)
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    _turn(world, _mirrored())
    lists = {
        u.source_uri.split("/")[-2]: world.docs[u.source_uri]["access_json"]
        for u, _ in world.upserts
    }
    assert lists["doc1"] == ""
    assert access_list.from_json(lists["doc2"]).emails == ("ann@example.com",)
    # `unread` admits nobody, so the listing still vouches.
    assert len(mirror["listings"]) == 1 and _detail(world)["status"] == "ok"
    # A note, not a mark: the dashboard shows a `listing:` note as a turn cut short.
    assert _detail(world)["error"] == "permissions: a file's list could not be read"


def test_a_copy_without_live_chunks_still_takes_the_store_s_list(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_file_bytes", 100)
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="10", permissions=READERS)]
    _turn(world, _mirrored())
    [uri] = world.docs
    world.docs[uri]["chunks"] = False  # a write cut before its chunks
    world.files = [_file("pdf1", PDF, "2026-01-02T00:00:00.000Z", size="101", permissions=[])]
    _turn(world, _mirrored())
    assert access_list.from_json(world.docs[uri]["access_json"]) == access_list.AccessList()
    assert len(mirror["listings"]) == 2


def test_list_rewrites_go_on_past_the_pass_s_document_bound(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    _turn(world, _mirrored())
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    for f in world.files:  # both changed: the bound fetches one and defers the other
        f["modifiedTime"] = f["modifiedTime"].replace("2026-01-0", "2026-02-0")
        f["permissions"] = []
    result = _turn(world, _mirrored())
    assert result.cut and len(world.upserts) == 3
    assert all(
        d["access_json"] == access_list.to_json(access_list.AccessList())
        for d in world.docs.values()
    )
    assert len(mirror["listings"]) == 2


def test_a_walk_cut_at_its_bounds_reads_unread_where_it_did_not_reach(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    [uri] = world.docs
    real = gdrive_items.list_items

    async def _cut(*a: Any, **kw: Any) -> Any:
        listing = await real(*a, **kw)
        return dataclasses.replace(listing, files=[], walk_cut="walk: depth bound reached")

    monkeypatch.setattr(gdrive_items, "list_items", _cut)
    world.file_answers["doc1"] = httpx.Response(
        200, json={"id": "doc1", "mimeType": DOCS, "parents": [FOLDER], "trashed": False}
    )
    _turn(world, _mirrored())
    assert world.docs[uri]["access_json"] == ""  # kept, since the store says it is there
    assert len(mirror["listings"]) == 2


def test_a_rate_limit_on_a_new_file_s_permissions_writes_no_document(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _limited(token: str, file_id: str, **kw: Any) -> Any:
        raise gdrive.RateLimited("rate_limited")

    monkeypatch.setattr(gdrive, "file_permissions", _limited)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    _turn(world, _mirrored())
    assert world.upserts == [] and _detail(world)["status"] == "rate_limited"
    # No stored copy's list was left unread, so the listing still vouches.
    assert len(mirror["listings"]) == 1


def test_a_file_whose_fetch_fails_leaves_the_listing_row_to_land(world: Any, mirror: Any) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("bad1", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    world.file_answers["bad1"] = httpx.Response(503)
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and _detail(world)["status"] == "failed"
    assert len(mirror["listings"]) == 1


def test_a_list_is_compared_with_the_one_the_committed_head_holds(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    [uri] = world.docs
    widened = READERS + [{"type": "user", "role": "reader", "emailAddress": "bob@example.com"}]

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, str]:
        # A widening committed and not yet published: ClickHouse still holds Ann alone.
        return {uri: access_list.to_json(access_list.from_drive_permissions(widened))}

    monkeypatch.setattr(documents, "committed_lists", _heads)
    _turn(world, _mirrored())  # the store names Ann alone again
    [row] = mirror["rewrites"]
    assert access_list.from_json(row["access_json"]).emails == ("ann@example.com",)


def test_a_moved_file_keeps_its_list_at_its_new_address_and_none_at_its_old(
    world: Any, mirror: Any
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    [(old, _)] = world.upserts
    world.files[0]["webViewLink"] = "https://docs.example.com/document/d/doc1b/edit"
    world.files[0]["modifiedTime"] = "2026-01-02T00:00:00.000Z"
    _turn(world, _mirrored())
    [moved] = world.docs
    assert old.source_uri not in world.docs and moved.endswith(
        "/doc1b/edit#" + _mirrored().source_id
    )
    assert access_list.from_json(world.docs[moved]["access_json"]).emails == ("ann@example.com",)


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


def test_a_publication_still_owed_in_the_mirror_records_no_listing_row(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _owed(org: str, namespace: str, uri_suffix: str) -> bool:
        return namespace == MIRROR  # a head ClickHouse has no row for yet

    monkeypatch.setattr(documents, "publications_owed", _owed)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and mirror["listings"] == []


def test_a_mirrored_source_is_not_synced_while_auth_is_off(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    result = _turn(world, _mirrored())
    assert (result.sources, world.upserts, mirror["listings"]) == (0, [], [])


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


def test_a_publication_owed_when_the_copies_were_read_records_no_listing_row(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([True, False])  # owed at the read, settled by the turn's end

    async def _owed(org: str, namespace: str, uri_suffix: str) -> bool:
        return next(answers)

    monkeypatch.setattr(documents, "publications_owed", _owed)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    assert len(world.upserts) == 1 and mirror["listings"] == []
    assert _detail(world)["error"] == "publication: a written list is not in ClickHouse yet"


def test_a_failed_permission_read_does_not_hold_back_retirement(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    _turn(world, _mirrored())

    async def _failing(token: str, file_id: str, **kw: Any) -> Any:
        raise gdrive.VendorError("permissions of doc1: http 500", status=500)

    monkeypatch.setattr(gdrive, "file_permissions", _failing)
    del world.files[0]["permissions"]
    gone = world.files.pop()  # doc2 deleted in the store
    world.file_answers[gone["id"]] = httpx.Response(404)
    result = _turn(world, _mirrored())
    assert result.removed == 1 and len(world.docs) == 1


def test_an_incomplete_search_leaves_an_unlisted_copy_s_list_as_it_was(
    world: Any, mirror: Any
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())
    world.files, world.incomplete_search = [], True
    _turn(world, _mirrored())
    assert mirror["rewrites"] == []


def test_lists_read_before_a_rate_limit_in_the_same_batch_are_written(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    _turn(world, _mirrored())

    async def _limited_after_one(token: str, file_id: str, **kw: Any) -> Any:
        if file_id == "doc2":
            raise gdrive.RateLimited("rate_limited")
        return []  # doc1 shared with nobody now

    monkeypatch.setattr(gdrive, "file_permissions", _limited_after_one)
    for f in world.files:
        del f["permissions"]
    _turn(world, _mirrored())
    [row] = mirror["rewrites"]
    assert row["source_uri"].split("/")[-2] == "doc1"
    assert access_list.from_json(row["access_json"]) == access_list.AccessList()
    assert _detail(world)["status"] == "rate_limited"


def test_a_cut_in_a_later_batch_keeps_the_earlier_batches_writes(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file(f"doc{n}", DOCS, f"2026-01-0{n}T00:00:00.000Z", permissions=READERS)
        for n in (1, 2, 3)
    ]
    _turn(world, _mirrored())
    reads: list[list[str]] = []

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, str]:
        reads.append(uris)
        return {}

    out = {"reads": 0}

    async def _permissions(token: str, file_id: str, **kw: Any) -> Any:
        out["reads"] += 1
        return []

    monkeypatch.setattr(source_sync_mirror, "_REWRITE_BATCH", 1)
    monkeypatch.setattr(documents, "committed_lists", _heads)
    monkeypatch.setattr(gdrive, "file_permissions", _permissions)
    monkeypatch.setattr(PassBudget, "exhausted", lambda self: out["reads"] >= 2)
    for f in world.files:
        del f["permissions"]
    result = _turn(world, _mirrored())
    assert result.cut and len(mirror["rewrites"]) == 2  # batches one and two
    assert reads == [[u] for u in sorted(world.docs)][:2]  # each batch its own heads, none past


def test_a_mirrored_source_without_a_mirror_turn_is_refused_a_turn() -> None:
    src = dataclasses.replace(_mirrored(), kind="notion")
    with pytest.raises(ValueError, match="does not sync"):
        source_sync._turn_of(src)


def test_a_rate_limit_on_a_new_file_after_the_lists_keeps_the_listing_row(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    _turn(world, _mirrored())

    async def _limited(token: str, file_id: str, **kw: Any) -> Any:
        raise gdrive.RateLimited("rate_limited")

    monkeypatch.setattr(gdrive, "file_permissions", _limited)
    world.files[0]["permissions"] = []  # the stored copy's list, narrowed
    world.files.append(_file("doc2", DOCS, "2026-01-02T00:00:00.000Z"))  # new, unlisted perms
    _turn(world, _mirrored())
    [row] = mirror["rewrites"]
    assert access_list.from_json(row["access_json"]) == access_list.AccessList()
    assert len(world.upserts) == 1 and len(mirror["listings"]) == 2


def test_no_mirror_is_made_for_a_source_that_cannot_list(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[str] = []

    async def _live(org: str, namespace: str) -> str:
        made.append(namespace)
        return MIRROR

    monkeypatch.setattr(mirrors, "live_mirror", _live)
    world.fail_listing = 403
    _turn(world, _mirrored())
    assert made == [] and world.upserts == []


def test_files_past_the_deadline_that_need_no_read_do_not_cut_the_turn(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z", permissions=READERS),
    ]
    _turn(world, _mirrored())
    copies_read = {"done": False}
    real = source_sync_mirror._refresh_lists

    async def _then_out_of_time(*a: Any, **kw: Any) -> bool:
        ok = await real(*a, **kw)
        copies_read["done"] = True
        return ok

    monkeypatch.setattr(source_sync_mirror, "_refresh_lists", _then_out_of_time)
    monkeypatch.setattr(PassBudget, "exhausted", lambda self: copies_read["done"])
    result = _turn(world, _mirrored())  # both unchanged: nothing to fetch or read
    assert not result.cut and len(mirror["listings"]) == 2
