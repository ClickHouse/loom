"""Cleanup captures real verified canonical identities before their ties disappear."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.auth import person_erasure as erasure
from server.core.errors import BackendUnavailableError
from server.core.secrets_at_rest import seal
from tests.unit.test_connected_source_authority import world as world

pytestmark = pytest.mark.asyncio


async def test_capture_retains_verified_canonical_subject_not_email_or_memory_user_id(
    world,
) -> None:
    world, _http = world
    encoded = await erasure.capture("scim-person-id", "person@example.com", {"person": {"team"}})
    captured = erasure.load("scim-person-id", encoded)
    assert captured.id == "scim-person-id"
    assert captured.people == (
        erasure.Person("team", "person", ("person@example.com",), ("person", "person@example.com")),
    )
    assert "person@example.com" not in encoded and "person@example.com" not in repr(captured)
    assert world.directory_reads > 0 and not world.native_requests and not world.writes


async def test_contact_spelling_without_actual_verified_tie_cannot_capture_another_person(
    world,
) -> None:
    world, _http = world
    with pytest.raises(BackendUnavailableError):
        await erasure.capture("scim-person-id", "unverified@example.com", {"person": {"team"}})
    assert not world.native_requests and not world.writes


async def test_capture_cannot_be_replayed_for_a_different_deleted_identity(world) -> None:
    encoded = await erasure.capture("scim-person-id", "person@example.com", {"person": {"team"}})
    with pytest.raises(BackendUnavailableError):
        erasure.load("another-scim-id", encoded)


@pytest.mark.parametrize(
    "change",
    [
        {"format": True},
        {"id": "another"},
        {
            "people": [
                {
                    "org": "team",
                    "subject": "person",
                    "emails": ["Unverified@example.com"],
                    "spellings": ["person"],
                }
            ]
        },
        {
            "people": [
                {
                    "org": "team",
                    "subject": "person",
                    "emails": [],
                    "spellings": ["person"],
                    "user_id": "person",
                }
            ]
        },
    ],
)
async def test_malformed_sealed_metadata_never_turns_into_cleanup_authority(
    world, change: Any
) -> None:
    value = {
        "format": 2,
        "id": "scim-person-id",
        "people": [
            {
                "org": "team",
                "subject": "person",
                "emails": ["person@example.com"],
                "spellings": ["person", "person@example.com"],
            },
        ],
        "workspaces": [],
        "prepared": False,
    } | change
    encoded = seal(erasure.PURPOSE, json.dumps(value))
    with pytest.raises(BackendUnavailableError):
        erasure.load("scim-person-id", encoded)


@pytest.mark.parametrize("pending", [False, True])
async def test_pending_historical_scim_erasure_is_a_required_uncached_crawl_gate(
    pending: bool,
) -> None:
    class Rows:
        async def query_one(self, sql: str):
            assert sql == (
                "SELECT 1 AS pending FROM loom.scim_users FINAL "
                "WHERE (deleted = 1 OR crawl_erasure_required = 1) AND NOT crawl_erasure_done LIMIT 1"
            )
            return {"pending": 1} if pending else None

    if pending:
        with pytest.raises(BackendUnavailableError):
            await erasure.require_settled(ch=Rows())
    else:
        await erasure.require_settled(ch=Rows())
