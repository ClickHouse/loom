"""Every decision input is bounded before memo keys or storage reads."""

from __future__ import annotations

import pytest

from server.auth import authz, scim
from server.cli import cell_scim
from server.settings import settings
from tests.unit.test_scim_people import people as people


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(
    "org",
    [None, [], {}, 1, "x" * 65537, "é" * 32769, "\x00", "\ud800"],
    ids=["none", "list", "dict", "number", "characters", "utf8", "nul", "surrogate"],
)
async def test_invalid_org_refuses_before_key_construction_or_store_reads(
    people, monkeypatch, org, required
):
    monkeypatch.setattr(settings, "cell", "first" if required else "")

    async def no_memo(*_, **__):
        pytest.fail("invalid decision inputs must refuse before constructing a memo key")

    monkeypatch.setattr(authz, "_remembered", no_memo)
    assert await scim.refuses_subject("owner", org=org) is True
    assert not people.reads


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(
    "subject,org",
    [("x" * 26000, "o" * 40000), ("owner", '"' * 32765)],
    ids=["aggregate-utf8", "aggregate-serialized"],
)
async def test_org_shares_the_same_utf8_and_serialized_input_budget(
    people, monkeypatch, subject, org, required
):
    monkeypatch.setattr(settings, "cell", "first" if required else "")

    async def no_memo(*_, **__):
        pytest.fail("the aggregate input budget must be checked before the memo")

    monkeypatch.setattr(authz, "_remembered", no_memo)
    assert await scim.refuses_subject(subject, org=org) is True
    assert not people.reads


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("org", ["", "*", "acme"])
async def test_valid_org_values_keep_the_existing_required_and_standalone_contract(
    people, monkeypatch, org, required
):
    monkeypatch.setattr(settings, "cell", "first" if required else "")
    people.provision("alice@example.test")
    assert await scim.refuses_subject("alice@example.test", org=org) is False


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("subject", ["", " \t"])
@pytest.mark.parametrize(
    "verified_email,also,deactivated",
    [
        ("", (), False),
        ("alice@example.test", (), False),
        ("disabled@example.test", (), True),
        ("", ("disabled@example.test",), True),
    ],
)
async def test_blank_primary_keeps_standalone_deactivations_but_never_qualifies_a_person(
    people, monkeypatch, subject, required, verified_email, also, deactivated
):
    monkeypatch.setattr(settings, "cell", "first" if required else "")
    people.provision("alice@example.test")
    people.provision("disabled@example.test", active=False)

    refused = await scim.refuses_subject(
        subject, org="acme", verified_email=verified_email, also=also
    )

    assert refused is (required or deactivated)
    if required:
        assert not people.reads


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize(
    "subject",
    [None, [], {}, 1, "x" * 65537, "é" * 32769, "\x00", "\ud800"],
    ids=["none", "list", "dict", "number", "characters", "utf8", "nul", "surrogate"],
)
async def test_invalid_primary_refuses_before_memo_or_reads(people, monkeypatch, subject, required):
    monkeypatch.setattr(settings, "cell", "first" if required else "")

    async def no_memo(*_, **__):
        pytest.fail("invalid decision inputs must refuse before constructing a memo key")

    monkeypatch.setattr(authz, "_remembered", no_memo)
    assert await scim.refuses_subject(subject, org="acme") is True
    assert not people.reads


@pytest.mark.parametrize("subject", ["", " \t"])
async def test_census_never_qualifies_a_blank_primary_from_its_verified_email(people, subject):
    people.provision("alice@example.test")

    assert (
        await cell_scim._refuses_person(
            "acme", subject, "alice@example.test", cell_scim.time.monotonic() + 30.0
        )
        is True
    )
    assert not people.reads
