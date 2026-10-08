"""CH-free unit tests for the bi-temporal bench oracle.

Lives under bench/ (not tests/) so it travels with the harnesses it
guards. Runnable two ways:

    python bench/test_temporal_assertions.py     # standalone, exit 0/1
    pytest bench/test_temporal_assertions.py      # if pointed at bench/

`testpaths = ["tests"]` means the repo's default `pytest` run does NOT
collect this file; that is intentional — these benches require a live
control plane, so only their *pure* core is unit-tested, and it is
tested here, decoupled from the integration suite.
"""

from __future__ import annotations

from datetime import datetime

from bench.temporal_assertions import (
    forgetting_violation,
    step_field_ok,
    triple_visible_as_of,
)


def test_open_interval_always_visible() -> None:
    assert triple_visible_as_of(None, None, "2026-01-01")
    assert triple_visible_as_of("2020-01-01", None, "2026-01-01")
    # before the open interval starts -> not visible
    assert not triple_visible_as_of("2026-06-01", None, "2026-01-01")


def test_closed_interval_inclusive_both_ends() -> None:
    vf, vt = "2022-01-01", "2024-01-01"
    # strictly inside
    assert triple_visible_as_of(vf, vt, "2023-01-01")
    # both ends inclusive (mirrors valid_from <= as_of AND valid_to >= as_of)
    assert triple_visible_as_of(vf, vt, "2022-01-01")
    assert triple_visible_as_of(vf, vt, "2024-01-01")
    # strictly after the close -> gone
    assert not triple_visible_as_of(vf, vt, "2024-01-02")
    # strictly before the start -> not yet
    assert not triple_visible_as_of(vf, vt, "2021-12-31")


def test_forgetting_violation_only_after_close() -> None:
    # Acme employment closed 2024-01-01; surfacing it in 2025 is a
    # forgetting violation, surfacing it in 2023 is correct history.
    vf, vt = "2022-01-01", "2024-01-01"
    assert forgetting_violation(valid_from=vf, valid_to=vt, as_of="2025-06-01", surfaced=True)
    assert not forgetting_violation(valid_from=vf, valid_to=vt, as_of="2023-06-01", surfaced=True)
    # Not surfacing at all is a recall miss, not a forgetting violation.
    assert not forgetting_violation(valid_from=vf, valid_to=vt, as_of="2025-06-01", surfaced=False)
    # Still-open interval surfaced now -> never a violation.
    assert not forgetting_violation(valid_from=vf, valid_to=None, as_of="2026-06-01", surfaced=True)


def test_datetime_and_string_inputs_interop() -> None:
    assert triple_visible_as_of(datetime(2022, 1, 1), datetime(2024, 1, 1), datetime(2023, 1, 1))
    # tz-aware 'Z' string must not raise mixing aware/naive
    assert triple_visible_as_of("2022-01-01T00:00:00Z", None, "2023-01-01T12:00:00Z")


def test_step_field_value_match_with_coercion() -> None:
    assert step_field_ok(
        expected_value=32,
        expected_state="value",
        observed_value=32.0,
        observed_state="value",
    )
    assert step_field_ok(
        expected_value="32",
        expected_state="value",
        observed_value=32,
        observed_state="value",
    )
    assert step_field_ok(
        expected_value="ClickHouse",
        expected_state="value",
        observed_value="clickhouse",
        observed_state="value",
    )
    assert not step_field_ok(
        expected_value=32,
        expected_state="value",
        observed_value=33,
        observed_state="value",
    )


def test_step_field_state_mismatch_and_cleared() -> None:
    # state mismatch always fails
    assert not step_field_ok(
        expected_value=1,
        expected_state="value",
        observed_value=1,
        observed_state="absent",
    )
    # cleared field: matched on state alone, value ignored
    assert step_field_ok(
        expected_value=None,
        expected_state="absent",
        observed_value="stale",
        observed_state="absent",
    )
    assert step_field_ok(
        expected_value=None,
        expected_state="unknown",
        observed_value=None,
        observed_state="unknown",
    )


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:  # pragma: no cover - smoke path
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
