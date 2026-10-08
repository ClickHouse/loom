"""What a search carries: the `CodeSearchResult` it returns, the request state
that flows between its phases, and the pool sizes every leg shares."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from server.code.app import history as code_history
from server.code.domain import deadline as code_deadline
from server.code.domain.search_scope import SearchScope
from server.code.domain.units import CodeUnit

_POOL = 200


@dataclass
class CodeSearchResult:
    units: list[CodeUnit] = dc_field(default_factory=list)
    route: str = "hybrid"
    # History section: item facts + up to N merged-PR rows for a question about a
    # change, with the files it anchored. Empty unless the history intent fired.
    history: dict[str, Any] = dc_field(default_factory=dict)
    abstain: bool = False
    abstain_code: str = ""
    abstain_reason: str = ""
    retrieval_risk: str = "low"
    retrieval_confidence: float = 0.0
    route_agreement: float = 0.0
    calibration_explanation: str = ""
    tokens_returned: int = 0
    score_explanation: str = ""
    score_explanation_extra: str = ""
    retrieval_work: dict[str, int] = dc_field(default_factory=dict)
    # Raw cosine of the top vector-leg hit; 0.0 when the vector leg did not
    # run (symbol_exact route, no embedder, leg failure).
    top_similarity: float = 0.0
    # The fused, ranked, diversified list the served page was cut from
    # (hybrid routes only; up to _POOL_KEEP units, bodies included, not
    # budgeted). The continuation cursor stores its ids so the next page is
    # a point read, not a re-search (code/app/search_cursor.py).
    pool: list[CodeUnit] = dc_field(default_factory=list)
    # Constrained search: a pattern / path filter narrowed both legs' pools;
    # matched_files is the pre-count the cap was read on.
    constrained: bool = False
    matched_files: int = 0
    # Which legs this call ran: "hybrid" | "lexical" | "vector".
    legs: str = "hybrid"
    # track_path diagnostic block (empty unless requested).
    tracked: dict[str, Any] = dc_field(default_factory=dict)
    # The legs a deadline dropped (their rows are absent from the fusion). `epistemic` is
    # "partial" then or when a content leg failed; "" when every leg that ran answered.
    legs_timed_out: list[str] = dc_field(default_factory=list)
    epistemic: str = ""
    # The request's wall-clock budget (None = unbounded); what it cut makes the answer
    # partial.
    budget: code_deadline.Budget | None = None

    @property
    def partial(self) -> bool:
        return self.budget is not None and self.budget.partial

    @property
    def partial_reason(self) -> str:
        return self.budget.reason() if self.budget is not None else ""


# Ranked units kept for continuation past the served page.
_POOL_KEEP = 100


@dataclass(slots=True)
class _SearchState:
    """Request-scoped state that flows between search()'s phases once the
    routes before the legs are done: what the legs share, what they
    returned, and what the ranking phases add for the explanation. Only
    fields two or more phases read live here."""

    result: CodeSearchResult
    work: dict[str, int]
    model: str
    scope: SearchScope | None
    constrained: bool
    shape: str
    # the request's leg ablation
    legs_mode: str
    run_lexical: bool
    run_vector: bool
    # set by _gather_legs before any leg runs
    tokens: list[str] = dc_field(default_factory=list)
    use_history: bool = False
    history_cap: float = 0.0
    history_stats: dict[str, Any] = dc_field(default_factory=dict)
    intent: code_history.HistoryIntent | None = None
    # written by the history route, even when a deadline drops its units
    history_section: dict[str, Any] = dc_field(default_factory=dict)
    card_gate: float = 0.0
    card_stats: dict[str, Any] = dc_field(default_factory=dict)
    concept_stats: dict[str, Any] = dc_field(default_factory=dict)
    lex_weight: float = 1.0
    # what the legs returned (their empty values when a deadline dropped one)
    lexical_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    lex_tokens: list[str] = dc_field(default_factory=list)
    idfs: list[float] = dc_field(default_factory=list)
    vector_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    qvec: list[float] | None = None
    history_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    history_units: list[CodeUnit] = dc_field(default_factory=list)
    fuzzy_hit: bool = False
    card_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    concept_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    hyde_rows: list[dict[str, Any]] = dc_field(default_factory=list)
    # the ranking phases' parts of score_explanation
    fan_in_explain: str = ""
    fusion_signals: bool = False
    kind_explain: str = ""
    tiebreak_applied: bool = False
    saturation_explain: str = ""


def _model_name(embedder: Any) -> str:
    return getattr(embedder, "model_name", "") or ""
