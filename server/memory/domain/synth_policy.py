"""Routing policy for memory.ask synthesis: which model tier answers.

A pure function over signals the ask pipeline has already computed (no
network, no I/O), so the decision is cheap, deterministic, testable, and
replayable offline over frozen retrievals (bench/route_replay.py).

The shape is a cascade router: answer with the cheap model when the pre-generation
signals say the question is easy, escalate to the strong model when they
say it is hard OR when the cheap answer fails a verifier. Here the
verifier is loom's own citation contract: an answer that cites no grounding
memory, hedges, or comes back empty did not do the job.

Why heuristics and not a learned router: loom has not yet assembled the
labelled routing dataset (model per event + judge outcome). This module is
the seam a learned policy replaces; keep its inputs the features a kNN or
GNN router would consume.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

CHEAP = "cheap"
STRONG = "strong"
# A third tier between the two. The strong tier's spend is
# 63% asks routed there for RETRIEVAL-uncertainty reasons (weak_top_hit,
# low_confidence, retrieval_risk_high, per the ask ledger), not
# because the synthesis is hard; the mid tier is the arm that measures whether
# a cheaper strong-class reader holds on exactly those routes.
MID = "mid"
MID_SUFFIX = "mid"
# The routes the mid tier takes under `mid_routes="uncertain"`: the three
# reasons whose answers the judge contradicts most often and
# whose windows are the uncertain half of the strong tier's bill.
MID_ROUTES_UNCERTAIN: frozenset[str] = frozenset(
    {"retrieval_risk_high", "low_confidence", "weak_top_hit"}
)

# The consistency judge's verdicts are the one per-answer wrongness signal loom records at
# scale, and they group by the router's own reason token. The contradicted rate per route,
# as the ask ledger records it (loom.agent_events, args.out_route_reason x out_consistency,
# synthesized asks only). It is a PRIOR on
# how often the window the route hands the synthesizer is wrong: a route the verdict rarely
# contradicts is one whose answer is a restatement of a right window — the shape the cheap
# tier handles; a route it often contradicts is one where the window is wrong and a stronger
# reader is the only defence. `easy_question` is the cheap tier's own floor; `single_tier`
# is the label of asks with no cheap tier configured (every one a bench namespace on an
# isolated server) and has nothing to be routed to.
MEASURED_ROUTE_PRIOR: Mapping[str, float] = {
    "retrieval_risk_high": 0.385,
    "low_confidence": 0.318,
    "weak_top_hit": 0.194,
    "breadth_query": 0.154,
    "single_tier": 0.073,  # no cheap tier configured
    "easy_question": 0.053,  # the cheap tier itself
}

# Routes the prior may never turn cheap: each is a hard stop measured on
# judged outcomes, not on the verdict rate (identifier questions lose more often on
# the cheap tier than the strong one at a low contradicted rate; rich evidence and
# breadth likewise;
# an ungrounded identifier or an as_of anchor is a discipline question, not a
# restatement).
PRIOR_HARD_STOPS: frozenset[str] = frozenset(
    {
        "budget_pressure",
        "identifier_ungrounded",
        "identifier_question",
        "rich_evidence",
        "as_of_anchor",
        "breadth_query",
        "composed_question",
        "retrieval_risk_abstain",
        "retrieval_risk_unknown",
    }
)
PRIOR_CHEAP_SUFFIX = "prior_cheap"


def parse_route_prior(spec: str | Mapping[str, float] | None) -> Mapping[str, float] | None:
    """The prior a setting or a request names: '' / 'off' = none, 'measured' =
    MEASURED_ROUTE_PRIOR, else a JSON object of route -> contradicted rate.
    A malformed spec is no prior (the router stays rule-only)."""
    if spec is None:
        return None
    if isinstance(spec, Mapping):
        return {str(k): float(v) for k, v in spec.items()}
    text = str(spec or "").strip()
    if not text or text.lower() == "off":
        return None
    if text.lower() == "measured":
        return MEASURED_ROUTE_PRIOR
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out or None


def parse_mid_routes(spec: str | None) -> frozenset[str] | None:
    """The routes a setting or a request hands to the mid tier: '' / 'off' =
    none (no mid routing), 'uncertain' = MID_ROUTES_UNCERTAIN, else a comma
    list of reason tokens. Hard stops are dropped from a custom list — they
    never move whichever tier is asking."""
    text = str(spec or "").strip()
    if not text or text.lower() == "off":
        return None
    if text.lower() == "uncertain":
        return MID_ROUTES_UNCERTAIN
    routes = frozenset(
        t.strip() for t in text.split(",") if t.strip() and t.strip() not in PRIOR_HARD_STOPS
    )
    return routes or None


# Breadth questions ask the synthesizer to ENUMERATE a set; a weak model
# generalizes across items instead of listing each (still-vague losses 2x on
# a production workload). Route them strong. Deliberately NOT
# matched: "what do we know about X" (one entity, not a set — 25 of 60
# sampled real questions took that form) and a bare "all"/"list" inside a
# noun phrase ("for all new failures", "task_list").
_BREADTH_RE = re.compile(
    r"(?:^\s*(?:which|list|enumerate|summar(?:y|ize|ise)|compare)\b"
    r"|\bhow many\b|\bevery(?:thing|one)?\b|\beach of\b"
    r"|\ball (?:the|of|my|our|your)\b|\boverview of\b"
    r"|\bwhat are all\b|\bwhich (?:of|ones)\b)",
    re.IGNORECASE,
)

# Composed questions chain relations: "the country of citizenship of the
# spouse of the author of X", "the capital of the country where the sport
# associated with Y originated". Each link is a hop the synthesizer must
# resolve against the right (latest) row before the next, so the question is
# hard however confident the top hit looks. A hop is a "the <noun> of" phrase
# or a relative link ("where", "in which", "with which", "associated with",
# "that ... is"); two or more make the question composed.
_HOP_RE = re.compile(
    r"\bthe (?:\w+ ){0,2}?\w+ of\b"
    r"|\b(?:where|in which|with which|of which|to which|from which)\b"
    r"|\b(?:associated|affiliated|connected) with\b",
    re.IGNORECASE,
)


def is_composed_query(query: str) -> bool:
    return len(_HOP_RE.findall(query or "")) >= 2


# Cheap-answer failure signatures: the model told us it could not answer.
_HEDGE_RE = re.compile(
    r"\b(no (?:memory|memories|information|record)s? (?:about|of|on|specifically)|"
    r"(?:don't|do not|cannot|can't) (?:know|find|tell|determine)|"
    r"not (?:enough|sufficient) (?:information|context)|"
    r"(?:no|nothing) (?:relevant|stored)|unclear from the memories)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SynthFeatures:
    """Everything the router looks at. All fields have safe defaults so a
    caller that lacks a signal degrades to the conservative (strong) path."""

    query_chars: int = 0
    n_memories: int = 0
    # Continuous calibration score from the search leg (0.55*top + 0.2*margin
    # + 0.2*agreement + 0.05). The banded `retrieval_risk` is derived from it
    # with "low" at >= 0.75, which real questions almost never reach (4 of 60
    # sampled: medians top 0.72, confidence 0.65) — so the router gates on the
    # continuous value and treats the band only as a hard stop for "high".
    retrieval_confidence: float = 0.0
    # "low" | "medium" | "high" | "abstain" — MemorySearchOutput.retrieval_risk
    retrieval_risk: str = "abstain"
    route_agreement: float = 0.0
    # Top hit cosine and its lead over the second hit. A clear leader means
    # the answer is one memory the cheap model just has to restate.
    top_similarity: float = 0.0
    similarity_margin: float = 0.0
    # A salient identifier in the query is NOT cited by the grounding pool.
    hedged: bool = False
    # The query names a salient identifier (number, @handle, symbol): the
    # synthesis is a technical brief, not a restatement.
    has_identifier: bool = False
    # Evidence tokens in the synthesis window (chars/4). Rich evidence needs
    # the strong tier whatever the retrieval signals say.
    evidence_tokens: int = 0
    # Caller/temporal anchor: as_of asks need careful date discipline.
    as_of: bool = False
    # 24h spend / cap for the namespace; 0 when no cap is configured.
    budget_ratio: float = 0.0
    query: str = ""


@dataclass(frozen=True)
class SynthDecision:
    tier: str  # CHEAP | MID | STRONG
    reason: str

    @property
    def cheap(self) -> bool:
        return self.tier == CHEAP

    @property
    def mid(self) -> bool:
        return self.tier == MID


def is_breadth_query(query: str) -> bool:
    return bool(_BREADTH_RE.search(query or ""))


# A question that asks for a SET a person has accumulated over
# many sessions — "what books has Dana read", "which places have Omar and
# Ravi planned to meet at", "who has written notes to Lena", "what desserts
# has Lena made". The items live one per session; a 25-row window built from
# cosine to the question holds some of them. The shape is a wh-word, an
# optional plural head, a perfect/past auxiliary and a subject.
_SET_RE = re.compile(
    r"^\s*(?:what|which|who)\b"
    r"(?:\s+(?:kinds?|types?|sorts?)\s+of)?"
    r"(?:\s+[a-z][a-z\-']*){0,3}?"
    r"\s+(?:has|have|had|did|do|does)\b"
    r"(?:\s+\w+){0,6}?"
    r"\s+(?:been|done|read|seen|watched|visited|made|met|helped|planned|attended|"
    r"tried|bought|cooked|baked|played|written|mentioned|shared|taken|gone|received|"
    r"given|learned|learnt|joined|started|used|worked|lived|traveled|travelled|"
    r"participated|volunteered|adopted|recommended|suggested|discussed)\b",
    re.IGNORECASE,
)
_SET_PLURAL_RE = re.compile(
    r"^\s*(?:what|which|who)\s+(?:are|were)\s+(?:the|all|some)\b"
    r"|^\s*(?:what|which)\s+[a-z\-']+s\s+(?:has|have|had|did|do|does)\b",
    re.IGNORECASE,
)


_ORDER_SET_RE = re.compile(
    r"(?:\b(?:what(?:'s| is| was)|in what|in which)\s+(?:the\s+)?(?:chronological\s+)?order\b"
    r"|\bin (?:chronological|the) order\b|\bchronological order\b"
    r"|\bfrom (?:the )?(?:earliest|first|oldest|most recent|latest) to\b"
    r"|\b(?:starting|beginning) (?:from|with) the (?:earliest|first|most recent|latest)\b"
    r"|\bsequence of\b|\brank(?:ed)? (?:them|these|the \w+) (?:by|in)\b)",
    re.IGNORECASE,
)


def is_set_question(query: str) -> bool:
    """Whether the question asks for every item of a kind ("what books has
    she read"), as opposed to one fact. Breadth words count too, and so does
    an ordering ("what is the order of the six museums I visited"): an order
    is the whole set with its dates, and a set read from the cosine's top
    rows alone misses the member whose row does not share the question's
    words (the 5K run among "sports events" in a chat-history question)."""
    q = query or ""
    return bool(
        _SET_RE.search(q)
        or _SET_PLURAL_RE.search(q)
        or _BREADTH_RE.search(q)
        or _ORDER_SET_RE.search(q)
    )


def decide(
    f: SynthFeatures,
    *,
    budget_cheap_ratio: float = 0.7,
    min_cheap_confidence: float = 0.6,
    min_cheap_top_similarity: float = 0.70,
    max_cheap_query_chars: int = 160,
    min_cheap_agreement: float = 0.5,
    max_cheap_memories: int = 0,
    max_cheap_evidence_tokens: int = 0,
    route_prior: Mapping[str, float] | None = None,
    max_cheap_prior: float = 0.0,
    mid_routes: frozenset[str] | None = None,
) -> SynthDecision:
    """Pick the synthesis tier from pre-generation signals.

    Order matters: budget pressure wins (a near-capped namespace would rather
    have a cheap answer than a retrieval_only dump); then the hard stops;
    then the easy-question gate on the continuous calibration signals;
    everything else is strong. `reason` is a short stable token that rides
    the wire output and the event stream, so the replay bench can group
    outcomes by rule. `max_cheap_memories` is off (0) by default: the pool the
    synthesizer sees is the configured window, not a difficulty signal.

    Defaults come from bench/route_replay.py over reference-backed questions;
    re-fit them there before changing them.

    `route_prior` is the measured contradicted rate per
    reason token. When the rules land on the strong tier for a reason that is
    NOT a hard stop and the prior says that route's answers are contradicted
    no more often than `max_cheap_prior`, the ask is handed to the cheap tier
    with the reason `<rule>:prior_cheap`; the citation verifier still
    escalates a failed cheap answer. A route the prior does not know keeps
    the rule's tier. No prior (the default) is the rule-only router.

    `mid_routes` names the strong-tier reasons the MID tier
    answers instead: an ask the rules (and the prior) leave on the strong
    tier for one of those reasons is handed to the mid tier with the reason
    `<rule>:mid`; the same verifier escalates a failed mid answer to the
    strong tier. Hard stops never move. None (the default) = no mid tier.
    """
    d = _decide_rules(
        f,
        budget_cheap_ratio=budget_cheap_ratio,
        min_cheap_confidence=min_cheap_confidence,
        min_cheap_top_similarity=min_cheap_top_similarity,
        max_cheap_query_chars=max_cheap_query_chars,
        min_cheap_agreement=min_cheap_agreement,
        max_cheap_memories=max_cheap_memories,
        max_cheap_evidence_tokens=max_cheap_evidence_tokens,
    )
    if (
        route_prior
        and max_cheap_prior > 0
        and d.tier == STRONG
        and d.reason not in PRIOR_HARD_STOPS
    ):
        rate = route_prior.get(d.reason)
        if rate is not None and float(rate) <= max_cheap_prior:
            return SynthDecision(CHEAP, f"{d.reason}:{PRIOR_CHEAP_SUFFIX}")
    if (
        mid_routes
        and d.tier == STRONG
        and d.reason in mid_routes
        and d.reason not in PRIOR_HARD_STOPS
    ):
        return SynthDecision(MID, f"{d.reason}:{MID_SUFFIX}")
    return d


def _decide_rules(
    f: SynthFeatures,
    *,
    budget_cheap_ratio: float,
    min_cheap_confidence: float,
    min_cheap_top_similarity: float,
    max_cheap_query_chars: int,
    min_cheap_agreement: float,
    max_cheap_memories: int,
    max_cheap_evidence_tokens: int,
) -> SynthDecision:
    """The rule ladder, without the prior."""
    if budget_cheap_ratio > 0 and f.budget_ratio >= budget_cheap_ratio:
        return SynthDecision(CHEAP, "budget_pressure")
    if f.hedged:
        return SynthDecision(STRONG, "identifier_ungrounded")
    if f.has_identifier:
        return SynthDecision(STRONG, "identifier_question")
    if max_cheap_evidence_tokens and f.evidence_tokens > max_cheap_evidence_tokens:
        return SynthDecision(STRONG, "rich_evidence")
    if f.as_of:
        return SynthDecision(STRONG, "as_of_anchor")
    if is_breadth_query(f.query):
        return SynthDecision(STRONG, "breadth_query")
    if is_composed_query(f.query):
        return SynthDecision(STRONG, "composed_question")
    if f.retrieval_risk in ("high", "abstain", ""):
        return SynthDecision(STRONG, f"retrieval_risk_{f.retrieval_risk or 'unknown'}")
    if f.retrieval_confidence < min_cheap_confidence:
        return SynthDecision(STRONG, "low_confidence")
    if f.top_similarity < min_cheap_top_similarity:
        return SynthDecision(STRONG, "weak_top_hit")
    if f.route_agreement < min_cheap_agreement:
        return SynthDecision(STRONG, "route_disagreement")
    if max_cheap_memories and f.n_memories > max_cheap_memories:
        return SynthDecision(STRONG, "wide_pool")
    if f.query_chars > max_cheap_query_chars:
        return SynthDecision(STRONG, "long_query")
    return SynthDecision(CHEAP, "easy_question")


def cheap_answer_failed(answer: str, cited_keys: list[str], *, tier: str = CHEAP) -> str:
    """Return a non-empty escalation reason when a lower tier's answer does
    not satisfy the citation contract, else ''. This is the cascade's
    verifier: it costs nothing and catches the failure modes a weak model
    actually shows on this workload (silence, hedging, no grounding). `tier`
    prefixes the token (`cheap_uncited`, `mid_uncited`) so the ledger says
    which tier failed."""
    text = (answer or "").strip()
    if not text:
        return f"{tier}_empty"
    if not cited_keys:
        return f"{tier}_uncited"
    if _HEDGE_RE.search(text):
        return f"{tier}_hedged"
    return ""
