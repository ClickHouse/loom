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
@pytest.mark.parametrize("replacement", [False, True], ids=["insertion", "interior-replacement"])
def test_source_origins_keep_changed_completed_whole_page_as_one_request(fault, replacement):
    change = "Replacement 界." if replacement else "Added 界.\nOriginal first."
    current = RAW.replace("Original first.", change)
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
    "invalid", ["absent", "forged", "endpoint", "ambiguous", "boundary", "language"]
)
def test_completed_whole_page_requires_valid_source_origins(invalid):
    current = RAW.replace("Original first.", "Added first.\nOriginal first.")
    if invalid == "endpoint":
        current = RAW.replace("# Sample\n", "# Replacement\n", 1)
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


def _interior_replacement_case(*, adjacent=False):
    historical = RAW.replace("Original first.", "Original first.\nRetained first tail.").replace(
        "Original second.", "Original second.\nRetained second tail."
    )
    current = historical.replace("Original first.", "Replacement first.")
    if adjacent:
        current = current.replace("Original second.", "Replacement second.")
    return _case(current, historical=historical)


@pytest.mark.parametrize("adjacent", [False, True])
def test_retained_endpoints_prove_interior_replacements_without_new_cuts(adjacent):
    case = _interior_replacement_case(adjacent=adjacent)
    plan = _plan(case, faults=(UnitFault("1", "semantic"),))
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == [
        "reuse",
        "changed",
        "changed" if adjacent else "reuse",
        "reuse",
    ]
    assert tuple(u.original for u in plan.units) == case[2]
    assert plan.units[1].fault_ids == ("semantic",)
    assert (
        "".join(
            u.separator_before + case[1].english[u.scope.start : u.scope.end] for u in plan.units
        )
        + plan.trailing_separator
        == case[1].english
    )


@pytest.mark.parametrize("problem", ["missing-endpoint", "reordered-retained-lines"])
def test_replacements_keep_endpoint_loss_and_reordering_refusals(problem):
    historical = RAW.replace("Original first.", "Original first.\nRetained first tail.")
    if problem == "missing-endpoint":
        current = historical.replace("Retained first tail.", "Replacement tail.")
    else:
        current = historical.replace(
            "Original first.\nRetained first tail.", "Retained first tail.\nOriginal first."
        )
    plan = _plan(_case(current, historical=historical))
    assert plan.conflicts
    assert not plan.units


def test_completed_history_uses_the_same_interior_replacement_proof():
    from scripts.i18n import completed_history as h

    old, now, units, origins = _interior_replacement_case(adjacent=True)
    ranges = tuple(h.RequestRange(u.start, u.end, u.source_sha256) for u in units)
    ident = h.partition_manifest_sha256(old, ranges)
    partitions = (h.RequestPartition(old, ranges, ident),)
    completions = tuple(h.CompletionOrigin(ident, i, str(i)) for i in range(len(units)))
    eligible = {str(i): True for i in range(len(units))}
    selected = tuple(str(i) for i in range(len(units)))
    plan = h.reconcile_completed_history(
        now,
        partitions,
        completions,
        selected,
        history_manifest_sha256=h.completion_history_sha256(partitions, completions, eligible),
        eligible_reuse=eligible,
        source_origins={ident: origins},
    )
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == ["reuse", "changed", "changed", "reuse"]
    assert tuple(u.original.reply_ref for u in plan.units) == selected
    assert len(plan.partitions) == 1
    assert len(plan.completions) == len(units)


@pytest.mark.parametrize("fits", [False, True])
def test_interior_replacement_keeps_the_exact_utf8_request_bound(fits):
    old, now, units, origins = _interior_replacement_case()
    raw = origins.current_raw.replace("Replacement first.", "Replacement 界界.")
    case = _case(raw, historical=origins.historical_raw)
    size = len(chunks.heading_sections(case[1].english)[1].encode())
    plan = _plan(case, bounded_new_request_bytes=size - int(not fits))
    assert bool(plan.conflicts) is not fits
    if fits:
        assert plan.units[1].original == units[1]
        assert plan.units[1].disposition == "changed"
    else:
        assert not plan.units


