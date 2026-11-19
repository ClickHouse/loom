"""Completed requests keep the partition where each reply actually originated."""

from dataclasses import replace
from hashlib import sha256

import pytest

from scripts.i18n import chunks
from scripts.i18n import completed_history as h
from scripts.i18n import translate as t
from scripts.i18n.origins import SourceOrigins
from scripts.i18n.reconcile import PreparedSource, UnitFault


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _partition(parts, separator="\n"):
    text = separator.join(parts)
    source = PreparedSource("docs/cost.md", text, _digest(text))
    ranges, at = [], 0
    for part in parts:
        ranges.append(h.RequestRange(at, at + len(part), _digest(part)))
        at += len(part) + len(separator)
    ranges = tuple(ranges)
    return h.RequestPartition(source, ranges, h.partition_manifest_sha256(source, ranges))


def _case():
    old_parts = ("Intro.\n", "Agent summary.\n", "Old table.\n", "Summary.\n", "End.\n")
    new_parts = (*old_parts[:2], "Current table.\n", *old_parts[3:])
    old, new = _partition(old_parts), _partition(new_parts)
    catalog = tuple(h.CompletionOrigin(old.manifest_sha256, i, f"old-{i}") for i in range(5))
    catalog += (h.CompletionOrigin(new.manifest_sha256, 2, "new-table"),)
    selected = ("old-0", "old-1", "new-table", "old-3", "old-4")
    eligible = {
        origin.reply_ref: origin.reply_ref not in {"old-1", "new-table"} for origin in catalog
    }
    return new.source, (old, new), catalog, selected, eligible


def _plan(case, *, faults=None, **kwargs):
    current, partitions, catalog, selected, eligible = case
    if faults is None:
        faults = (UnitFault("old-1", "role"), UnitFault("new-table", "role"))
    metadata = kwargs.pop(
        "history_manifest_sha256", h.completion_history_sha256(partitions, catalog, eligible)
    )
    return h.reconcile_completed_history(
        current,
        partitions,
        catalog,
        selected,
        history_manifest_sha256=metadata,
        eligible_reuse=eligible,
        faults=faults,
        **kwargs,
    )


def test_two_generations_preserve_two_faults_and_three_good_neighbors():
    case = _case()
    current, partitions, catalog, selected, _ = case
    old, new = partitions
    plan = _plan(case)
    assert not plan.conflicts
    assert [unit.disposition for unit in plan.units] == [
        "reuse",
        "correction",
        "correction",
        "reuse",
        "reuse",
    ]
    assert tuple(unit.original.reply_ref for unit in plan.units) == selected
    assert plan.units[1].original.partition_id == old.manifest_sha256
    assert plan.units[2].original.partition_id == new.manifest_sha256
    assert plan.completions == catalog
    assert plan.partitions == partitions
    assert (
        "".join(
            u.separator_before + u.scope.english[u.scope.start : u.scope.end] for u in plan.units
        )
        == current.english
    )
    assert "old-2" in dict(plan.eligible_reuse)
    assert "old-2" not in selected


