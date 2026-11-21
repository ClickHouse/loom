"""Completed translation units retain their authenticated cuts across source edits."""

from dataclasses import replace
from hashlib import sha256
from itertools import pairwise

import pytest

from scripts.i18n import chunks
from scripts.i18n.context import SourceContext
from scripts.i18n.reconcile import (
    OriginalUnit,
    PreparedSource,
    UnitFault,
    original_unit_manifest_sha256,
    reconcile_completed_units,
)


def _reconcile(historical, current, units, **kwargs):
    return reconcile_completed_units(
        historical,
        current,
        units,
        unit_manifest_sha256=original_unit_manifest_sha256(units),
        **kwargs,
    )


def _source(text):
    return PreparedSource("docs/guide.md", text, sha256(text.encode()).hexdigest())


def _history(*parts):
    return _history_with_separators(parts, ("\n",) * (len(parts) - 1))


def _history_with_separators(parts, separators):
    source = _source("".join(p + s for p, s in zip(parts, (*separators, ""), strict=True)))
    units, start = [], 0
    for i, (part, separator) in enumerate(zip(parts, (*separators, ""), strict=True)):
        units.append(
            OriginalUnit(start, start + len(part), sha256(part.encode()).hexdigest(), str(i))
        )
        start += len(part) + len(separator)
    return source, tuple(units)


def _texts(plan):
    return [u.scope.english[u.scope.start : u.scope.end] for u in plan.units]


def _assert_assembly(plan):
    assert not plan.conflicts
    assert (
        "".join(u.separator_before + text for u, text in zip(plan.units, _texts(plan), strict=True))
        + plan.trailing_separator
        == plan.current.english
    )


def test_large_completed_whole_page_is_free_reuse():
    old, units = _history("An indivisible paragraph " + "界" * 6000)
    plan = _reconcile(old, old, units)
    _assert_assembly(plan)
    assert [u.disposition for u in plan.units] == ["reuse"]
    assert plan.units[0].original == units[0]


def test_insertion_uses_current_context_and_keeps_original_neighbors():
    old, units = _history("First paragraph.\n", "Final paragraph.\n")
    current = _source("First paragraph.\n\nInserted paragraph.\n\nFinal paragraph.\n")
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert [u.disposition for u in plan.units] == ["reuse", "new", "reuse"]
    assert [u.original for u in plan.units] == [units[0], None, units[1]]
    assert all(
        u.scope.path == current.path and u.scope.english == current.english for u in plan.units
    )


def test_removal_never_repackages_retained_units():
    old, units = _history("First.\n", "Removed.\n", "Last.\n")
    current = _source("First.\n\nLast.\n")
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert [u.original for u in plan.units] == [units[0], units[2]]
    assert plan.removed == (units[1],)


def test_changed_unit_coalesces_faults_without_old_assistant_replay():
    old, units = _history("First.\n", "Old statement.\n", "Last.\n")
    current = _source("First.\n\nChanged statement.\n\nLast.\n")
    faults = (UnitFault("1", "semantic"), UnitFault("1", "language"), UnitFault("1", "semantic"))
    plan = _reconcile(old, current, units, faults=faults)
    _assert_assembly(plan)
    assert [u.disposition for u in plan.units] == ["reuse", "changed", "reuse"]
    assert plan.units[1].fault_ids == ("language", "semantic")
    assert _texts(plan)[1] == "Changed statement.\n"


def test_unchanged_fault_is_explicit_original_unit_correction():
    old, units = _history("First.\n", "Last.\n")
    plan = _reconcile(old, old, units, faults=(UnitFault("1", "semantic"),))
    _assert_assembly(plan)
    assert [u.disposition for u in plan.units] == ["reuse", "correction"]
    assert plan.units[1].original == units[1]


@pytest.mark.parametrize("text", ["Repeated.\n\nRepeated.\n", "Repeated.\n\nExtra.\n\nRepeated.\n"])
def test_repeated_whole_unit_is_an_explicit_ambiguity(text):
    old, units = _history("Repeated.\n", "Repeated.\n")
    plan = _reconcile(old, _source(text), units)
    assert [c.kind for c in plan.conflicts] == ["ambiguity"]
    assert not plan.units


