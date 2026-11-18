"""Original counterparts follow complete raw source origins, never guessed headings."""

from dataclasses import replace
from hashlib import sha256

import pytest

from scripts.i18n import chunks
from scripts.i18n import translate as t
from scripts.i18n.origins import SourceOrigins
from scripts.i18n.reconcile import (
    OriginalUnit,
    PreparedSource,
    UnitFault,
    original_unit_manifest_sha256,
    reconcile_completed_units,
)

PATH = "docs/concepts/sample.md"
RAW = "# Sample\n\nIntro.\n\n## First\n\nOriginal first.\n\n## Second\n\nOriginal second.\n\n## Last\n\nEnd.\n"


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _case(current, *, historical=RAW, whole=False):
    old = t.anchor_headings(historical, t.Counter())
    now = t.anchor_headings(current, t.Counter())
    units, at = [], 0
    for i, part in enumerate((old,) if whole else chunks.heading_sections(old)):
        units.append(OriginalUnit(at, at + len(part), _digest(part), str(i)))
        at += len(part) + 1
    return (
        PreparedSource(PATH, old, _digest(old)),
        PreparedSource(PATH, now, _digest(now)),
        tuple(units),
        SourceOrigins(historical, current, "zh-CN"),
    )


def _plan(case, **kwargs):
    old, now, units, origins = case
    return reconcile_completed_units(
        old,
        now,
        units,
        unit_manifest_sha256=original_unit_manifest_sha256(units),
        source_origins=origins,
        **kwargs,
    )


def test_adjacent_changed_originals_have_separate_current_counterparts():
    current = RAW.replace("Original first.", "Added first.\nOriginal first.").replace(
        "Original second.", "Added second.\nOriginal second."
    )
    case = _case(current)
    plan = _plan(case)
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == ["reuse", "changed", "changed", "reuse"]
    assert tuple(u.original for u in plan.units) == case[2]
    assert (
        "Added first."
        in plan.units[1].scope.english[plan.units[1].scope.start : plan.units[1].scope.end]
    )
    assert (
        "Added second."
        not in plan.units[1].scope.english[plan.units[1].scope.start : plan.units[1].scope.end]
    )
    assert (
        "".join(
            u.separator_before + u.scope.english[u.scope.start : u.scope.end] for u in plan.units
        )
        + plan.trailing_separator
        == case[1].english
    )


def test_absent_origin_evidence_keeps_adjacent_change_refusal():
    case = _case(RAW.replace("Original", "New Original"))
    old, now, units, _ = case
    plan = reconcile_completed_units(
        old, now, units, unit_manifest_sha256=original_unit_manifest_sha256(units)
    )
    assert plan.conflicts[0].kind == "boundary"
    assert not plan.units


@pytest.mark.parametrize(
    "current",
    [
        RAW.replace("Original first.", "Replacement."),
        RAW.replace("## Second", "Inserted.\n\n## Second"),
        "Prefix.\n" + RAW,
        RAW + "Tail.\n",
        RAW.replace("## First", "## Last", 1),
    ],
)
def test_changed_or_unowned_raw_origins_refuse(current):
    plan = _plan(_case(current))
    assert plan.conflicts
    assert not plan.units


def test_origin_evidence_keeps_pair_refusal_and_unknown_fault_guards():
    case = _case(RAW.replace("Original", "Added Original"))
    assert _plan(case, pair_refused=True).conflicts[0].kind == "refusal"
    assert _plan(case, faults=(UnitFault("absent", "finding"),)).conflicts[0].kind == "context"


def test_counterpart_preserves_faults_and_whole_request_bound():
    case = _case(
        RAW.replace("Original first.", "Added first.\nOriginal first.").replace(
            "Original second.", "Added second.\nOriginal second."
        )
    )
    plan = _plan(case, faults=(UnitFault("1", "security"),))
    assert not plan.conflicts
    assert plan.units[1].fault_ids == ("security",)
    assert plan.units[1].disposition == "changed"
    refused = _plan(case, bounded_new_request_bytes=1)
    assert refused.conflicts[0].kind == "boundary"
    assert not refused.units


def test_unrelated_prepared_prose_cannot_claim_raw_source_origins():
    old, now, units, origins = _case(RAW.replace("Original", "Added Original"))
    forged = now.english.replace("Added", "Forged", 1)
    plan = _plan((old, replace(now, english=forged, sha256=_digest(forged)), units, origins))
    assert plan.conflicts
    assert not plan.units


def _routing_case(target=None):
    raw = RAW.replace("Original first.", "See [first](../guides/one.md).").replace(
        "Original second.", "See [second](../guides/two.md)."
    )
    old, now, units, origins = _case(raw, historical=raw)
    old_text = old.english.replace("../guides/", t.REPO_URL + "/blob/main/docs/guides/")
    old = replace(old, english=old_text, sha256=_digest(old_text))
    parts = chunks.heading_sections(old_text)
    at, original = 0, []
    for i, part in enumerate(parts):
        original.append(OriginalUnit(at, at + len(part), _digest(part), str(i)))
        at += len(part) + 1
    if target:
        text = now.english.replace("../guides/one.md", target)
        now = replace(now, english=text, sha256=_digest(text))
    return old, now, tuple(original), origins


