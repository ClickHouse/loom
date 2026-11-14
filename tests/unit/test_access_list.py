"""A mirrored document's permission list from Drive permissions, and its stored form
(server/knowledge/domain/access_list.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.knowledge.domain import access_list


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
    assert got == access_list.AccessList(
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
        _perm("domain", domain="example.com"),  # discoverability unknown
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
    assert access_list.from_drive_permissions([permission]) == access_list.AccessList()


def test_no_permissions_from_the_store_read_unread() -> None:
    assert access_list.from_drive_permissions(None) == access_list.UNREAD
    assert access_list.from_drive_permissions([]) == access_list.AccessList()  # read, and empty


def test_unread_is_stored_empty_as_every_other_writer_stores_it() -> None:
    assert access_list.to_json(access_list.UNREAD) == ""


def test_a_read_list_that_admits_nobody_is_stored_apart_from_unread() -> None:
    nobody = access_list.AccessList()
    assert access_list.to_json(nobody) != ""
    assert access_list.from_json(access_list.to_json(nobody)) == nobody
    assert nobody != access_list.UNREAD


def test_the_stored_form_round_trips_and_compares_equal_for_one_list() -> None:
    a = access_list.AccessList(
        emails=("a@example.com",),
        owner="user:a",
        groups=("team@example.com",),
        domains=("example.com",),
        anyone=True,
    )
    text = access_list.to_json(a)
    assert access_list.from_json(text) == a
    respelled = access_list.from_drive_permissions(
        [
            _perm("user", emailAddress="b@example.com"),
            _perm("user", emailAddress="A@example.com"),
            _perm("user", emailAddress="a@example.com"),
        ]
    )
    assert access_list.to_json(respelled) == access_list.to_json(
        access_list.AccessList(emails=("a@example.com", "b@example.com"))
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
    assert access_list.from_json(text) == access_list.UNREAD


def test_a_list_built_directly_is_kept_in_the_one_spelling() -> None:
    built = access_list.AccessList(emails=("B@example.com", "a@example.com", "a@example.com"))
    assert built == access_list.AccessList(emails=("a@example.com", "b@example.com"))


def test_an_unread_list_that_names_someone_is_refused() -> None:
    with pytest.raises(ValueError, match="unread"):
        access_list.AccessList(unread=True, emails=("a@example.com",))


def test_no_operation_takes_or_answers_a_permission_list() -> None:
    from server.operations.registry import ALL_OPERATIONS

    for op in ALL_OPERATIONS:
        for model in (op.input_model, op.output_model):
            schema = json.dumps(model.model_json_schema())
            assert "access_json" not in schema, op.name
            assert "AccessList" not in schema, op.name
