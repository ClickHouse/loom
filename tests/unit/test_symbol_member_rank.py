"""A bare name answers its top-level declaration before same-named members,
whatever kind the tier wrote."""

from __future__ import annotations

from server.code.store.index import (
    demote_generated_paths,
    demote_member_rows,
    demote_noise_paths,
)

# ---------------------------------------------------------------- top-level first


def _row(qn: str, path: str, kind: str = "function", tier: str = "scip") -> dict:
    return {"qualified_name": qn, "path": path, "kind": kind, "tier": tier}


def test_top_level_declaration_outranks_a_same_named_field_method_or_enum_member() -> None:
    # a struct field, a method and a field must not beat the generic function
    # / the type of the same bare name under the kind rule
    rows = [
        _row(
            "clickhouse-operator.metrics.resourcecount::Registration::ForObject",
            "clickhouse-operator/internal/metrics/resourcecount/registry.go",
            kind="class",
        ),
        _row(
            "clickhouse-autoscaler.internal.cachewait.cachewait::ForObject",
            "clickhouse-autoscaler/internal/cachewait/cachewait.go",
        ),
    ]
    out = demote_member_rows(rows)
    assert [r["path"].split("/")[0] for r in out] == [
        "clickhouse-autoscaler",
        "clickhouse-operator",
    ]
    rows = [
        _row(
            "capacity-controller.api.v1.v1::AWSCapacityReservationList::ListMeta", "a.go", "field"
        ),
        _row("stresshouse.sdk.api.http.v2.v2::ListMeta", "b.go", "struct"),
    ]
    assert demote_member_rows(rows)[0]["path"] == "b.go"


def test_partition_is_stable_and_keeps_equal_depth_order() -> None:
    rows = [
        _row("a::X", "1.go"),
        _row("b::T::X", "2.go"),
        _row("c::X", "3.go"),
        _row("d::U::X", "4.go"),
    ]
    assert [r["path"] for r in demote_member_rows(rows)] == ["1.go", "3.go", "2.go", "4.go"]
    assert demote_member_rows([]) == []
    only_members = [_row("a::T::X", "1.go"), _row("b::U::X", "2.go")]
    assert demote_member_rows(only_members) == only_members  # nothing shallower: untouched


def test_member_partition_composes_with_the_generated_and_noise_partitions() -> None:
    # a hand-written member still beats a generated top-level row,
    # and a top-level row in tests/ still sits last (noise)
    rows = [
        _row("gen.v1::X", "api/v1/x.pb.go"),
        _row("pkg.svc::T::X", "svc/t.go"),
        _row("pkg.svc::X", "svc/tests/x_test.go"),
    ]
    out = demote_generated_paths(demote_noise_paths(demote_member_rows(rows)))
    assert [r["path"] for r in out] == ["svc/t.go", "api/v1/x.pb.go", "svc/tests/x_test.go"]
