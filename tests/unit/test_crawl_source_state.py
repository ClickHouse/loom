"""Real source row construction and complete multi-person union contracts."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from server.knowledge.domain.source_crawl import Expansion, Options, Person, Union
from server.knowledge.domain.sources import KnowledgeSource, mirror_synced
from server.knowledge.store import sources


def _options() -> dict[str, Any]:
    return {
        "directory_subject": "directory@example.com",
        "subjects": [{"type": "group", "email": "team@example.com"}],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"directory_subject": "person@example.com\n"},
        {"subjects": []},
        {"subjects": [{"type": "customer", "email": "all@example.com"}]},
        {"subjects": [{"type": "person", "email": "person@example.com", "id": "guessed"}]},
        {"subjects": [{"type": "person", "email": "person@example.com"}] * 2},
        {"query": "all users"},
    ],
)
def test_crawl_options_refuse_untyped_or_ambiguous_admission(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Options.parse(_options() | change)


@pytest.mark.asyncio
async def test_real_source_row_roundtrip_keeps_separate_crawl_authority_and_never_serves_it() -> (
    None
):
    written: list[dict[str, Any]] = []

    class Store:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == sources.TABLE
            written.extend(rows)

    source = KnowledgeSource(
        org="org",
        namespace="team",
        name="meeting-notes",
        kind="gdrive",
        source_id="a8895a80-d41c-4515-8c89-57fc1f7b4994",
        config={"include": ["docs"]},
        credential_mode="crawl",
        access="mirrored",
        items=[{"type": "file", "id": "file-identifier"}],
        crawl_options=_options(),
        registrant_subject="canonical-admin",
        approval_enc="sealed-approval",
        crawl_generation="registration-generation",
        crawl_customer_id="verified-workspace",
        crawl_credential_hash="a" * 64,
        secret_enc="sealed-delegated-key",
        created_by="audit-principal",
    )
    await sources.insert_source(source, ch=Store())
    (row,) = written
    assert (
        row["kind"] == row["credential_enc"] == row["owner_subject"] == row["connection_id"] == ""
    )
    assert json.loads(row["options_json"]) == _options()
    restored = sources._source(row)
    assert dataclasses.replace(restored, created_at=None, updated_at=None) == source
    assert mirror_synced(restored)  # Crawl writes remain confined to the checked mirror.
    assert restored.registrant_subject != restored.created_by


def _people() -> tuple[Person, Person]:
    return Person("customer", "one", "one@example.com", ()), Person(
        "customer", "two", "two@example.com", ()
    )


def _file(**changes: Any) -> dict[str, Any]:
    return {"id": "file", "version": "1", "mimeType": "text/plain", **changes}


def test_one_person_absence_cannot_retire_another_persons_file() -> None:
    one, two = _people()
    union = Union(Expansion("customer", (one, two), (), "exclusions"), 100)
    union.add(one, [_file(owners=[{"me": True, "emailAddress": one.email}])], whole=True)
    with pytest.raises(ValueError, match="incomplete"):
        union.complete()
    union.add(two, [], whole=True)
    (file,) = union.complete()
    assert (file.id, file.access.emails, file.presenters) == ("file", (one.email,), (one.id,))
    assert file.access.owner == ""


def test_complete_union_uses_explicit_permissions_not_link_only_listing_as_person() -> None:
    one, two = _people()
    union = Union(Expansion("customer", (one, two), (), "exclusions"), 100)
    union.add(
        one,
        [
            _file(
                permissions=[
                    {"type": "user", "role": "reader", "emailAddress": one.email},
                    {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
                ]
            )
        ],
        whole=True,
    )
    union.add(two, [_file(permissions=[])], whole=True)
    (file,) = union.complete()
    assert file.presenters == (one.id, two.id)
    assert file.access.emails == (one.email,)
    assert not file.access.anyone and not file.access.owner


@pytest.mark.parametrize("change", [{"version": "2"}, {"modifiedTime": "new"}])
def test_union_refuses_an_acl_observed_on_a_different_content_revision(
    change: dict[str, Any],
) -> None:
    one, two = _people()
    union = Union(Expansion("customer", (one, two), (), "exclusions"), 100)
    union.add(one, [_file(permissions=[])], whole=True)
    with pytest.raises(ValueError, match="changed"):
        union.add(two, [_file(permissions=[], **change)], whole=True)
    with pytest.raises(ValueError, match="incomplete"):
        union.complete()


def test_truncated_listing_never_completes_or_publishes_a_partial_union() -> None:
    one, two = _people()
    union = Union(Expansion("customer", (one, two), (), "exclusions"), 100)
    with pytest.raises(ValueError, match="incomplete"):
        union.add(one, [_file()], whole=False)
    union.add(two, [], whole=True)
    with pytest.raises(ValueError, match="incomplete"):
        union.complete()


def test_global_file_bound_applies_to_the_union_not_each_person_separately() -> None:
    one, two = _people()
    union = Union(Expansion("customer", (one, two), (), "exclusions"), 1)
    union.add(one, [_file(id="first")], whole=True)
    with pytest.raises(ValueError, match="file bound"):
        union.add(two, [_file(id="second")], whole=True)
    with pytest.raises(ValueError, match="incomplete"):
        union.complete()
