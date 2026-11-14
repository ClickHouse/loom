"""A mirrored document's permission list from Drive permissions, and its stored form
(server/knowledge/domain/access_list.py)."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.domain import access_list
from server.knowledge.domain.access_list import UNREAD, AccessList


def _perm(kind: str, **kw: Any) -> dict[str, Any]:
    return {"type": kind, "role": kw.pop("role", "reader"), **kw}


def test_every_role_a_reader_and_group_grants_and_discoverable_shares_count() -> None:
    got = access_list.from_drive_permissions(
        [
            _perm("user", role="owner", emailAddress="Owner@Example.com"),
            _perm("user", role="commenter", emailAddress="b@example.com"),
            # Offered ownership, and a writer meanwhile.
            _perm("user", role="writer", emailAddress="c@example.com", pendingOwner=True),
            _perm("group", role="writer", emailAddress="Team@Example.com"),
            _perm("domain", domain="Example.com", allowFileDiscovery=True),
            _perm("anyone", allowFileDiscovery=True),
        ]
    )
    assert got == AccessList(
        emails=("b@example.com", "c@example.com", "owner@example.com"),
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
    ],
)
def test_anything_but_a_read_list_is_unread(text: str) -> None:
    assert access_list.from_json(text) == UNREAD