@pytest.mark.parametrize("wrapper", [("<div>\n\n", "\n</div>\n"), ("```text\n", "```\n")])
def test_identical_text_inside_a_new_protected_container_is_not_reused(wrapper):
    old, units = _history("First.\n", "Last.\n")
    current = _source(wrapper[0] + old.english + wrapper[1])
    plan = _reconcile(old, current, units)
    assert plan.conflicts
    assert not plan.units


def test_pair_refusal_blocks_even_unchanged_source():
    old, units = _history("A paragraph.\n")
    plan = _reconcile(old, old, units, pair_refused=True)
    assert [c.kind for c in plan.conflicts] == ["refusal"]
    assert not plan.units


@pytest.mark.parametrize("change", ["container", "unit", "gap", "context"])
def test_unauthenticated_or_incomplete_history_is_a_conflict(change):
    old, units = _history("First.\n", "Last.\n")
    current = old
    if change == "container":
        old = replace(old, sha256="wrong")
    elif change == "unit":
        units = (replace(units[0], source_sha256="wrong"), units[1])
    elif change == "gap":
        units = units[1:]
    else:
        current = replace(current, path="docs/other.md")
    plan = _reconcile(old, current, units)
    assert plan.conflicts
    assert not plan.units


def test_changed_historical_whole_page_is_not_repaid():
    old, units = _history("Original page.\n")
    plan = _reconcile(old, _source("Changed page.\n"), units)
    assert [c.kind for c in plan.conflicts] == ["whole_page_replacement"]


def test_multiple_changed_units_refuse_to_guess_a_new_partition():
    old, units = _history("First.\n", "Old one.\n", "Old two.\n", "Last.\n")
    current = _source("First.\n\nReplacement one.\n\nReplacement two.\n\nLast.\n")
    plan = _reconcile(old, current, units)
    assert [c.kind for c in plan.conflicts] == ["boundary"]
    assert not plan.units


def test_reordered_units_are_not_reused_in_new_order():
    old, units = _history("First.\n", "Last.\n")
    plan = _reconcile(old, _source("Last.\n\nFirst.\n"), units)
    assert [c.kind for c in plan.conflicts] == ["ambiguity"]


def test_fault_cannot_target_an_unknown_reply():
    old, units = _history("First.\n", "Last.\n")
    plan = _reconcile(old, old, units, faults=(UnitFault("absent", "fault"),))
    assert [c.kind for c in plan.conflicts] == ["context"]


def test_faulty_large_original_unit_cannot_be_sent_again():
    old, units = _history("First.\n", "界" * 6000 + "\n", "Last.\n")
    free = _reconcile(old, old, units)
    _assert_assembly(free)
    repair = _reconcile(old, old, units, faults=(UnitFault("1", "fault"),))
    assert [c.kind for c in repair.conflicts] == ["boundary"]
    assert not repair.units


def test_new_gap_is_bounded_without_splitting_completed_neighbors():
    old, units = _history("First.\n", "Last.\n")
    paragraphs = "Inserted paragraph " + "界" * 2000 + "\n\n"
    current = _source("First.\n\n" + paragraphs * 3 + "Last.\n")
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert plan.units[0].original == units[0] and plan.units[-1].original == units[-1]
    assert len(plan.units) > 3
    assert all(
        len(text.encode()) <= chunks.MAX_BYTES
        for u, text in zip(plan.units, _texts(plan), strict=True)
        if u.disposition != "reuse"
    )


def test_twenty_one_original_units_with_only_first_two_completed():
    old, units = _history(*(f"Unit {i:02d}. " + "source " * 200 + "\n" for i in range(21)))
    units = tuple(unit if i < 2 else replace(unit, reply_ref=None) for i, unit in enumerate(units))
    plan = _reconcile(old, old, units)
    _assert_assembly(plan)
    assert [unit.original for unit in plan.units if unit.disposition == "reuse"] == list(units[:2])
    assert all(unit.original is None for unit in plan.units if unit.disposition == "new")
    assert plan.original_units == units
    assert plan.unit_manifest_sha256 == original_unit_manifest_sha256(units)
    assert all(
        len(text.encode()) <= chunks.MAX_BYTES
        for unit, text in zip(plan.units, _texts(plan), strict=True)
        if unit.disposition == "new"
    )


