"""A permission list admits one verified reader, independently of its candidates."""

from __future__ import annotations

import pytest

from server.knowledge.domain.access_list import UNREAD, AccessList, allows, from_json


@pytest.mark.parametrize(
    "access,subject,emails,domains,groups,owner,expected",
    [
        (AccessList(anyone=True), "", (), (), (), "", True),
        (
            UNREAD,
            "lee",
            ("lee@example.com",),
            ("example.com",),
            ("team@example.com",),
            "lee",
            False,
        ),
        (
            AccessList(),
            "lee",
            ("lee@example.com",),
            ("example.com",),
            ("team@example.com",),
            "",
            False,
        ),
        (AccessList(owner="alias@example.com"), "lee", (), (), (), "lee", True),
        (AccessList(owner="alias@example.com"), "other", (), (), (), "lee", False),
        (AccessList(owner="lee"), "", (), (), (), "lee", False),
        (AccessList(emails=("lee@example.com",)), "lee", ("lee@example.com",), (), (), "", True),
        (AccessList(emails=("lee@example.com",)), "lee@example.com", (), (), (), "", False),
        (AccessList(emails=("lee@example.com",)), "", ("lee@example.com",), (), (), "", False),
        (AccessList(domains=("example.com",)), "lee", ("lee@example.com",), (), (), "", False),
        (AccessList(domains=("example.com",)), "lee", (), ("example.com",), (), "", True),
        (AccessList(domains=("example.com",)), "lee", (), ("sub.example.com",), (), "", False),
        (
            AccessList(groups=("team@example.com",)),
            "lee",
            ("lee@example.com",),
            (),
            ("team@example.com",),
            "",
            True,
        ),
        (
            AccessList(groups=("team@example.com",)),
            "lee",
            ("lee@example.com",),
            (),
            ("other@example.com",),
            "",
            False,
        ),
        (AccessList(groups=("team@example.com",)), "", (), (), ("team@example.com",), "", False),
    ],
)
def test_one_verified_person_or_discoverable_anyone(
    access, subject, emails, domains, groups, owner, expected
):
    assert (
        allows(
            access,
            subject=subject,
            emails=frozenset(emails),
            domains=frozenset(domains),
            groups=frozenset(groups),
            canonical_owner=owner,
        )
        is expected
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "null",
        "[]",
        '{"anyone":true}',
        '{"unread":false,"anyone":"true"}',
        '{"unread":false,"anyone":1}',
        '{"unread":false,"anyone":true,"future_restriction":true}',
        '{"unread":false,"emails":[true],"anyone":true}',
        '{"unread":false,"groups":"team@example.com","anyone":true}',
        '{"unread":false,"owner":[],"anyone":true}',
    ],
)
def test_malformed_permission_never_reaches_anyone_or_identity_arms(text):
    assert not allows(
        from_json(text),
        subject="lee",
        emails=frozenset({"lee@example.com"}),
        domains=frozenset({"example.com"}),
        groups=frozenset({"team@example.com"}),
        canonical_owner="lee",
    )


@pytest.mark.parametrize(
    "tags,uri,expected",
    [
        (
            ["source-id:11111111-1111-4111-8111-111111111111"],
            "gdrive:file#11111111-1111-4111-8111-111111111111",
            "11111111-1111-4111-8111-111111111111",
        ),
        (["source:name"], "gdrive:file#11111111-1111-4111-8111-111111111111", ""),
        (
            ["source-id:11111111-1111-4111-8111-111111111111"],
            "gdrive:file#22222222-2222-4222-8222-222222222222",
            "",
        ),
        (
            [
                "source-id:11111111-1111-4111-8111-111111111111",
                "source-id:22222222-2222-4222-8222-222222222222",
            ],
            "gdrive:file#11111111-1111-4111-8111-111111111111",
            "",
        ),
        (["source-id:bad"], "gdrive:file#bad", ""),
        (
            ["source-id:11111111-1111-4111-8111-111111111111"],
            "#11111111-1111-4111-8111-111111111111",
            "",
        ),
    ],
)
def test_source_identity_requires_one_id_and_its_address_suffix(tags, uri, expected):
    from server.knowledge.domain.document_access import source_id

    assert source_id(uri, tags) == expected


@pytest.mark.parametrize(
    "age,expected", [(0, True), (3599, True), (3600, True), (3601, False), (-1, False)]
)
def test_listing_and_group_age_must_be_known_and_within_bound(age, expected):
    from datetime import UTC, datetime, timedelta

    from server.knowledge.domain.document_access import fresh

    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert fresh(now - timedelta(seconds=age), now, 3600) is expected
    assert not fresh(None, now, 3600)


@pytest.mark.asyncio
async def test_child_leg_mark_is_monotonic_and_visible_after_private_scope():
    import asyncio

    from server.core import request_work
    from server.core.diagnostics import private_work

    bag = request_work.begin()

    async def child():
        with private_work():
            request_work.mark_mirrored(unread=True)
            request_work.mark_mirrored()

    await asyncio.gather(child(), child())
    assert request_work.mirror_marked() and bag.mirror_unread


@pytest.mark.asyncio
async def test_next_request_has_no_preceding_mirror_mark():
    from server.core import request_work

    first = request_work.begin()
    request_work.mark_mirrored(unread=True)
    second = request_work.begin()
    assert first.mirror_marked and first.mirror_unread
    assert not request_work.mirror_marked() and not second.mirror_unread


def test_candidate_keys_are_coarse_and_keep_unknown_readers_to_anyone():
    from server.knowledge.domain.access_list import candidate_keys

    assert candidate_keys(subject="", emails=frozenset(), domains=frozenset()) == ["anyone"]
    keys = candidate_keys(
        subject="reader",
        emails=frozenset({"lee@example.com"}),
        domains=frozenset({"example.com"}),
    )
    assert set(keys) == {
        "anyone",
        "owner",
        "group",
        "email_unicode",
        "domain_unicode",
        "e:lee@example.com",
        "d:example.com",
    }
