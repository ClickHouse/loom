"""Declined call sites as ranked candidates, and dispatch through overrides: the
`include_ambiguous` widening of code.callers / code.callees.

The fast-tier resolver declines a call it cannot pin to one definition and records the site
in `loom.code_call_sites_unresolved`, keyed by the bare callee name. `code.callers` reports
the count; this module
returns the sites, as a block apart from `edges` that is never merged into
it: each site is a place that MAY call the seed, with the reason the resolver
declined it and a confidence from the size of its candidate set.

Two reads, both keyed: the callee-name key for callers (the seed's bare
names), the `by_path` projection for callees (the seed's own file, then its
line span). Filters are applied in Python over the bounded rows: the site's
language must be the seed's (a Python `.find(` cannot call a C++ method; on
the ClickHouse index every one of the 125 sites named `find` was Python), a
site written `Scope::name` must name the seed's scope, and a site `edges`
already holds at the same path:line is not a candidate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.code.api.models_symbol import (
    CodeCandidateGroupWire,
    CodeCandidateSitesWire,
    CodeCandidateSiteWire,
)
from server.code.domain.extract.treesitter import lang_for_path

if TYPE_CHECKING:
    from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)

REASON_OVERLOAD = "ambiguous_overload"
REASON_VIRTUAL = "virtual_dispatch"
REASON_UNRESOLVED = "unresolved_name"

# keyed-read row cap for the callee-name read: a hub name (`parse`, `get`)
# has thousands of declined sites; past this the block says `read_capped`
CANDIDATE_READ_CAP = 2000
# overrides of a base seed walked for their callers; more is disclosed
OVERRIDE_FANOUT_CAP = 50
UNRESOLVED_CONFIDENCE = 0.5
CONFIDENCE_FLOOR = 0.1


def confidence_of(disposition: str, candidates: int) -> float:
    """1/|candidates| floored at 0.1 for an ambiguous site; a flat 0.5 for an
    unresolved name — the table had no definition of the name when the file
    was indexed, so the seed being the one definition now is a fact about
    the table at that time, not evidence about the call."""
    if disposition == "unresolved":
        return UNRESOLVED_CONFIDENCE
    return round(max(CONFIDENCE_FLOOR, 1.0 / max(1, int(candidates))), 3)


def reason_of(disposition: str, *, virtual: bool) -> str:
    if disposition == "unresolved":
        return REASON_UNRESOLVED
    return REASON_VIRTUAL if virtual else REASON_OVERLOAD


def _seed_langs(seeds: list[dict[str, Any]]) -> set[str]:
    return {lang_for_path(str(s.get("path") or "")) for s in seeds if s.get("path")}


def _seed_qualified(seeds: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(s.get("qualified_name") or "") for s in seeds))


def _written_scope_matches(call_text: str, qualified: list[str]) -> bool:
    """`Foo::find(...)` may call `DB::Foo::find` and not `DB::Bar::find` —
    the same rule the resolver's rule 3 applies (candidates_for_call)."""
    if "::" not in call_text:
        return True
    return any(q == call_text or q.endswith("::" + call_text) for q in qualified)


def bare_names(seeds: list[dict[str, Any]]) -> list[str]:
    out = list(
        dict.fromkeys(
            str(s.get("name") or str(s.get("qualified_name") or "").split("::")[-1]) for s in seeds
        )
    )
    return [b for b in out if b]


def _site(row: dict[str, Any], *, reason: str, caller: str, callee_text: str) -> dict[str, Any]:
    disposition = str(row.get("disposition") or "")
    return {
        "path": str(row.get("path") or ""),
        "line": int(row.get("line") or 0),
        "caller": caller,
        "callee_text": callee_text,
        "reason": reason,
        "confidence": confidence_of(disposition, int(row.get("candidates") or 0)),
        "disposition": disposition,
        "candidates": int(row.get("candidates") or 0),
    }