def test_routing_only_counterparts_are_changed_without_repartition_or_free_replay():
    case = _routing_case()
    plan = _plan(case)
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == ["reuse", "changed", "changed", "reuse"]
    assert tuple(u.original for u in plan.units) == case[2]


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example/docs/guides/one.md",
        "../guides/one.md#invented",
        "../guides/one.md?query=1",
        t.REPO_URL + "/raw/main/docs/guides/one.md",
        "../guides/two.md",
    ],
)
def test_source_associated_routing_rejects_external_fragment_query_kind_or_occurrence(target):
    assert _plan(_routing_case(target)).conflicts


def test_source_proof_never_turns_an_incomplete_original_into_a_completed_counterpart():
    old, now, units, origins = _case(RAW)
    units = (replace(units[0], reply_ref=None), *units[1:])
    plan = _plan((old, now, units, origins))
    assert plan.conflicts
    assert not plan.units


@pytest.mark.parametrize("fault", [False, True], ids=["source-change", "source-change-with-fault"])
def test_source_origins_keep_changed_completed_whole_page_as_one_request(fault):
    current = RAW.replace("Original first.", "Added 界.\nOriginal first.")
    case = _case(current, whole=True)
    _, now, units, _ = case
    plan = _plan(
        case,
        faults=(UnitFault("0", "semantic"),) if fault else (),
        bounded_new_request_bytes=len(now.english.encode()),
    )
    assert not plan.conflicts
    assert len(plan.units) == 1
    request = plan.units[0]
    assert request.disposition == "changed"
    assert request.original == units[0]
    assert request.fault_ids == (("semantic",) if fault else ())
    assert request.scope.english[request.scope.start : request.scope.end] == now.english
    assert request.separator_before == plan.trailing_separator == ""
    assert not plan.removed


@pytest.mark.parametrize(
    "invalid", ["absent", "forged", "replacement", "ambiguous", "boundary", "language"]
)
def test_completed_whole_page_requires_valid_source_origins(invalid):
    current = RAW.replace("Original first.", "Added first.\nOriginal first.")
    if invalid == "replacement":
        current = RAW.replace("Original first.", "Replacement.")
    elif invalid == "ambiguous":
        current = current.replace("Intro.", "# Sample\n\nIntro.")
    elif invalid == "boundary":
        current += "Tail.\n"
    old, now, units, origins = _case(current, whole=True)
    if invalid == "forged":
        origins = replace(origins, current_raw=RAW)
    elif invalid == "language":
        origins = replace(origins, language="unknown")
    plan = reconcile_completed_units(
        old,
        now,
        units,
        unit_manifest_sha256=original_unit_manifest_sha256(units),
        source_origins=None if invalid == "absent" else origins,
    )
    assert plan.conflicts[0].kind == (
        "whole_page_replacement" if invalid == "absent" else "boundary"
    )
    assert not plan.units


@pytest.mark.parametrize("fault", [False, True], ids=["reuse", "correction"])
def test_source_origins_keep_same_input_whole_page_disposition(fault):
    case = _case(RAW, whole=True)
    plan = _plan(case, faults=(UnitFault("0", "semantic"),) if fault else ())
    assert not plan.conflicts
    assert len(plan.units) == 1
    assert plan.units[0].disposition == ("correction" if fault else "reuse")
    assert plan.units[0].original == case[2][0]


def test_changed_completed_whole_page_is_not_split_to_fit_request_bound():
    case = _case(RAW.replace("Original first.", "Added 界.\nOriginal first."), whole=True)
    plan = _plan(case, bounded_new_request_bytes=len(case[1].english.encode()) - 1)
    assert plan.conflicts[0].kind == "boundary"
    assert plan.conflicts[0].reply_refs == ("0",)
    assert not plan.units


@pytest.mark.parametrize("fits", [False, True])
def test_changed_counterpart_exact_utf8_bound_is_not_raised_or_split(fits):
    current = RAW.replace("Original first.", "Added 界.\nOriginal first.").replace(
        "Original second.", "Added second.\nOriginal second."
    )
    case = _case(current)
    parts = chunks.heading_sections(case[1].english)
    bound = max(len(parts[i].encode()) for i in (1, 2)) - int(not fits)
    plan = _plan(case, bounded_new_request_bytes=bound)
    assert bool(plan.conflicts) is not fits
    if fits:
        assert len(plan.units) == 4
        assert plan.units[1].disposition == plan.units[2].disposition == "changed"
    else:
        assert not plan.units


def test_fence_insertions_cannot_make_original_counterpart_cuts_unsafe():
    current = RAW.replace("## First\n\n", "## First\n\n```text\n").replace(
        "## Last", "```\n\n## Last"
    )
    plan = _plan(_case(current))
    assert plan.conflicts
    assert not plan.units


def test_inserted_heading_inside_original_does_not_create_a_new_paid_unit():
    current = RAW.replace(
        "Original first.", "### Added subsection\n\nAdded text.\n\nOriginal first."
    ).replace("Original second.", "Added second.\nOriginal second.")
    case = _case(current)
    plan = _plan(case)
    assert not plan.conflicts
    assert len(plan.units) == 4
    assert tuple(u.original for u in plan.units) == case[2]
    assert plan.units[1].disposition == plan.units[2].disposition == "changed"


def test_literal_code_links_remain_part_of_the_exact_source_not_routing_options():
    raw = RAW.replace("Original first.", "`[literal](../code.md)`\nOriginal first.")
    current = raw.replace("Original second.", "Added second.\nOriginal second.")
    old, now, units, origins = _case(current, historical=raw)
    forged = now.english.replace("../code.md", "../other.md")
    plan = _plan((old, replace(now, english=forged, sha256=_digest(forged)), units, origins))
    assert plan.conflicts
    assert not plan.units
