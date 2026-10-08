"""Persisted plans reject missing, ambiguous or substituted table identities."""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan


@pytest.fixture
def manifest():
    return RebuildManifest(
        uuid4(),
        "a" * 64,
        tuple(
            RebuildTablePlan(
                table,
                uuid4(),
                "ReplacingMergeTree",
                ("content", "embedding"),
                "b" * 64,
                uuid4(),
                uuid4(),
            )
            for table in REBUILD_TABLES
        ),
    )


def test_json_roundtrip_preserves_every_identity(manifest):
    serialized = json.loads(json.dumps(manifest.to_dict()))
    assert RebuildManifest.from_dict(serialized) == manifest


def test_nonempty_prefix_policy_survives_recovery(manifest):
    manifest = replace(manifest, prefix_dimensions=(256, 1024))
    restored = RebuildManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored == manifest
    assert restored.prefix_dimensions == (256, 1024)


@pytest.mark.parametrize("policy", [None, "256", [True], [0], [65536], [256, 256]])
def test_invalid_persisted_prefix_policy_is_rejected(manifest, policy):
    data = manifest.to_dict()
    data["prefix_dimensions"] = policy
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


def test_missing_prefix_policy_is_not_assumed_to_mean_empty(manifest):
    data = manifest.to_dict()
    del data["prefix_dimensions"]
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


def test_prepared_successor_cannot_change_prefix_policy(manifest):
    prepared = replace(
        manifest,
        tables=tuple(
            replace(table, prepared_uuid=uuid4(), verified_rows=0) for table in manifest.tables
        ),
        prefix_dimensions=(256,),
    )
    with pytest.raises(ValueError, match="changed"):
        prepared.prepared_successor(manifest)


@pytest.mark.parametrize("version", [0, 1, 3, True, "2", None])
def test_unknown_format_never_defaults_to_current(manifest, version):
    data = manifest.to_dict()
    data["version"] = version
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered", "unknown"])
def test_inventory_must_be_complete_and_ordered(manifest, change):
    data = manifest.to_dict()
    if change == "missing":
        data["tables"].pop()
    elif change == "duplicate":
        data["tables"][1] = data["tables"][0]
    elif change == "reordered":
        data["tables"].reverse()
    else:
        data["tables"][0]["table"] = "unrelated"
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


@pytest.mark.parametrize("field", ["source_uuid", "snapshot_uuid", "target_uuid"])
@pytest.mark.parametrize("value", [None, str(UUID(int=0)), "not-a-uuid", 42])
def test_table_identities_cannot_be_missing_or_zero(manifest, field, value):
    data = manifest.to_dict()
    data["tables"][0][field] = value
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


def test_cross_table_uuid_alias_is_rejected(manifest):
    data = manifest.to_dict()
    data["tables"][1]["target_uuid"] = data["tables"][0]["snapshot_uuid"]
    with pytest.raises(ValueError, match="distinct"):
        RebuildManifest.from_dict(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("columns", []),
        ("columns", ["x", "x"]),
        ("columns", ["x; DROP TABLE"]),
        ("columns", "content"),
        ("columns", ["_loom_rebuild_ordinal"]),
        ("engine", "Memory"),
        ("schema_fingerprint", "not-a-hash"),
        ("verified_rows", 0),
    ],
)
def test_invalid_table_metadata_is_rejected(manifest, field, value):
    data = manifest.to_dict()
    data["tables"][0][field] = value
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


def test_unknown_table_fields_are_not_silently_dropped(manifest):
    data = manifest.to_dict()
    data["tables"][0]["unrecognized"] = "value"
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


@pytest.mark.parametrize("count", [None, True, -1, "0"])
def test_prepared_table_requires_explicit_nonnegative_integer_count(manifest, count):
    data = manifest.to_dict()
    data["tables"][0].update(prepared_uuid=str(uuid4()), verified_rows=count)
    with pytest.raises(ValueError):
        RebuildManifest.from_dict(data)


def test_prepared_successor_retains_all_original_identities(manifest):
    prepared = replace(
        manifest,
        tables=tuple(
            replace(table, prepared_uuid=uuid4(), verified_rows=0) for table in manifest.tables
        ),
    )
    prepared.prepared_successor(manifest)
    assert RebuildManifest.from_dict(prepared.to_dict()) == prepared
    with pytest.raises(ValueError, match="unprepared"):
        manifest.prepared_successor(manifest)
    substituted = replace(
        prepared, tables=(replace(prepared.tables[0], prepared_uuid=uuid4()), *prepared.tables[1:])
    )
    with pytest.raises(ValueError, match="changed"):
        substituted.prepared_successor(prepared)
