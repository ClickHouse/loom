"""Exact selection consent is a pure contract, not upstream permission evidence."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from server.knowledge.domain.mcp_sources import (
    CopyConsent,
    ResourceIdentity,
    SourceContract,
    selection_sha256,
)


def contract(**changes):
    resources = (ResourceIdentity(type="resource", id="opaque:A"),)
    values = dict(
        version=1,
        slot="mcp:" + "a" * 32,
        generation="a" * 32 + "/1" + "b" * 31,
        profile_id="reader",
        profile_sha256="c" * 64,
        mode="snapshot",
        reader_policy="owner_only",
        resources=resources,
    )
    values.update(changes)
    return SourceContract(**values)


def consent(resources):
    return CopyConsent(
        version=1,
        publisher="owner",
        namespace="destination",
        resources_sha256=selection_sha256(resources),
    )


def test_copy_requires_explicit_selection_bound_consent():
    with pytest.raises(ValidationError):
        contract(reader_policy="collection_copy")
    original = contract()
    accepted = contract(reader_policy="collection_copy", copy_consent=consent(original.resources))
    assert accepted.copy_consent.resources_sha256 == selection_sha256(original.resources)
    with pytest.raises(ValidationError):
        contract(
            reader_policy="collection_copy",
            copy_consent=accepted.copy_consent,
            resources=original.resources + (ResourceIdentity(type="resource", id="opaque:B"),),
        )


@pytest.mark.parametrize("mode,policy", [("live", "collection_copy"), ("snapshot", "owner_only")])
def test_consent_cannot_authorize_live_or_owner_only(mode, policy):
    with pytest.raises(ValidationError):
        contract(mode=mode, reader_policy=policy, copy_consent=consent(contract().resources))


def test_digest_preserves_order_and_exact_unicode_ids():
    resources = tuple(
        ResourceIdentity(type="resource", id=r) for r in ("é", "e\u0301", "https://x/A")
    )
    expected = hashlib.sha256(
        json.dumps([r.id for r in resources], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    assert selection_sha256(resources) == expected
    assert selection_sha256(resources) != selection_sha256(tuple(reversed(resources)))


def test_pause_and_label_are_bounded_metadata():
    assert contract(paused=True, label="Reading").paused is True
    with pytest.raises(ValidationError):
        contract(label="x" * 257)
    with pytest.raises(ValidationError):
        contract(label="bad\nlabel")
    with pytest.raises(ValidationError):
        CopyConsent(
            version=2, publisher="owner", namespace="destination", resources_sha256="c" * 64
        )