def _group(sites: list[dict[str, Any]], key_of: Any) -> list[CodeCandidateGroupWire]:
    """Groups ordered by best confidence, then size, then key; sites within a
    group by confidence then path:line."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in sites:
        groups.setdefault(key_of(s), []).append(s)
    out: list[CodeCandidateGroupWire] = []
    for (kind, key), members in groups.items():
        members.sort(key=lambda s: (-s["confidence"], s["path"], s["line"]))
        out.append(
            CodeCandidateGroupWire(
                kind=kind,
                key=key,
                n=len(members),
                confidence=max(s["confidence"] for s in members),
                sites=[CodeCandidateSiteWire(**m) for m in members],
            )
        )
    out.sort(key=lambda g: (-g.confidence, -g.n, g.key))
    return out


def _block(
    sites: list[dict[str, Any]],
    *,
    max_nodes: int,
    read_capped: bool,
    key_of: Any,
    note: str,
    min_confidence: float = 0.0,
) -> CodeCandidateSitesWire:
    # The default answer keeps the tier a hand-check rated 87% precise; the rest are
    # counted, not returned.
    if min_confidence > 0.0:
        below = [x for x in sites if x["confidence"] < min_confidence]
        if below:
            sites = [x for x in sites if x["confidence"] >= min_confidence]
            note = (f"{note}; " if note else "") + (
                f"{len(below)} candidate site(s) below the confidence floor {min_confidence:g} "
                "not returned (pass candidate_min_confidence=0 to see them)"
            )
    sites.sort(key=lambda s: (-s["confidence"], s["path"], s["line"]))
    total = len(sites)
    kept = sites[:max_nodes]
    by_reason: dict[str, int] = {}
    for s in sites:
        by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
    parts = [note] if note else []
    if total > len(kept):
        parts.append(
            f"{total} candidate site(s) passed the filters, {len(kept)} returned (max_nodes)"
        )
    if read_capped:
        parts.append(f"the keyed read stopped at {CANDIDATE_READ_CAP} rows; `total` is a floor")
    return CodeCandidateSitesWire(
        epistemic="lower_bound",
        total=total,
        returned=len(kept),
        truncated=total > len(kept),
        read_capped=read_capped,
        by_reason=dict(sorted(by_reason.items())),
        groups=_group(kept, key_of),
        note="; ".join(parts),
    )


async def caller_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    edges: list[Any],
    *,
    max_nodes: int,
    virtual: bool,
    min_confidence: float = 0.0,
) -> CodeCandidateSitesWire:
    """Declined sites named like a seed that MAY call it. One keyed read on
    the callee-name key; language, written-scope and already-an-edge
    filters in Python. Fails open to an empty block with a note."""
    names = bare_names(seeds)
    try:
        rows = await repo.live_call_misses_for_names(org, ns, names, limit=CANDIDATE_READ_CAP)
    except Exception as e:  # noqa: BLE001 — a disclosure never fails the answer
        logger.debug("candidate-site read skipped: %s", e)
        return _block(
            [],
            max_nodes=max_nodes,
            read_capped=False,
            key_of=_caller_key,
            note="candidate read failed; none listed",
        )
    langs = _seed_langs(seeds)
    qualified = _seed_qualified(seeds)
    edge_sites = {(str(e.path), int(e.line)) for e in edges}
    dropped_lang = dropped_scope = dropped_edge = 0
    sites: list[dict[str, Any]] = []
    for r in rows:
        path = str(r.get("path") or "")
        if langs and lang_for_path(path) not in langs:
            dropped_lang += 1
            continue
        call_text = str(r.get("call_text") or "")
        if not _written_scope_matches(call_text, qualified):
            dropped_scope += 1
            continue
        if (path, int(r.get("line") or 0)) in edge_sites:
            dropped_edge += 1
            continue
        sites.append(
            _site(
                r,
                reason=reason_of(str(r.get("disposition") or ""), virtual=virtual),
                caller=str(r.get("from_qualified_name") or ""),
                callee_text=call_text,
            )
        )
    note = (
        f"{len(rows)} declined site(s) named like the seed; dropped {dropped_lang} in another "
        f"language, {dropped_scope} written with another scope, {dropped_edge} already in edges"
    )
    return _block(
        sites,
        max_nodes=max_nodes,
        read_capped=len(rows) >= CANDIDATE_READ_CAP,
        key_of=_caller_key,
        note=note,
        min_confidence=min_confidence,
    )


def _caller_key(s: dict[str, Any]) -> tuple[str, str]:
    text = str(s.get("callee_text") or "")
    if "::" in text:
        return ("receiver_scope", text.rsplit("::", 1)[0])
    return ("file", str(s["path"]))


def _callee_key(s: dict[str, Any]) -> tuple[str, str]:
    return ("callee", str(s.get("callee_text") or "").split("::")[-1])


async def callee_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    *,
    max_nodes: int,
    min_confidence: float = 0.0,
) -> CodeCandidateSitesWire:
    """The seed's own declined call sites: what it MAY call. Read per seed
    path through the by_path projection, kept to the seed's line span (or
    its symbol id where the span is unknown)."""
    sites: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    paths = list(dict.fromkeys(str(s.get("path") or "") for s in seeds if s.get("path")))[:3]
    failed = False
    for path in paths:
        try:
            rows = await repo.live_call_misses_for_path(org, ns, path)
        except Exception as e:  # noqa: BLE001
            logger.debug("candidate-site read skipped: %s", e)
            failed = True
            continue
        for r in rows:
            if not _inside_seed(r, seeds, path):
                continue
            key = (path, int(r.get("line") or 0), str(r.get("callee_name") or ""))
            if key in seen:
                continue
            seen.add(key)
            call_text = str(r.get("call_text") or "") or str(r.get("callee_name") or "")
            sites.append(
                _site(
                    r,
                    reason=reason_of(str(r.get("disposition") or ""), virtual=False),
                    caller=str(r.get("from_qualified_name") or ""),
                    callee_text=call_text,
                )
            )
    note = f"{len(sites)} declined call site(s) inside the seed"
    if failed:
        note += "; a per-path read failed, the list is partial"
    return _block(
        sites,
        max_nodes=max_nodes,
        read_capped=False,
        key_of=_callee_key,
        note=note,
        min_confidence=min_confidence,
    )


def _inside_seed(row: dict[str, Any], seeds: list[dict[str, Any]], path: str) -> bool:
    line = int(row.get("line") or 0)
    from_id = str(row.get("from_symbol_id") or "")
    from_q = str(row.get("from_qualified_name") or "")
    for s in seeds:
        if str(s.get("path") or "") != path:
            continue
        if from_id and from_id == str(s.get("symbol_id") or ""):
            return True
        if from_q and from_q == str(s.get("qualified_name") or ""):
            return True
        start, end = int(s.get("start_line") or 0), int(s.get("end_line") or 0)
        if start and end and start <= line <= end:
            return True
    return False


async def overrides_of(
    repo: CodeRepo,
    org: str,
    ns: str,
    seed_ids: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """(overrides of these seeds from the compiler tier, capped) — the
    `overrides` edge points from the override to the base, so the base's
    overrides are its callers along that kind. Fail-open to none."""
    if not seed_ids:
        return [], False
    try:
        rows = await repo.edges_for_symbols(
            org,
            ns,
            seed_ids,
            direction="callers",
            edge_kinds=["overrides"],
            limit=OVERRIDE_FANOUT_CAP + 1,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("overrides read skipped: %s", e)
        return [], False
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        sid = str(r["from_symbol_id"])
        if sid in seen or sid in seed_ids:
            continue
        seen.add(sid)
        out.append(
            {
                "symbol_id": sid,
                "qualified_name": str(r.get("from_qualified_name") or ""),
                "path": str(r.get("path") or ""),
                "via": "overridden_by",
            }
        )
    capped = len(out) > OVERRIDE_FANOUT_CAP
    return out[:OVERRIDE_FANOUT_CAP], capped