@pytest.mark.parametrize("incomplete", [0, 1, 2], ids=["start", "middle", "end"])
def test_explicit_incomplete_unit_is_new_without_repaying_completed_neighbors(incomplete):
    old, units = _history("First.\n", "Middle.\n", "Last.\n")
    units = tuple(
        replace(unit, reply_ref=None) if i == incomplete else unit for i, unit in enumerate(units)
    )
    plan = _reconcile(old, old, units)
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == [
        "new" if i == incomplete else "reuse" for i in range(3)
    ]
    assert [unit.original for unit in plan.units if unit.disposition == "reuse"] == [
        unit for unit in units if unit.reply_ref is not None
    ]


def test_incomplete_gap_tracks_changed_english_outside_completed_cuts():
    old, units = _history("First.\n", "Unfinished one.\n", "Unfinished two.\n", "Last.\n")
    units = (
        units[0],
        replace(units[1], reply_ref=None),
        replace(units[2], reply_ref=None),
        units[3],
    )
    current = _source("First.\n\nA changed gap.\n\nAnother new paragraph.\n\nLast.\n")
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == ["reuse", "new", "reuse"]
    assert plan.units[0].original == units[0] and plan.units[-1].original == units[-1]
    # New work owns its full safe current gap; no historical completed cut is changed.
    assert _texts(plan)[1] == "A changed gap.\n\nAnother new paragraph.\n\n"
    assert plan.units[-1].separator_before == ""


def test_partial_plan_preserves_completed_deletion_and_incomplete_gaps():
    old, units = _history("Prefix.\n", "First.\n", "Removed.\n", "Last.\n", "Suffix.\n")
    units = (replace(units[0], reply_ref=None), *units[1:4], replace(units[4], reply_ref=None))
    current = _source("Prefix.\n\nFirst.\n\nLast.\n\nSuffix.\n")
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert plan.removed == (units[2],)
    assert [unit.disposition for unit in plan.units] == ["new", "reuse", "reuse", "new"]


def test_deleted_mixed_completed_and_incomplete_gap_creates_no_replacement():
    old, units = _history("First.\n", "Finished then removed.\n", "Unfinished.\n", "Last.\n")
    units = (units[0], units[1], replace(units[2], reply_ref=None), units[3])
    plan = _reconcile(old, _source("First.\n\nLast.\n"), units)
    _assert_assembly(plan)
    assert plan.removed == units[1:3]
    assert [unit.disposition for unit in plan.units] == ["reuse", "reuse"]


def test_missing_incomplete_manifest_range_is_still_an_authentication_failure():
    old, units = _history("First.\n", "Unfinished.\n", "Last.\n")
    units = (units[0], replace(units[1], reply_ref=None), units[2])
    plan = _reconcile(old, old, (units[0], units[2]))
    assert [conflict.kind for conflict in plan.conflicts] == ["authentication"]


def test_completed_reply_cannot_be_relabelled_incomplete_under_authenticated_manifest():
    old, units = _history("First.\n", "Bad settled reply.\n", "Last.\n")
    digest = original_unit_manifest_sha256(units)
    changed = (units[0], replace(units[1], reply_ref=None), units[2])
    plan = reconcile_completed_units(old, old, changed, unit_manifest_sha256=digest)
    assert [conflict.kind for conflict in plan.conflicts] == ["authentication"]
    correction = _reconcile(old, old, units, faults=(UnitFault("1", "semantic"),))
    _assert_assembly(correction)
    assert correction.units[1].disposition == "correction"


