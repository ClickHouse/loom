"""A mirrored document's permission list from Drive permissions, and its stored form
(server/knowledge/domain/access_list.py)."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.domain import access_list
from server.knowledge.domain.access_list import UNREAD, AccessList


def _perm(kind: str, **kw: Any) -> dict[str, Any]:
    return {"type": kind, "role": kw.pop("role", "reader"), **kw}


def test_every_drive_role_and_discoverable_share_is_listed() -> None:
    got = access_list.from_drive_permissions(
        [
            _perm("user", role="owner", emailAddress="Owner@Example.com"),
            _perm("user", role="commenter", emailAddress="b@example.com"),
            # Offered ownership, and a writer meanwhile.
            _perm("user", role="writer", emailAddress="c@example.com", pendingOwner=True),
            _perm("group", role="writer", emailAddress="Team@Example.com"),
            _perm("user", role="organizer", emailAddress="d@example.com"),
            _perm("user", role="fileOrganizer", emailAddress="e@example.com"),
            _perm("domain", domain="Example.com", allowFileDiscovery=True),
            _perm("anyone", allowFileDiscovery=True),
        ]
    )
    assert got == AccessList(
        emails=(
            "b@example.com",
            "c@example.com",
            "d@example.com",
            "e@example.com",
            "owner@example.com",
        ),
        groups=("team@example.com",),
        domains=("example.com",),
        anyone=True,
    )


@pytest.mark.parametrize(
    "permission",
    [
        _perm("domain", domain="example.com", allowFileDiscovery=False),  # link-only
        _perm("anyone", allowFileDiscovery=False),
        _perm("anyone"),  # discoverability unknown
        _perm("user", emailAddress="gone@example.com", deleted=True),
        _perm("user", role="unknownRole", emailAddress="x@example.com"),
        _perm("audience", emailAddress="x@example.com"),
        _perm("user", emailAddress="  "),
        _perm("user", emailAddress=123),
        _perm("user", role="published_reader", emailAddress="x@example.com", view="published"),
        _perm("user", role="reader", emailAddress="x@example.com", view="published"),
        _perm("anyone", role="reader", allowFileDiscovery=True, view="published"),
    ],
)
def test_a_permission_that_cannot_be_trusted_admits_nobody(permission: dict[str, Any]) -> None:
    assert access_list.from_drive_permissions([permission]) == AccessList()


def test_no_permissions_from_the_store_read_unread() -> None:
    assert access_list.from_drive_permissions(None) == UNREAD
    assert access_list.from_drive_permissions([]) == AccessList()  # read, and empty


def test_unread_is_stored_empty_as_every_other_writer_stores_it() -> None:
    assert access_list.to_json(UNREAD) == ""


def test_the_stored_form_round_trips_and_compares_equal_for_one_list() -> None:
    a = AccessList(emails=("a@example.com",), owner="user:a", domains=("example.com",), anyone=True)
    text = access_list.to_json(a)
    assert access_list.from_json(text) == a
    reordered = access_list.from_drive_permissions(
        [_perm("user", emailAddress="A@example.com"), _perm("user", emailAddress="a@example.com")]
    )
    assert access_list.to_json(reordered) == access_list.to_json(
        AccessList(emails=("a@example.com",))
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"emails": ["a@example.com"]}',  # no `unread: false`
        '{"unread": true, "emails": ["a@example.com"]}',
        '{"unread": "false", "anyone": true}',
        '{"unread": false, "anyone": true, "emails": "a@example.com"}',
        '{"unread": false, "anyone": "yes"}',
        '{"unread": false, "owner": 7}',
        '{"unread": false, "emails": ["a@example.com", 5]}',
        '{"unread": false, "anyone": true, "except": ["a@example.com"]}',
    ],
)
def test_anything_but_a_read_list_is_unread(text: str) -> None:
    assert access_list.from_json(text) == UNREAD


def test_a_list_built_directly_is_stored_in_the_one_spelling() -> None:
    built = AccessList(emails=("B@example.com", "a@example.com", "a@example.com"))
    assert access_list.to_json(built) == access_list.to_json(
        AccessList(emails=("a@example.com", "b@example.com"))
    )