@pytest.mark.parametrize("trailing", ["", "\n\n"])
@pytest.mark.parametrize(
    "suffix", ["\nAdded paragraph.\n", "\nOne paragraph.\n\nAnother paragraph.\n"]
)
def test_terminal_paragraph_append_extends_only_the_original_final_section(trailing, suffix):
    historical = RAW + trailing
    case = _case(historical + suffix, historical=historical)
    plan = _plan(case)
    assert not plan.conflicts
    assert len(plan.units) == len(case[2]) == 4
    assert [u.disposition for u in plan.units] == ["reuse", "reuse", "reuse", "changed"]
    assert tuple(u.original for u in plan.units) == case[2]
    assert all(
        (u.scope.start, u.scope.end) == (original.start, original.end)
        for u, original in zip(plan.units[:-1], case[2][:-1], strict=True)
    )
    assert plan.units[-1].scope.start == case[2][-1].start
    assert plan.units[-1].scope.end == len(case[1].english)
    assert case[1].english[plan.units[-1].scope.start :].endswith(suffix)
    assert (
        "".join(
            u.separator_before + u.scope.english[u.scope.start : u.scope.end] for u in plan.units
        )
        == case[1].english
    )
    assert not plan.removed and not plan.trailing_separator


def _terminal_route_case(*, shorten=True, suffix="\nAppended.\n"):
    link = "../guides/one.md"
    raw = RAW.replace("Original first.", f"Original first. [Guide]({link}).").replace(
        "End.", f"End. [Guide]({link})."
    )
    routes = sorted(t.link_routes(link, False, PATH, "zh-CN"), key=len)
    assert len(routes) == 2

    def routed(text, native):
        return t.replace_spans(
            text,
            [(a, b, routes[0 if native else 1]) for a, b, _, _ in t.link_targets(text)],
        )

    old, now, units, _ = _case(routed(raw + suffix, shorten), historical=routed(raw, not shorten))
    return old, now, units, SourceOrigins(raw, raw + suffix, "zh-CN")


@pytest.mark.parametrize("shorten", [False, True])
def test_terminal_append_uses_authenticated_routes_when_prepared_prefix_changes(shorten):
    case = _terminal_route_case(shorten=shorten)
    old, now, units, _ = case
    assert not now.english.startswith(old.english)
    assert (len(now.english) < len(old.english)) is shorten
    plan = _plan(case)
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == ["reuse", "changed", "reuse", "changed"]
    assert tuple(u.original for u in plan.units) == units
    assert plan.units[-1].scope.end == len(now.english)
    assert (
        "".join(
            u.separator_before + u.scope.english[u.scope.start : u.scope.end] for u in plan.units
        )
        == now.english
    )


@pytest.mark.parametrize("problem", ["prose", "route"])
def test_terminal_route_changes_still_require_complete_prepared_authentication(problem):
    old, now, units, origins = _terminal_route_case()
    text = now.english.replace(
        "Original first." if problem == "prose" else "../guides/one.md",
        "Forged prose." if problem == "prose" else "../guides/unrelated.md",
    )
    plan = _plan((old, replace(now, english=text, sha256=_digest(text)), units, origins))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize(
    "suffix", ["\n## Added\n\nText.\n", "\n- Item.\n", "\n<b>Text</b>\n", "\n{: .x}\n"]
)
def test_terminal_route_changes_do_not_relax_suffix_ownership(suffix):
    plan = _plan(_terminal_route_case(suffix=suffix))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize(
    "suffix",
    [
        "Tail.\n",
        "\n## Added section\n\nNew text.\n",
        "\n```text\nUnclosed.\n",
        "\n```text\nClosed.\n```\n",
        "\n- New list item.\n",
        "\n> Quoted paragraph.\n",
        "\nA | B\n--- | ---\n1 | 2\n",
        "\n<div>HTML</div>\n",
        "\nInline <b>HTML</b>.\n",
        "\n[id]: https://example.com\n\nAdded paragraph.\n",
        "\n<!-- languages -->\n",
        "\n!!! note\n    Site extension.\n",
        "\n{: .attribute}\n",
        "\nNew text without a terminal newline.",
    ],
)
def test_terminal_append_does_not_claim_new_blocks_or_ambiguous_context(suffix):
    plan = _plan(_case(RAW + suffix))
    assert plan.conflicts and not plan.units