def test_uncompleted_unit_cannot_be_a_settled_reply_fault_target():
    old, units = _history("First.\n", "Unfinished.\n", "Last.\n")
    units = (units[0], replace(units[1], reply_ref=None), units[2])
    plan = _reconcile(old, old, units, faults=(UnitFault("1", "fault"),))
    assert [conflict.kind for conflict in plan.conflicts] == ["context"]


def test_partial_history_does_not_clear_repeated_completed_unit_ambiguity_or_refusal():
    old, units = _history("Repeated.\n", "Unfinished.\n", "Repeated.\n")
    units = (units[0], replace(units[1], reply_ref=None), units[2])
    ambiguous = _reconcile(old, old, units)
    assert [conflict.kind for conflict in ambiguous.conflicts] == ["ambiguity"]
    refused = _reconcile(old, old, units, pair_refused=True)
    assert [conflict.kind for conflict in refused.conflicts] == ["refusal"]


def test_never_completed_whole_page_can_receive_its_first_bounded_plan():
    old, units = _history("Not completed.\n")
    units = (replace(units[0], reply_ref=None),)
    plan = _reconcile(old, _source("Current English.\n"), units)
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == ["new"]


def _results_original_cut_layout():
    # Exact original cut lengths and separator ownership; the prose itself is synthetic.
    ends = [
        7743,
        15093,
        22443,
        30553,
        38672,
        46676,
        54724,
        62801,
        70902,
        78853,
        87016,
        94631,
        102627,
        110343,
        111578,
    ]
    sizes = [ends[0], *(b - a for a, b in pairwise(ends))]
    intro = "Intro " + "a" * (671 - len("Intro ") - 1) + "\n"
    table = []
    for i, size in enumerate(sizes):
        prefix = ("| Item | Value |\n| --- | --- |\n" if i == 0 else "") + f"| Row {i} | "
        table.append(prefix + "a" * (size - len(prefix) - 3) + " |\n")
    tail = [
        f"Tail {i} " + "a" * (size - len(f"Tail {i} ") - 1) + "\n"
        for i, size in enumerate([2083, 1675, 942, 1045, 289])
    ]
    parts = [intro, *table, *tail]
    old, units = _history_with_separators(parts, ("\n", *("",) * 14, *("\n",) * 5))
    units = tuple(unit if i < 2 else replace(unit, reply_ref=None) for i, unit in enumerate(units))
    assert len(old.english) == 118289
    assert (units[1].start, units[1].end, units[2].start) == (672, 8415, 8415)
    return old, units


@pytest.mark.parametrize("insert", [False, True], ids=["unchanged", "paragraph-insertion"])
def test_actual_twenty_one_cut_layout_keeps_two_completed_payloads(insert):
    old, units = _results_original_cut_layout()
    current = (
        _source(old.english[:672] + "A new paragraph.\n\n" + old.english[672:]) if insert else old
    )
    plan = _reconcile(old, current, units)
    _assert_assembly(plan)
    assert [unit.original for unit in plan.units if unit.disposition == "reuse"] == list(units[:2])
    assert all(
        len(text.encode()) <= chunks.MAX_BYTES
        for unit, text in zip(plan.units, _texts(plan), strict=True)
        if unit.disposition == "new"
    )


def test_contiguous_completed_anchors_accept_no_assembly_separator():
    old, units = _history_with_separators(("First.\n\n", "Last.\n"), ("",))
    plan = _reconcile(old, old, units)
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == ["reuse", "reuse"]
    assert plan.units[1].separator_before == ""


def test_changed_contiguous_completed_counterpart_preserves_its_outer_newlines():
    old, units = _history_with_separators(("First.\n\n", "Old.\n\n", "Last.\n"), ("", ""))
    current = _source("First.\n\nChanged.\n\nLast.\n")
    plan = _reconcile(old, current, units, faults=(UnitFault("1", "fault"),))
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == ["reuse", "changed", "reuse"]
    assert _texts(plan)[1] == "Changed.\n\n"


def test_contiguous_completed_deletion_preserves_neighbors():
    old, units = _history_with_separators(("First.\n\n", "Removed.\n\n", "Last.\n"), ("", ""))
    plan = _reconcile(old, _source("First.\n\nLast.\n"), units)
    _assert_assembly(plan)
    assert plan.removed == (units[1],)
    assert [unit.original for unit in plan.units] == [units[0], units[2]]