@pytest.mark.parametrize(
    ("old_separator", "current_separator"),
    [("\n", ""), ("", "\n")],
    ids=["LF-to-empty", "empty-to-LF"],
)
def test_exact_matches_preserve_each_origin_separator(old_separator, current_separator):
    parts = ("First.\n\n", "Last.\n")
    partition = _partition(parts, old_separator)
    text = current_separator.join(parts)
    current = PreparedSource(partition.source.path, text, _digest(text))
    catalog = tuple(
        h.CompletionOrigin(partition.manifest_sha256, i, f"reply-{i}") for i in range(2)
    )
    selected = tuple(origin.reply_ref for origin in catalog)
    eligible = {key: True for key in selected}
    plan = _plan((current, (partition,), catalog, selected, eligible), faults=())
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("invalid", ["source", "manifest", "range", "unsafe", "coverage"])
def test_partition_evidence_must_bind_every_whole_range(invalid):
    current, partitions, catalog, selected, eligible = _case()
    old, new = partitions
    if invalid == "source":
        old = replace(old, source=replace(old.source, sha256="forged"))
    elif invalid == "manifest":
        old = replace(old, manifest_sha256="forged")
    elif invalid == "range":
        old = replace(old, ranges=(replace(old.ranges[0], source_sha256="forged"), *old.ranges[1:]))
    elif invalid == "unsafe":
        end = old.ranges[0].end - 2
        ranges = (
            h.RequestRange(0, end, _digest(old.source.english[:end])),
            h.RequestRange(
                end, old.ranges[1].end, _digest(old.source.english[end : old.ranges[1].end])
            ),
            *old.ranges[2:],
        )
        old = replace(old, ranges=ranges)
    else:
        old = replace(old, ranges=old.ranges[1:])
    plan = _plan((current, (old, new), catalog, selected, eligible))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("invalid", ["duplicate", "None", "slot", "partition", "restamp", "omit"])
def test_completion_variants_and_full_history_cannot_be_rebound(invalid):
    current, partitions, catalog, selected, eligible = _case()
    metadata = h.completion_history_sha256(partitions, catalog, eligible)
    if invalid == "duplicate":
        catalog += (catalog[1],)
    elif invalid == "None":
        catalog = (replace(catalog[0], reply_ref=None), *catalog[1:])
    elif invalid == "slot":
        catalog = (replace(catalog[0], slot=True), *catalog[1:])
    elif invalid == "partition":
        catalog = (replace(catalog[0], partition_id="absent"), *catalog[1:])
    elif invalid == "restamp":
        catalog = (
            catalog[0],
            replace(catalog[1], partition_id=partitions[1].manifest_sha256),
            *catalog[2:],
        )
    else:
        catalog = tuple(c for c in catalog if c.reply_ref != "old-2")
        eligible = {key: value for key, value in eligible.items() if key != "old-2"}
    plan = _plan(
        (current, partitions, catalog, selected, eligible), history_manifest_sha256=metadata
    )
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("eligible", [{}, {"old-0": True}, {"old-0": "yes"}])
def test_reuse_policy_is_explicit_for_the_entire_catalog(eligible):
    current, partitions, catalog, selected, _ = _case()
    assert _plan((current, partitions, catalog, selected, eligible)).conflicts


def test_reuse_policy_has_no_default_admission():
    current, partitions, catalog, selected, _ = _case()
    with pytest.raises(TypeError, match="eligible_reuse"):
        h.reconcile_completed_history(
            current, partitions, catalog, selected, history_manifest_sha256=""
        )


def test_complete_reuse_policy_requires_boolean_values():
    current, partitions, catalog, selected, eligible = _case()
    eligible["old-0"] = 1
    assert _plan((current, partitions, catalog, selected, eligible)).conflicts


def test_blocked_unfaulted_reply_does_not_become_reuse():
    current, partitions, catalog, selected, eligible = _case()
    eligible["old-0"] = False
    plan = _plan((current, partitions, catalog, selected, eligible))
    assert "not eligible" in plan.conflicts[0].message


def test_nonselected_variant_faults_remain_bound_in_the_result():
    case = _case()
    case[-1]["old-2"] = False
    faults = (
        UnitFault("old-1", "role"),
        UnitFault("new-table", "role"),
        UnitFault("old-2", "old-fault"),
    )
    plan = _plan(case, faults=faults)
    assert not plan.conflicts
    assert plan.faults == faults
    assert "old-2" not in {unit.original.reply_ref for unit in plan.units}