def test_terminal_proof_does_not_move_an_interior_boundary():
    current = RAW.replace("## Last", "Inserted paragraph.\n\n## Last") + "\nAppended.\n"
    plan = _plan(_case(current))
    assert plan.conflicts and not plan.units


def test_terminal_append_requires_the_final_original_to_own_its_heading():
    historical = RAW.replace("End.", "Earlier paragraph.\n\nEnd.")
    old, now, _, origins = _case(historical + "\nAppended.\n", historical=historical)
    start = old.english.index("End.")
    parts = (old.english[: start - 1], old.english[start:])
    units = (
        OriginalUnit(0, start - 1, _digest(parts[0]), "first"),
        OriginalUnit(start, len(old.english), _digest(parts[1]), "last"),
    )
    plan = _plan((old, now, units, origins))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("problem", ["forged-raw", "forged-prepared", "incomplete"])
def test_terminal_append_keeps_authentication_and_completion_guards(problem):
    old, now, units, origins = _case(RAW + "\nAppended.\n")
    if problem == "forged-raw":
        origins = replace(origins, current_raw=RAW)
    elif problem == "forged-prepared":
        text = now.english.replace("Appended", "Forged")
        now = replace(now, english=text, sha256=_digest(text))
    else:
        units = (*units[:-1], replace(units[-1], reply_ref=None))
    plan = _plan((old, now, units, origins))
    assert plan.conflicts and not plan.units


def test_terminal_extension_cannot_split_an_oversized_changed_original():
    case = _case(RAW + "\n" + "界" * chunks.MAX_BYTES + ".\n")
    plan = _plan(case)
    assert plan.conflicts and not plan.units


def test_terminal_paragraph_after_a_related_list_keeps_inline_link_origins():
    historical = RAW.replace("End.", "- [One](../guides/one.md).\n- [Two](../guides/two.md).")
    suffix = "\nRead the [guide](../guides/one.md) for\nmore detail.\n"
    case = _case(historical + suffix, historical=historical)
    plan = _plan(case)
    assert not plan.conflicts
    assert len(plan.units) == 4 and plan.units[-1].disposition == "changed"
    assert tuple(u.original for u in plan.units) == case[2]
    assert case[1].english[plan.units[-1].scope.start :].endswith(suffix)


def test_terminal_append_refuses_a_site_ordered_list_continuation():
    historical = RAW.replace("End.", "1. Original related.")
    plan = _plan(_case(historical + "\n  Appended paragraph.\n", historical=historical))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize(
    "suffix",
    [
        "\nAdded paragraph.\n{ .class }\n",
        "\nAdded **paragraph**{:.class}.\n",
        "\nAdded **paragraph**{.class}.\n",
        "\nAdded paragraph }.\n",
    ],
)
def test_terminal_append_refuses_brace_marked_text(suffix):
    plan = _plan(_case(RAW + suffix))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("fits", [False, True])
def test_terminal_counterpart_keeps_the_exact_utf8_bound(fits):
    case = _case(RAW + "\nAppended 界.\n")
    size = len(chunks.heading_sections(case[1].english)[-1].encode())
    plan = _plan(case, bounded_new_request_bytes=size - int(not fits))
    assert bool(plan.conflicts) is not fits
    if fits:
        assert len(plan.units) == 4 and plan.units[-1].original == case[2][-1]
        assert plan.units[-1].disposition == "changed"
    else:
        assert not plan.units