def test_incomplete_unsafe_old_cut_is_replanned_outside_completed_ranges():
    old, units = _history_with_separators(("First.\n\n", "New para", "graph.\n"), ("", ""))
    units = (units[0], replace(units[1], reply_ref=None), replace(units[2], reply_ref=None))
    plan = _reconcile(old, old, units)
    _assert_assembly(plan)
    assert [unit.disposition for unit in plan.units] == ["reuse", "new"]
    assert _texts(plan)[1] == "New paragraph.\n"


def test_contiguous_completed_cut_inside_a_paragraph_is_still_refused():
    old, units = _history_with_separators(("First para", "graph.\n"), ("",))
    plan = _reconcile(old, old, units)
    assert [conflict.kind for conflict in plan.conflicts] == ["boundary"]


def test_headerless_gap_uses_full_current_table_rows_and_preserves_separator_lf():
    header = "| Item | Value |\n| --- | --- |\n"
    rows = [f"| Row {i} | " + "a" * 1000 + " |\n" for i in range(25)]
    english = header + "".join(rows)
    start, end = len(header + rows[0]), len(english) - 1
    text = english[start:end]
    scope = SourceContext("docs/table.md", english, start, end)
    parts = chunks.split(text, source_context=scope)
    assert len(parts) >= 2
    assert "\n".join(parts) == text
    assert all(len(part.encode()) <= chunks.MAX_BYTES for part in parts)
    assert all(part.startswith("| Row ") for part in parts)


@pytest.mark.parametrize("invalid", ["foreign", "negative", "past_end", "cell", "fence"])
def test_contextual_split_refuses_foreign_or_unsafe_ranges_before_shortcut(invalid):
    english = "| Field | Meaning |\n| --- | --- |\n| A | inside a cell |\n"
    start, end = 0, len(english)
    if invalid == "foreign":
        text = "A different input."
    else:
        if invalid == "negative":
            start = -1
        elif invalid == "past_end":
            end += 1
        elif invalid == "cell":
            end = english.index("inside") + 3
        else:
            english = "```text\ninside code\n```\n"
            end = english.index("code") + len("code\n")
        text = english[start:end]
    scope = SourceContext("docs/table.md", english, start, end)
    with pytest.raises(ValueError, match="source context|parser-safe"):
        chunks.split(text, source_context=scope)


@pytest.mark.parametrize("insert", [False, True], ids=["unchanged", "insertion"])
def test_request_bound_preserves_completed_table_payloads_and_default_plan(insert):
    old, units = _results_original_cut_layout()
    current = (
        _source(old.english[:672] + "Inserted paragraph.\n\n" + old.english[672:])
        if insert
        else old
    )
    default = _reconcile(old, current, units)
    assert _reconcile(old, current, units, bounded_new_request_bytes=chunks.MAX_BYTES) == default
    smaller = _reconcile(old, current, units, bounded_new_request_bytes=8192)
    _assert_assembly(smaller)
    assert smaller.original_units == units
    assert smaller.unit_manifest_sha256 == default.unit_manifest_sha256
    assert [u.original for u in smaller.units if u.disposition == "reuse"] == list(units[:2])
    assert all(
        u.scope.path == current.path and u.scope.english == current.english for u in smaller.units
    )
    assert any(
        len(text.encode()) > 8192
        for u, text in zip(default.units, _texts(default), strict=True)
        if u.disposition == "new"
    )
    assert all(
        len(text.encode()) <= 8192
        for u, text in zip(smaller.units, _texts(smaller), strict=True)
        if u.disposition != "reuse"
    )


def test_request_bound_counts_utf8_and_preserves_default_split():
    text = ("Paragraph " + "界" * 1800 + "\n\n") * 4
    assert len(text) < 8192 < len(text.encode())
    assert chunks.split(text, bounded_new_request_bytes=chunks.MAX_BYTES) == chunks.split(text)
    parts = chunks.split(text, bounded_new_request_bytes=8192)
    assert len(parts) > 1
    assert "\n".join(parts) == text
    assert all(len(part.encode()) <= 8192 for part in parts)


