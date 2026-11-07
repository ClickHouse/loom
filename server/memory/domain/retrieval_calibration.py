"""Route-aware retrieval confidence scoring.

Static similarity thresholds are useful gates, but they do not explain how
well independent retrieval routes agree. This module computes a compact
risk/coverage signal from the final hit set plus route metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalCalibration:
    confidence: float
    risk: str
    route_agreement: float
    explanation: str


def calibrate_retrieval(
    *,
    top_similarity: float,
    second_similarity: float = 0.0,
    result_count: int,
    graph_seeds: int = 0,
    graph_neighbors: int = 0,
    triples_extracted: int = 0,
    triples_matched: int = 0,
    evidence_support_count: int = 0,
    abstain_threshold: float = 0.30,
    # Additional independent retrieval routes beyond the built-in graph/
    # triple/evidence slots — the code plane counts its lexical and
    # fuzzy-symbol legs here. Votes are capped to possible.
    extra_route_votes: int = 0,
    extra_routes_possible: int = 0,
    # A pool narrowed by a caller-supplied predicate (code.search `pattern`
    # / path filters) is small by construction: the count
    # term saturates and the explanation says so, so a three-chunk pool
    # that matches the pattern is not read as thin retrieval.
    constrained: bool = False,
    # The confidence below which the result is flagged
    # `abstain`. 0.35 is the memory plane's cut-off; the code plane passes
    # its own setting.
    confidence_abstain_below: float = 0.35,
    # The confidences from which the risk reads "medium" and "low". These are
    # the memory plane's; the code plane passes its own, because code
    # embeddings score lower cosines for an equally right hit.
    medium_risk_from: float = 0.55,
    low_risk_from: float = 0.75,
) -> RetrievalCalibration:
    """Return calibrated confidence and risk label for a search result set."""

    if result_count <= 0:
        return RetrievalCalibration(
            confidence=0.0,
            risk="abstain",
            route_agreement=0.0,
            explanation=(
                "no chunk matched the constraint (pattern / path filters)"
                if constrained
                else "no results survived retrieval filters"
            ),
        )

    margin = max(0.0, top_similarity - second_similarity)
    route_votes = 1  # semantic/primary route exists because we have a top hit
    route_possible = 1
    if graph_seeds:
        route_possible += 1
        if graph_neighbors:
            route_votes += 1
    if triples_extracted:
        route_possible += 1
        if triples_matched:
            route_votes += 1
    if evidence_support_count:
        route_possible += 1
        route_votes += 1
    if extra_routes_possible > 0:
        route_possible += extra_routes_possible
        route_votes += min(extra_route_votes, extra_routes_possible)

    route_agreement = route_votes / max(route_possible, 1)
    score = (
        0.55 * max(0.0, min(top_similarity, 1.0))
        + 0.20 * max(0.0, min(margin * 2.0, 1.0))
        + 0.20 * route_agreement
        + 0.05 * (1.0 if constrained else min(result_count / 5.0, 1.0))
    )
    confidence = max(0.0, min(score, 1.0))
    if top_similarity < abstain_threshold or confidence < confidence_abstain_below:
        risk = "abstain"
    elif confidence < medium_risk_from:
        risk = "high"
    elif confidence < low_risk_from:
        risk = "medium"
    else:
        risk = "low"
    explanation = (
        f"top_similarity={top_similarity:.2f}, margin={margin:.2f}, "
        f"route_agreement={route_agreement:.2f}, evidence_support={evidence_support_count}"
        + (", constrained=true" if constrained else "")
    )
    return RetrievalCalibration(
        confidence=round(confidence, 3),
        risk=risk,
        route_agreement=round(route_agreement, 3),
        explanation=explanation,
    )


# High-precision salient-identifier patterns. Deliberately narrow: only forms
# that name a *specific* entity (PR/issue numbers, bare test ids, @handles), so
# a query naming one the corpus lacks is caught without minting false abstains
# on ordinary prose. The digit-bearing patterns capture the bare number so the
# grounding check matches "#113594", "PR 113594", and "113594" alike.
_IDENTIFIER_NUM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"#(\d{3,})"),
    re.compile(r"\bPR[ #]?(\d{3,})\b", re.IGNORECASE),
    re.compile(r"\bissue[ #]?(\d{3,})\b", re.IGNORECASE),
    # 5-6 digit bare numbers only: ClickHouse PR/issue/test ids are 5-6 digits,
    # so this catches "113594"/"03005" while NOT matching 4-digit years (2026)
    # that would otherwise trigger false abstains. Prefixed forms (#/PR/issue)
    # above stay 3+ since the prefix already disambiguates.
    re.compile(r"\b(\d{5,6})\b"),
)
# Capture the BARE handle after an ``@``, tolerating a space between the ``@``
# and the name. Space-broken refs ("@ octocat") are a deliberate convention in
# some corpora (breaking the ref so it doesn't fire a GitHub notification); a
# glued-only pattern would extract no identifier from them, leaving the person
# invisible to injection + the grounding/abstain gate. Emitting the bare handle
# also makes the downstream substring match land on every mention form
# ("@octocat", "@ octocat", "octocat").
_IDENTIFIER_HANDLE_PATTERN = re.compile(r"@\s*([A-Za-z0-9][A-Za-z0-9_-]{1,})")

# Test-name-shaped tokens: the keys of test_failure records are strings, not
# numbers or @handles, so _salient_identifiers never surfaces them. These patterns catch
# the common CI test-name shapes (pytest path::name, test_-prefixed names,
# numbered SQL tests, gtest Suite.Case). Precision is NOT carried by the
# extraction — a token only answers if it EXACTLY equals a stored test_name and
# matches exactly one record — so the patterns can be liberal.
# pytest path::name with optional [param] — must carry ".py::" to be test-specific;
# group 1, read from a path's first word only, so a long path costs one pass
_PYTEST_ID_RE = re.compile(r"(?<![\w./-])[./-]*+\b([\w./-]+\.py::[\w./-]+(?:\[[^\]\s]+\])?)")
_TEST_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PYTEST_ID_RE,
    # test_-prefixed name with optional [param]
    re.compile(r"\btest_[A-Za-z0-9_]{3,}(?:\[[^\]\s]+\])?"),
    # numbered SQL test, e.g. 05047_distributed_plan_finished_stages_drain
    re.compile(r"\b\d{4,6}_[a-z][a-z0-9_]{5,}\b"),
    # gtest Suite.Case (both parts capitalized), e.g. Executor.RemoveTasks
    re.compile(r"\b[A-Z][A-Za-z0-9]+\.[A-Z][A-Za-z0-9]+\b"),
)


def salient_test_identifiers(query_text: str) -> list[str]:
    """Extract test-name-shaped tokens from a query, in order of first
    appearance, de-duplicated case-insensitively. The tracker route reads
    them; the numeric/handle grounding leg does not."""
    seen: set[str] = set()
    out: list[str] = []
    for pat in _TEST_IDENTIFIER_PATTERNS:
        for m in pat.finditer(query_text):
            ident = m.group(1) if pat is _PYTEST_ID_RE else m.group(0)
            if ident.lower() not in seen:
                seen.add(ident.lower())
                out.append(ident)
    return out


def _salient_identifiers(query_text: str) -> list[str]:
    """Extract high-precision salient identifiers from a query, in order of
    first appearance, de-duplicated case-insensitively. Numbers are returned as
    the bare digit run; ``@``-handles are returned WITH a normalized leading
    ``@`` (tolerating a space after it in the query, e.g. ``@ octocat``);
    consumers that want the bare form ``lstrip("@")`` at the point of use."""
    seen: set[str] = set()
    out: list[str] = []
    for pat in _IDENTIFIER_NUM_PATTERNS:
        for m in pat.finditer(query_text):
            ident = m.group(1)
            if ident.lower() not in seen:
                seen.add(ident.lower())
                out.append(ident)
    for m in _IDENTIFIER_HANDLE_PATTERN.finditer(query_text):
        ident = "@" + m.group(1)
        if ident.lower() not in seen:
            seen.add(ident.lower())
            out.append(ident)
    return out


def identifier_grounding_applies(query_text: str, max_tokens: int) -> bool:
    """The grounding leg is for a *question that names an
    identifier* ("fix status of PR 113594"). A document-shaped query — a PR
    body, a tokenised path list of 50–200 tokens — carries PR and test numbers
    that are not its subject, and abstaining because none of them appears in
    a near-verbatim match (cosine 0.84–0.96) is a false absence.
    `max_tokens <= 0` always applies."""
    if max_tokens <= 0:
        return True
    return len((query_text or "").split()) <= max_tokens


def identifier_grounding_missing(query_text: str, hit_texts: list[str]) -> str | None:
    """Detect the "on-topic but factually wrong" recall mode.

    On a domain-homogeneous corpus, a query naming a specific identifier the
    corpus does NOT contain (e.g. "fix status of PR 113594" when only #117593
    exists) still draws a same-domain neighbour at cosine 0.5-0.62, so the
    rank-0 cosine abstain gate never fires and search serves a confidently
    wrong answer. This is a pure, conservative grounding check.

    Returns a short reason code ``"entity_absent:<id>"`` when the query carries
    >=1 salient identifier and NONE of them appear (case-insensitive substring)
    in ANY hit text. Returns None when the query has no such identifier (fall
    back to existing behaviour) or when at least one identifier is grounded in a
    hit — a single grounded identifier is enough to keep the result.
    """
    idents = _salient_identifiers(query_text)
    if not idents:
        return None
    lowered_hits = [t.lower() for t in hit_texts]
    for ident in idents:
        # Match on the bare run so a "@octocat" ident lands on bare/space-broken
        # mentions too; else the hedge over-abstains on entities that ARE present.
        needle = ident.lstrip("@").lower()
        if any(needle in h for h in lowered_hits):
            return None
    return f"entity_absent:{idents[0].lstrip('@')}"


def finalize_abstain(
    *,
    top_similarity: float | None,
    calibration: RetrievalCalibration | object,
    abstain_threshold: float,
    no_match_reason: str = "nothing matched the query above the noise floor",
    grounding_reason: str | None = None,
) -> tuple[bool, str, str]:
    """Shared abstain envelope for any retrieval plane.

    Precedence is load-bearing (see memory/app/search/verdict._finalize_abstain):
    empty set -> no_match; weak top hit -> below_threshold (carrying the
    actual threshold an operator would tune); only then the calibrator's
    verdict -> low_calibrated_confidence. Wire fields named abstain/
    abstain_code/abstain_reason — metrics and Langfuse emitters key off
    these exact names.

    `top_similarity` is None when there are no hits.

    `grounding_reason`, when set (an "entity_absent:<id>" code from
    `identifier_grounding_missing`), forces abstain even for a top hit ABOVE
    the cosine threshold — that is the whole point of the leg: it catches an
    on-topic same-domain neighbour that names none of the identifiers the query
    asked about. It ranks below the empty/weak-hit gates (those already abstain
    for a stronger reason) and above the calibrator's verdict.
    """
    if top_similarity is None:
        return (True, "no_match", no_match_reason)
    if top_similarity < abstain_threshold:
        return (
            True,
            "below_threshold",
            f"top hit similarity {top_similarity:.2f} is below the abstain "
            f"threshold {abstain_threshold:.2f} — results returned but "
            f"agent should hedge or refuse to answer from these",
        )
    if grounding_reason is not None:
        return (True, "entity_absent", grounding_reason)
    if getattr(calibration, "risk", "") == "abstain":
        return (
            True,
            "low_calibrated_confidence",
            "calibrated retrieval confidence is too low for a grounded answer: "
            + str(getattr(calibration, "explanation", "")),
        )
    return False, "", ""