@pytest.mark.parametrize("allowed_alternative", [False, True])
def test_exact_reply_variants_need_individual_faults_and_free_resolution(allowed_alternative):
    current, partitions, catalog, selected, eligible = _case()
    other = h.CompletionOrigin(partitions[1].manifest_sha256, 1, "old-1:other-reply-bytes")
    catalog += (other,)
    eligible[other.reply_ref] = allowed_alternative
    faults = (UnitFault("old-1", "role"), UnitFault("new-table", "role"))
    if not allowed_alternative:
        faults += (UnitFault(other.reply_ref, "other-variant-finding"),)
    plan = _plan((current, partitions, catalog, selected, eligible), faults=faults)
    if allowed_alternative:
        assert "requires reuse" in plan.conflicts[0].message
        assert not plan.units
    else:
        assert not plan.conflicts
        assert plan.units[1].fault_ids == ("role",)
        assert other in plan.completions


def test_faults_cannot_infer_reply_variant_identity_from_a_shared_request():
    plan = _plan(_case(), faults=(UnitFault("old-1:unlisted-reply", "role"),))
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("selection", ["duplicate", "overlap", "reorder", "gap", "unknown"])
def test_explicit_reply_selection_cannot_supply_a_partition(selection):
    current, partitions, catalog, selected, eligible = _case()
    if selection == "overlap":
        other = h.CompletionOrigin(partitions[0].manifest_sha256, 0, "same-slot-other-variant")
        catalog += (other,)
        eligible[other.reply_ref] = True
    changed = {
        "duplicate": selected + (selected[-1],),
        "overlap": (selected[0], "same-slot-other-variant", *selected[1:]),
        "reorder": tuple(reversed(selected)),
        "gap": selected[1:],
        "unknown": ("missing", *selected[1:]),
    }[selection]
    plan = _plan((current, partitions, catalog, changed, eligible))
    assert plan.conflicts and not plan.units


def _whole_case(current=None):
    raw = "# Guide\n\nBefore.\n\nAfter.\n"
    now = current if current is not None else raw
    old = _partition((t.anchor_headings(raw, t.Counter()),))
    source = _partition((t.anchor_headings(now, t.Counter()),)).source
    origin = h.CompletionOrigin(old.manifest_sha256, 0, "actual-whole-variant")
    case = source, (old,), (origin,), (origin.reply_ref,), {origin.reply_ref: True}
    return case, SourceOrigins(raw, now, "ko")


@pytest.mark.parametrize("replacement", [False, True], ids=["insertion", "interior-replacement"])
def test_changed_whole_preserves_origin_and_never_becomes_same_input_correction(replacement):
    raw = "# Guide\n\nBefore.\nAdded 界.\n\nAfter.\n"
    if replacement:
        raw = raw.replace("Before.", "Replaced.")
    case, origins = _whole_case(raw)
    case[-1][case[2][0].reply_ref] = False
    plan = _plan(
        case,
        faults=(UnitFault(case[2][0].reply_ref, "finding"),),
        source_origins={case[1][0].manifest_sha256: origins},
    )
    assert not plan.conflicts
    assert len(plan.units) == 1
    assert plan.units[0].disposition == "changed"
    assert plan.units[0].original == case[2][0]
    assert plan.units[0].fault_ids == ("finding",)
    assert plan.partitions == case[1]
    assert plan.completions == case[2]
    assert plan.history_manifest_sha256 == h.completion_history_sha256(case[1], case[2], case[-1])
    assert (plan.units[0].scope.start, plan.units[0].scope.end) == (0, len(case[0].english))


@pytest.mark.parametrize("fits", [False, True])
@pytest.mark.parametrize("replacement", [False, True], ids=["insertion", "interior-replacement"])
def test_changed_whole_uses_utf8_bound_without_splitting(fits, replacement):
    raw = "# Guide\n\nBefore.\nAdded 界.\n\nAfter.\n"
    if replacement:
        raw = raw.replace("Before.", "Replaced.")
    case, origins = _whole_case(raw)
    plan = _plan(
        case,
        faults=(),
        source_origins={case[1][0].manifest_sha256: origins},
        bounded_new_request_bytes=len(case[0].english.encode()) - int(not fits),
    )
    assert bool(plan.conflicts) is not fits
    if fits:
        assert len(plan.units) == 1 and plan.units[0].disposition == "changed"
    else:
        assert not plan.units


