from dataclasses import replace
from uuid import UUID

import pytest

from server.knowledge.domain.mcp_worker_permissions import (
    ReaderConnection,
    StoredCopyRevision,
    VerifiedWorkerReadEvidence,
    can_read_stored_copy,
)

NOW = 1_800_000_000


@pytest.fixture
def revision():
    return StoredCopyRevision(
        org="org",
        namespace="team",
        source_id=UUID(int=1),
        selection_generation=7,
        resource_id_sha256="1" * 64,
        revision_id=UUID(int=2),
        content_sha256="2" * 64,
    )


@pytest.fixture
def connection():
    return ReaderConnection(
        owner="alice",
        worker_id=UUID(int=3),
        connection_id=UUID(int=4),
        connection_generation=11,
        profile_digest="3" * 64,
    )


@pytest.fixture
def evidence(revision, connection):
    return VerifiedWorkerReadEvidence(
        confirmed_revision=revision,
        connection=connection,
        issued_at=NOW - 5,
        confirmed_at=NOW - 3,
        expires_at=NOW + 5,
    )


@pytest.fixture
def check(revision, connection, evidence):
    def evaluate(**overrides):
        inputs = dict(
            collection_granted=True,
            requester="alice",
            current_revision=revision,
            active_connection=connection,
            evidence=evidence,
            now=NOW,
            max_evidence_age_seconds=10,
        )
        return can_read_stored_copy(**(inputs | overrides))

    return evaluate


def test_current_collection_grant_and_matching_evidence_allow_read(check):
    assert check()


def test_importers_evidence_does_not_authorize_another_collection_reader(
    check, revision, connection, evidence
):
    bob_connection = replace(connection, owner="bob", connection_id=UUID(int=5))
    assert not check(requester="bob", active_connection=bob_connection)
    assert not check(requester="bob", active_connection=connection)
    assert check(
        requester="bob",
        active_connection=bob_connection,
        evidence=replace(evidence, confirmed_revision=revision, connection=bob_connection),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"collection_granted": False},
        {"collection_granted": None},
        {"collection_granted": 1},
        {"requester": None},
        {"current_revision": None},
        {"active_connection": None},
        {"evidence": None},
    ],
)
def test_unknown_or_revoked_authority_denies(check, overrides):
    assert not check(**overrides)


def test_wire_shaped_evidence_does_not_authorize(check, evidence):
    assert not check(evidence=vars(evidence))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("org", "other-org"),
        ("namespace", "other-collection"),
        ("source_id", UUID(int=6)),
        ("selection_generation", 9),
        ("resource_id_sha256", "4" * 64),
        ("revision_id", UUID(int=7)),
        ("content_sha256", "5" * 64),
    ],
)
def test_scope_or_revision_mismatch_denies_even_after_scope_or_content_returns(
    check, revision, field, value
):
    assert not check(current_revision=replace(revision, **{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "bob"),
        ("worker_id", UUID(int=6)),
        ("connection_id", UUID(int=7)),
        ("connection_generation", 13),
        ("profile_digest", "4" * 64),
    ],
)
def test_connection_mismatch_denies_even_after_the_same_profile_returns(
    check, connection, field, value
):
    assert not check(active_connection=replace(connection, **{field: value}))


@pytest.mark.parametrize("field", ["resource_id_sha256", "content_sha256"])
def test_even_matching_unknown_digests_deny(check, revision, evidence, field):
    unknown = replace(revision, **{field: ""})
    assert not check(
        current_revision=unknown, evidence=replace(evidence, confirmed_revision=unknown)
    )


def test_even_matching_unknown_profile_denies(check, connection, evidence):
    unknown = replace(connection, profile_digest="")
    assert not check(active_connection=unknown, evidence=replace(evidence, connection=unknown))


def test_expiry_is_exclusive_and_confirmation_is_inclusive(check, evidence):
    assert not check(now=evidence.confirmed_at - 1)
    assert check(now=evidence.confirmed_at)
    assert check(now=evidence.expires_at - 1)
    assert not check(now=evidence.expires_at)


@pytest.mark.parametrize(
    "overrides",
    [
        {"issued_at": NOW + 1, "confirmed_at": NOW + 1},
        {"issued_at": NOW - 4, "confirmed_at": NOW - 5},
        {"issued_at": NOW, "confirmed_at": NOW, "expires_at": NOW},
        {"expires_at": NOW + 6},
        {"issued_at": True},
    ],
)
def test_invalid_or_overlong_evidence_interval_denies(check, evidence, overrides):
    assert not check(evidence=replace(evidence, **overrides))


@pytest.mark.parametrize("max_age", [0, -1, None, True])
def test_unknown_or_invalid_freshness_policy_denies(check, max_age):
    assert not check(max_evidence_age_seconds=max_age)
