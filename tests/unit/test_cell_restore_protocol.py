"""A bounded restore page identifies one permanent placement snapshot."""

from __future__ import annotations

import copy

import pytest

from server.core import cell


def descriptor():
    return {
        "cell": "first",
        "deployment_id": "dep-first",
        "is_first": True,
        "state": "serving",
        "mode": "enforced",
        "bucket": "",
        "bucket_prefix": "",
        "current_fp": cell.as_value({"deployment_id": "dep-first"}),
        "next_fp": None,
        "placed": 3,
        "sole_cell": True,
    }


def test_full_page_and_last_page_keep_the_same_snapshot():
    from server.router import restore

    first = restore.page(descriptor(), ["a", "b", "c"], count=2)
    assert first["orgs"] == ["a", "b"]
    assert first["next"] == "b" and first["done"] is False
    assert restore.validate_page(first, after=None, previous=None) == descriptor()
    last = restore.page(descriptor(), ["c"], count=2)
    assert last["next"] is None and last["done"] is True
    assert restore.validate_page(last, after="b", previous=descriptor()) == descriptor()


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_first", 1),
        ("placed", True),
        ("placed", -1),
        ("mode", "open"),
        ("state", "missing"),
        ("cell", "bad_cell"),
        ("next_fp", {"v": 2, "fields": {}}),
    ],
)
def test_malformed_directory_metadata_refuses(field, value):
    from server.router import restore

    value_by_name = descriptor()
    value_by_name[field] = value
    with pytest.raises(ValueError):
        restore.page(value_by_name, [])


@pytest.mark.parametrize(
    "orgs", [["b", "a"], ["a", "a"], ["-"], ["a\0b"], ["a" * (cell.ORG_MAX_BYTES + 1)]]
)
def test_invalid_or_nonincreasing_orgs_refuse(orgs):
    from server.router import restore

    answer = restore.page(descriptor(), ["a"])
    answer["orgs"] = orgs
    with pytest.raises(ValueError):
        restore.validate_page(answer, after=None, previous=None)


def test_hash_tamper_and_cursor_regression_refuse():
    from server.router import restore

    answer = restore.page(descriptor(), ["a"])
    answer["descriptor"]["mode"] = "recording"
    with pytest.raises(ValueError):
        restore.validate_page(answer, after=None, previous=None)
    answer = restore.page(descriptor(), ["a"])
    with pytest.raises(ValueError):
        restore.validate_page(answer, after="z", previous=None)


def test_count_change_restarts_but_configuration_change_refuses():
    from server.router import restore

    changed = copy.deepcopy(descriptor())
    changed["placed"] += 1
    with pytest.raises(restore.SnapshotChanged):
        restore.validate_page(restore.page(changed, ["a"]), after=None, previous=descriptor())
    changed["mode"] = "recording"
    with pytest.raises(ValueError) as error:
        restore.validate_page(restore.page(changed, ["a"]), after=None, previous=descriptor())
    assert not isinstance(error.value, restore.SnapshotChanged)


def test_maximum_length_orgs_progress_within_the_byte_bound():
    from server.router import restore

    orgs = [f"{n:04d}" + "x" * (cell.ORG_MAX_BYTES - 4) for n in range(501)]
    snap = descriptor()
    snap["placed"] = len(orgs)
    result = restore.page(snap, orgs)
    assert 0 < len(result["orgs"]) < 500
    assert result["done"] is False
    assert result["next"] == result["orgs"][-1]
    assert len(restore.encode(result)) <= restore.MAX_RESPONSE_BYTES


@pytest.mark.parametrize(
    "field,value",
    [("scim", 0), ("configured_orgs", "acme"), ("bearer_issuers", [1]), ("bucket", False)],
)
def test_fingerprint_known_types_are_exact(field, value):
    from server.router import restore

    snap = descriptor()
    snap["current_fp"]["fields"][field] = value
    with pytest.raises(ValueError):
        restore.page(snap, [])


def test_descriptor_does_not_silently_omit_unknown_fields():
    from server.router import restore

    snap = descriptor()
    snap["credential_sha256"] = "secret"
    with pytest.raises(ValueError):
        restore.page(snap, [])


def test_empty_first_page_cannot_claim_a_nonempty_snapshot_is_complete():
    from server.router import restore

    with pytest.raises(ValueError):
        restore.validate_page(restore.page(descriptor(), []), after=None, previous=None)


def test_serving_recording_descriptor_is_not_an_existing_lifecycle_state():
    from server.router import restore

    snap = descriptor()
    snap["mode"] = "recording"
    with pytest.raises(ValueError):
        restore.validate_descriptor(snap)


def test_response_byte_bound_also_applies_to_an_empty_snapshot():
    from server.router import restore

    snap = descriptor()
    snap["placed"] = 0
    snap["current_fp"]["fields"]["public_host"] = "x" * restore.MAX_RESPONSE_BYTES
    with pytest.raises(ValueError):
        restore.page(snap, [])


def test_unknown_newer_fingerprint_field_preserves_rolling_fieldwise_semantics():
    from server.router import restore

    snap = descriptor()
    snap["placed"] = 0
    snap["current_fp"]["fields"]["future_field"] = ["a future value"]
    answer = restore.page(snap, [])
    assert restore.validate_page(answer, after=None, previous=None) == snap
