"""A namespace's first full backfill implies the edges-only
second pass — indexing order otherwise leaves every call
to a later file unresolved with zero candidates."""

from __future__ import annotations

from server.cli.code_backfill import second_pass_wanted


def test_first_full_backfill_implies_the_second_pass() -> None:
    assert (
        second_pass_wanted(explicit=False, declined=False, paths_file=False, done_upto="") is True
    )


def test_a_resumed_run_or_a_repair_does_not_imply_it() -> None:
    assert (
        second_pass_wanted(explicit=False, declined=False, paths_file=False, done_upto="412")
        is False
    )
    assert (
        second_pass_wanted(explicit=False, declined=False, paths_file=True, done_upto="") is False
    )


def test_the_flags_decide_when_given() -> None:
    assert second_pass_wanted(explicit=True, declined=False, paths_file=True, done_upto="9") is True
    assert (
        second_pass_wanted(explicit=False, declined=True, paths_file=False, done_upto="") is False
    )
