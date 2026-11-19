"""Whole completed requests with distinct, caller-authenticated source partitions.

Partitions describe genuine complete planned layouts, not completion or absence claims.
Callers authenticate every layout, actual reply variant and complete same-pair history
against receipts, payloads, defaults, policy and settlements. Digests bind metadata only.
Reuse eligibility is required for every catalog entry; callers must authenticate it.
This pure planner neither resolves replies nor admits provider calls or publication.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal

from scripts.i18n import chunks
from scripts.i18n.context import SourceContext
from scripts.i18n.origins import SourceOrigins, partition_counterparts
from scripts.i18n.reconcile import (
    PreparedSource,
    ReconciliationConflict,
    UnitFault,
    _matches,
    _safe_end,
)


@dataclass(frozen=True)
class RequestRange:
    start: int
    end: int
    source_sha256: str


@dataclass(frozen=True)
class RequestPartition:
    """Full genuine planning layout; a retained slot is not a new paid original."""

    source: PreparedSource
    ranges: tuple[RequestRange, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class CompletionOrigin:
    """One actual reply variant in its own original partition, never a carried alias.

    reply_ref is an opaque caller-authenticated variant identity. A cache request key
    alone is insufficient when that key can identify different reply bytes.
    """

    partition_id: str
    slot: int
    reply_ref: str


@dataclass(frozen=True)
class OriginPlannedUnit:
    scope: SourceContext
    disposition: Literal["reuse", "correction", "changed"]
    original: CompletionOrigin
    fault_ids: tuple[str, ...] = ()
    separator_before: str = ""


@dataclass(frozen=True)
class HistoryReconciliation:
    current: PreparedSource
    partitions: tuple[RequestPartition, ...]
    completions: tuple[CompletionOrigin, ...]
    eligible_reuse: tuple[tuple[str, bool], ...]
    source_origins: tuple[tuple[str, SourceOrigins], ...]
    history_manifest_sha256: str
    faults: tuple[UnitFault, ...] = ()
    units: tuple[OriginPlannedUnit, ...] = ()
    conflicts: tuple[ReconciliationConflict, ...] = ()
    trailing_separator: str = ""


def _digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def _metadata_digest(value) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def partition_manifest_sha256(source: PreparedSource, ranges: tuple[RequestRange, ...]) -> str:
    return _metadata_digest(
        {"schema": 1, "source": asdict(source), "ranges": [asdict(r) for r in ranges]}
    )


def completion_history_sha256(partitions, completions, eligible_reuse) -> str:
    """Bind the entire catalog and explicit eligibility, not just selected replies."""
    return _metadata_digest(
        {
            "schema": 1,
            "partitions": [asdict(p) for p in partitions],
            "completions": [asdict(c) for c in completions],
            "eligible_reuse": dict(eligible_reuse),
        }
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _source_cuts(source, path):
    _require(type(source) is PreparedSource, "actual PreparedSource required")
    _require(
        isinstance(source.english, str)
        and isinstance(source.path, str)
        and bool(source.path)
        and source.path == path
        and _digest(source.english) == source.sha256,
        "prepared source path or hash differs",
    )
    return set(chunks.request_boundaries(source.english))


def _partition_catalog(partitions, current):
    _require(type(partitions) is tuple and bool(partitions), "complete partition tuple required")
    result = {}
    for partition in partitions:
        _require(type(partition) is RequestPartition, "actual RequestPartition required")
        cuts = _source_cuts(partition.source, current.path)
        _require(type(partition.ranges) is tuple, "complete range tuple required")
        text, at = partition.source.english, 0
        for index, unit in enumerate(partition.ranges):
            _require(type(unit) is RequestRange, "actual RequestRange required")
            _require(
                type(unit.start) is int
                and type(unit.end) is int
                and at <= unit.start < unit.end <= len(text)
                and (unit.start == 0 if index == 0 else True)
                and text[at : unit.start] in {"", "\n"}
                and _digest(text[unit.start : unit.end]) == unit.source_sha256,
                "partition range, source hash or coverage differs",
            )
            _require(
                unit.start in cuts and _safe_end(text, unit.end, cuts),
                "partition cuts are not parser-safe",
            )
            at = unit.end
        _require(at == len(text), "partition omits source")
        _require(
            partition_manifest_sha256(partition.source, partition.ranges)
            == partition.manifest_sha256
            and partition.manifest_sha256 not in result,
            "partition manifest differs or repeats",
        )
        result[partition.manifest_sha256] = partition
    return result


def _completion_catalog(completions, partitions, eligible):
    _require(type(completions) is tuple, "complete completion tuple required")
    result, texts = {}, {}
    for origin in completions:
        _require(type(origin) is CompletionOrigin, "actual CompletionOrigin required")
        _require(
            isinstance(origin.reply_ref, str)
            and bool(origin.reply_ref)
            and origin.reply_ref not in result
            and isinstance(origin.partition_id, str)
            and origin.partition_id in partitions,
            "completion reference or partition differs",
        )
        partition = partitions[origin.partition_id]
        _require(
            type(origin.slot) is int and 0 <= origin.slot < len(partition.ranges),
            "completion does not name a whole planned slot",
        )
        unit = partition.ranges[origin.slot]
        texts[origin.reply_ref] = partition.source.english[unit.start : unit.end]
        result[origin.reply_ref] = origin
    _require(
        isinstance(eligible, Mapping)
        and set(eligible) == set(result)
        and all(type(value) is bool for value in eligible.values()),
        "explicit reuse eligibility required for every completion",
    )
    return result, texts


def _faults(faults, catalog, eligible):
    _require(type(faults) is tuple, "fault tuple required")
    result = {key: set() for key in catalog}
    for fault in faults:
        _require(
            type(fault) is UnitFault
            and isinstance(fault.reply_ref, str)
            and fault.reply_ref in catalog
            and isinstance(fault.finding_id, str)
            and bool(fault.finding_id),
            "fault does not identify an actual reply variant and finding",
        )
        _require(not eligible[fault.reply_ref], "faulty reply cannot be eligible for reuse")
        result[fault.reply_ref].add(fault.finding_id)
    return {key: tuple(sorted(ids)) for key, ids in result.items()}


def _origin_projections(evidence, partitions, current):
    _require(isinstance(evidence, Mapping), "source origin mapping required")
    result = {}
    for key, sources in evidence.items():
        _require(
            key in partitions
            and type(sources) is SourceOrigins
            and all(isinstance(v, str) for v in asdict(sources).values()),
            "source origin partition or evidence differs",
        )
        partition = partitions[key]
        result[key] = partition_counterparts(partition.source, current, partition.ranges, sources)
    return result


def _selected_units(
    current, selected, catalog, texts, partitions, projections, fault_ids, eligible, bound
):
    _require(
        type(selected) is tuple and all(isinstance(key, str) for key in selected),
        "explicit selected reply tuple required",
    )
    _require(len(set(selected)) == len(selected), "selected reply repeats")
    cuts = set(chunks.request_boundaries(current.english))
    planned, at = [], 0
    for key in selected:
        _require(key in catalog, "selected reply is absent from complete history")
        origin = catalog[key]
        partition = partitions[origin.partition_id]
        slot = partition.ranges[origin.slot]
        matches = _matches(partition.source.english, slot, current.english, cuts)
        _require(len(matches) <= 1, "selected whole has ambiguous current matches")
        projected = projections.get(origin.partition_id)
        _require(projected is not None or bool(matches), "changed whole needs raw source origins")
        start, end = projected[origin.slot] if projected is not None else matches[0]
        separator = current.english[at:start]
        previous_end = partition.ranges[origin.slot - 1].end if origin.slot else 0
        original_separator = partition.source.english[previous_end : slot.start]
        _require(
            at <= start < end <= len(current.english)
            and separator in {"", "\n"}
            and separator == original_separator
            and start in cuts
            and _safe_end(current.english, end, cuts),
            "selected ranges overlap, reorder, omit source or change separators",
        )
        text, ids = current.english[start:end], fault_ids[key]
        changed = text != texts[key]
        disposition: Literal["reuse", "correction", "changed"] = (
            "changed" if changed else "correction" if ids else "reuse"
        )
        if disposition == "reuse":
            _require(eligible[key], "selected reply is not eligible for reuse")
        else:
            _require(len(text.encode()) <= bound, "whole counterpart exceeds request-byte bound")
            same = {ref for ref, original in texts.items() if original == text}
            _require(not changed or not same, "current input already completed; select its origin")
            _require(
                not any(eligible[ref] for ref in same),
                "an eligible same-input completion requires reuse",
            )
        planned.append(
            OriginPlannedUnit(
                SourceContext(current.path, current.english, start, end),
                disposition,
                origin,
                ids,
                separator,
            )
        )
        at = end
    _require(at == len(current.english), "selected requests do not cover current source")
    return tuple(planned)


def reconcile_completed_history(
    current: PreparedSource,
    partitions: tuple[RequestPartition, ...],
    completions: tuple[CompletionOrigin, ...],
    selected_reply_refs: tuple[str, ...],
    *,
    history_manifest_sha256: str,
    eligible_reuse: Mapping[str, bool],
    faults: tuple[UnitFault, ...] = (),
    pair_refused: bool = False,
    bounded_new_request_bytes: int = chunks.MAX_BYTES,
    source_origins: Mapping[str, SourceOrigins] | None = None,
) -> HistoryReconciliation:
    """Plan a complete current cover from whole actual completion origins.

    The caller authenticates catalog completeness, reply-specific faults, eligibility,
    raw Git sources, uncertainty, partial attempts, refusals and security/financial
    policy. A retained slot creates no new completion origin. Each variant must have
    its own strong opaque reply reference even when request keys coincide.
    Only exact-input correction may replay that origin's assistant; changed input
    uses the current USER only. This result is never execution or publication approval.
    """
    chunks.validate_request_bound(bounded_new_request_bytes)
    evidence = {} if source_origins is None else source_origins
    eligible_items = tuple(eligible_reuse.items()) if isinstance(eligible_reuse, Mapping) else ()
    origin_items = tuple(evidence.items()) if isinstance(evidence, Mapping) else ()
    header = (
        current,
        partitions,
        completions,
        eligible_items,
        origin_items,
        history_manifest_sha256,
        faults,
    )
    try:
        _require(not pair_refused, "locale/source pair is refused across all history")
        _require(type(current) is PreparedSource, "actual current PreparedSource required")
        _source_cuts(current, current.path)
        by_partition = _partition_catalog(partitions, current)
        catalog, texts = _completion_catalog(completions, by_partition, eligible_reuse)
        expected = completion_history_sha256(partitions, completions, eligible_reuse)
        _require(expected == history_manifest_sha256, "complete history metadata digest differs")
        fault_ids = _faults(faults, catalog, eligible_reuse)
        projections = _origin_projections(evidence, by_partition, current)
        units = _selected_units(
            current,
            selected_reply_refs,
            catalog,
            texts,
            by_partition,
            projections,
            fault_ids,
            eligible_reuse,
            bounded_new_request_bytes,
        )
    except ValueError as error:
        conflict = ReconciliationConflict("refusal" if pair_refused else "history", (), str(error))
        return HistoryReconciliation(*header, conflicts=(conflict,))
    return HistoryReconciliation(*header, units=units)