def test_non_lf_separator_cannot_join_selected_requests():
    partition = _partition(("First.\n", "Last.\n"))
    text = partition.source.english.replace("\n\n", "\n \n")
    current = PreparedSource(partition.source.path, text, _digest(text))
    catalog = tuple(h.CompletionOrigin(partition.manifest_sha256, i, str(i)) for i in range(2))
    plan = _plan((current, (partition,), catalog, ("0", "1"), {"0": True, "1": True}), faults=())
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("eligible", [False, True])
def test_completed_current_input_cannot_be_repaid_as_changed_old_input(eligible):
    case, origins = _whole_case("# Guide\n\nBefore.\nAdded.\n\nAfter.\n")
    current, partitions, catalog, selected, policy = case
    current_partition = _partition((current.english,))
    partitions += (current_partition,)
    catalog += (h.CompletionOrigin(current_partition.manifest_sha256, 0, "already-completed"),)
    policy["already-completed"] = eligible
    plan = _plan(
        (current, partitions, catalog, selected, policy),
        faults=(),
        source_origins={partitions[0].manifest_sha256: origins},
    )
    assert "already completed" in plan.conflicts[0].message
    assert not plan.units


@pytest.mark.parametrize(
    "bad", ["absent", "forged", "endpoint", "ambiguous", "boundary", "language"]
)
def test_changed_whole_requires_valid_raw_proof(bad):
    raw = "# Guide\n\nBefore.\nAdded.\n\nAfter.\n"
    if bad == "endpoint":
        raw = raw.replace("# Guide\n", "# Changed\n", 1)
    elif bad == "ambiguous":
        raw = raw.replace("Before.", "# Guide\n\nBefore.")
    elif bad == "boundary":
        raw += "Tail.\n"
    case, origins = _whole_case(raw)
    if bad == "forged":
        origins = replace(origins, current_raw=origins.historical_raw)
    elif bad == "language":
        origins = replace(origins, language="unknown")
    evidence = {} if bad == "absent" else {case[1][0].manifest_sha256: origins}
    plan = _plan(case, faults=(), source_origins=evidence)
    assert plan.conflicts and not plan.units


def test_invalid_supplied_proof_does_not_fall_back_to_exact_reuse():
    case, origins = _whole_case()
    plan = _plan(
        case,
        faults=(),
        source_origins={case[1][0].manifest_sha256: replace(origins, current_raw="forged")},
    )
    assert plan.conflicts and not plan.units


@pytest.mark.parametrize("fits", [False, True])
def test_faulty_current_whole_obeys_exact_utf8_bound(fits):
    case, _ = _whole_case()
    key = case[2][0].reply_ref
    case[-1][key] = False
    plan = _plan(
        case,
        faults=(UnitFault(key, "finding"),),
        bounded_new_request_bytes=len(case[0].english.encode()) - int(not fits),
    )
    assert bool(plan.conflicts) is not fits
    if fits:
        assert len(plan.units) == 1 and plan.units[0].disposition == "correction"
    else:
        assert not plan.units


@pytest.mark.parametrize("bound", [True, 0, -1, 1.5, "8192", 16385])
def test_invalid_bound_is_rejected_even_before_reuse(bound):
    case, _ = _whole_case()
    with pytest.raises(ValueError, match="byte bound"):
        _plan(case, faults=(), bounded_new_request_bytes=bound)


def test_unchanged_reuse_may_exceed_the_request_bound_but_refusal_still_wins():
    case, _ = _whole_case()
    assert _plan(case, faults=(), bounded_new_request_bytes=1).units[0].disposition == "reuse"
    refusal = _plan(case, pair_refused=True)
    assert refusal.conflicts[0].kind == "refusal" and not refusal.units