def test_request_bound_keeps_headerless_rows_in_their_current_container():
    header = "| Item | Value |\n| --- | --- |\n"
    rows = [f"| Row {i} | " + "a" * 1000 + " |\n" for i in range(12)]
    english = header + "".join(rows)
    start, end = len(header + rows[0]), len(english) - 1
    text = english[start:end]
    scope = SourceContext("docs/table.md", english, start, end)
    assert chunks.split(text, source_context=scope) == [text]
    parts = chunks.split(text, source_context=scope, bounded_new_request_bytes=8192)
    assert len(parts) > 1
    assert "\n".join(parts) == text
    assert all(part.startswith("| Row ") and len(part.encode()) <= 8192 for part in parts)


def test_request_bound_refuses_an_indivisible_unit_that_fits_the_default():
    text = "An indivisible paragraph " + "a" * 9000
    assert chunks.split(text) == [text]
    with pytest.raises(ValueError, match="8192-byte"):
        chunks.split(text, bounded_new_request_bytes=8192)


@pytest.mark.parametrize("bound", [1, 8192])
def test_request_bound_never_reslices_a_large_free_completed_page(bound):
    old, units = _history("An indivisible paragraph " + "界" * 6000)
    plan = _reconcile(old, old, units, bounded_new_request_bytes=bound)
    _assert_assembly(plan)
    assert len(plan.units) == 1
    assert plan.units[0].disposition == "reuse" and plan.units[0].original == units[0]
    assert _texts(plan) == [old.english]


@pytest.mark.parametrize("disposition", ["changed", "correction"])
@pytest.mark.parametrize("bound", [8192, 10240], ids=["conflict", "whole-request"])
def test_request_bound_keeps_completed_repairs_whole_or_conflicts(disposition, bound):
    old, units = _history("First.\n", "Old " + "a" * 9000 + "\n", "Last.\n")
    current = _source(old.english.replace("Old ", "New ")) if disposition == "changed" else old
    faults = (UnitFault("1", "semantic"), UnitFault("1", "language"))
    plan = _reconcile(old, current, units, faults=faults, bounded_new_request_bytes=bound)
    if bound == 8192:
        assert [c.kind for c in plan.conflicts] == ["boundary"]
        assert not plan.units
    else:
        _assert_assembly(plan)
        assert [u.disposition for u in plan.units] == ["reuse", disposition, "reuse"]
        assert plan.units[1].original == units[1]
        assert plan.units[1].fault_ids == ("language", "semantic")
        assert _texts(plan)[1] == current.english[units[1].start : units[1].end]


@pytest.mark.parametrize("bound", [True, 0, -1, 1.5, "8192", chunks.MAX_BYTES + 1])
@pytest.mark.parametrize("text", ["", "A short paragraph.\n"], ids=["empty", "short"])
def test_invalid_request_bound_is_rejected_before_split_shortcuts(bound, text):
    with pytest.raises(ValueError, match="byte bound"):
        chunks.split(text, bounded_new_request_bytes=bound)


@pytest.mark.parametrize("bound", [True, 0, -1, 1.5, "8192", chunks.MAX_BYTES + 1])
@pytest.mark.parametrize("empty", [False, True], ids=["all-reuse", "empty"])
def test_invalid_request_bound_is_rejected_before_reconciliation_shortcuts(bound, empty):
    old, units = (_source(""), ()) if empty else _history("A completed paragraph.\n")
    with pytest.raises(ValueError, match="byte bound"):
        _reconcile(old, old, units, bounded_new_request_bytes=bound)


def test_request_bound_allows_a_one_byte_new_request():
    old, units = _history("x")
    units = (replace(units[0], reply_ref=None),)
    plan = _reconcile(old, old, units, bounded_new_request_bytes=1)
    _assert_assembly(plan)
    assert _texts(plan) == ["x"]
    assert [u.disposition for u in plan.units] == ["new"]