def _terminal_case():
    raw = "# Intro\n\nOpening.\n" + "".join(
        f"\n## Section {i}\n\nOriginal paragraph {i}.\n" for i in range(6)
    )
    current_raw = raw + (
        "\nDefinition drafts from knowledge gaps and query history are visible only to namespace\n"
        "admins, including their queue counts and lineage. Approving one publishes its complete\n"
        "value, including supporting SQL, to namespace readers.\n"
    )
    old = _partition(chunks.heading_sections(t.anchor_headings(raw, t.Counter())))
    current = _partition(chunks.heading_sections(t.anchor_headings(current_raw, t.Counter())))
    catalog = tuple(h.CompletionOrigin(old.manifest_sha256, i, f"reply-{i}") for i in range(7))
    selected = tuple(origin.reply_ref for origin in catalog)
    case = current.source, (old,), catalog, selected, {key: True for key in selected}
    return case, SourceOrigins(raw, current_raw, "ja"), current


def test_terminal_append_retains_seven_genuine_slots_with_six_free_neighbors():
    case, origins, _ = _terminal_case()
    plan = _plan(case, faults=(), source_origins={case[1][0].manifest_sha256: origins})
    assert not plan.conflicts
    assert [u.disposition for u in plan.units] == ["reuse"] * 6 + ["changed"]
    assert tuple(u.original for u in plan.units) == case[2]
    assert plan.completions == case[2] and plan.partitions == case[1]
    changed = plan.units[-1].scope.english[plan.units[-1].scope.start :]
    assert "only to namespace\nadmins" in changed
    assert "queue counts and lineage" in changed
    assert (
        "publishes its complete\nvalue, including supporting SQL, to namespace readers" in changed
    )
    assert (
        "".join(
            u.separator_before + u.scope.english[u.scope.start : u.scope.end] for u in plan.units
        )
        == case[0].english
    )


def test_terminal_append_needs_raw_evidence_even_when_every_old_whole_still_matches():
    case, _, _ = _terminal_case()
    plan = _plan(case, faults=())
    assert plan.conflicts and not plan.units


def test_terminal_changed_input_keeps_already_completed_current_input_priority():
    case, origins, current_partition = _terminal_case()
    current, partitions, catalog, selected, eligible = case
    complete = h.CompletionOrigin(current_partition.manifest_sha256, 6, "current-final")
    catalog += (complete,)
    partitions += (current_partition,)
    eligible[complete.reply_ref] = True
    evidence = {partitions[0].manifest_sha256: origins}
    refused = _plan(
        (current, partitions, catalog, selected, eligible), faults=(), source_origins=evidence
    )
    assert refused.conflicts and "already completed" in refused.conflicts[0].message
    assert not refused.units
    selected = (*selected[:-1], complete.reply_ref)
    reused = _plan(
        (current, partitions, catalog, selected, eligible), faults=(), source_origins=evidence
    )
    assert not reused.conflicts
    assert all(u.disposition == "reuse" for u in reused.units)
    assert reused.units[-1].original == complete


@pytest.mark.parametrize("problem", ["refused", "incomplete", "oversized"])
def test_terminal_history_keeps_refusal_coverage_and_byte_bound(problem):
    case, origins, _ = _terminal_case()
    current, partitions, catalog, selected, eligible = case
    kwargs = {"pair_refused": True} if problem == "refused" else {}
    if problem == "incomplete":
        old = partitions[0]
        ranges = old.ranges[:-1]
        incomplete = replace(
            old, ranges=ranges, manifest_sha256=h.partition_manifest_sha256(old.source, ranges)
        )
        partitions = (incomplete,)
    elif problem == "oversized":
        kwargs["bounded_new_request_bytes"] = 1
    plan = _plan(
        (current, partitions, catalog, selected, eligible),
        faults=(),
        source_origins={partitions[0].manifest_sha256: origins},
        **kwargs,
    )
    assert plan.conflicts and not plan.units
