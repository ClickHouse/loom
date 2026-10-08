"""Which voting legs a question gets (card, concept, hypothetical answer), the
awaitables the leg phase runs them as, and what they decide after
calibration: the off_domain gate on the nearest card and the concept terms in
the explanation."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from server.code.app.search.cards import _card_and_concept_rows, card_term_files
from server.code.app.search.hyde import _hyde_config, _hyde_leg_rows
from server.code.app.search.state import CodeSearchResult
from server.code.app.search_legs import llm_legs_off
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.settings import settings


def _voting_leg_config(
    query: str,
    *,
    prose: bool,
    run_lexical: bool,
    work: dict[str, Any],
    llm_legs: str | None = None,
) -> tuple[bool, float, str, float]:
    """Which voting legs a prose question gets: the card leg and the concept leg's model or
    `cards[:n]` source run only for the kinds of question they were measured to help
    (responsibility questions, not incident titles); the off_domain gate is not gated; and
    the lexical list's fusion weight for a responsibility question. Returns (use_cards,
    card_gate, concept_model, lexical_weight). `llm_legs="none"` drops a model-named
    concept source; the card-derived one (`cards:N`) calls no model and is kept."""
    from server.code.domain.index.question_kind import kind_allowed, question_kind

    kind = question_kind(query) if prose else "other"
    work["question_kind"] = kind
    use_cards = (
        bool(settings.code_search_card_leg)
        and prose
        and kind_allowed(kind, settings.code_search_card_leg_kinds)
    )
    card_gate = float(settings.code_search_abstain_card_below) if prose else 0.0
    concept_model = (
        str(settings.code_search_concept_expand_model).strip()
        if prose and run_lexical and kind_allowed(kind, settings.code_search_concept_expand_kinds)
        else ""
    )
    if concept_model and card_term_files(concept_model) == 0 and llm_legs_off(llm_legs):
        concept_model = ""
    lex_weight = (
        float(settings.code_search_rrf_lexical_weight_responsibility)
        if kind == "responsibility"
        else 1.0
    )
    return use_cards, card_gate, concept_model, lex_weight


def _voting_leg_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query: str,
    query_vec: Any,
    use_cards: bool,
    card_gate: float,
    concept_model: str,
    path_prefix: str,
    scope: SearchScope | None,
    work: dict[str, Any],
    card_stats: dict[str, Any],
    concept_stats: dict[str, Any],
    embedder: Any = None,
    llm_legs: str | None = None,
) -> tuple[
    Awaitable[tuple[list[dict[str, Any]], list[dict[str, Any]]]], Awaitable[list[dict[str, Any]]]
]:
    """The card leg with the concept leg, and the hypothetical answer's leg, as two awaitables
    the leg phase runs beside the others. Two, not one, so a deadline or the model-leg grace
    can drop HyDE (a model call, a provider embed and a full scan) and keep the cards;
    `llm_legs="none"` does not start it."""
    hyde_model = (
        ""
        if llm_legs_off(llm_legs)
        else _hyde_config(
            work,
            prose=(embedder is not None and scope is None),
        )
    )
    hyde = _hyde_leg_rows(
        repo,
        org,
        ns,
        model,
        query=query,
        embedder=embedder,
        path_prefix=path_prefix,
        scope=scope,
        work=work,
        hyde_model=hyde_model,
    )
    cards = _card_and_concept_rows(
        repo,
        org,
        ns,
        model,
        query=query,
        query_vec=query_vec,
        use_cards=use_cards,
        card_gate=card_gate,
        concept_model=concept_model,
        path_prefix=path_prefix,
        scope=scope,
        work=work,
        card_stats=card_stats,
        concept_stats=concept_stats,
    )
    return cards, hyde


def _apply_voting_leg_outcomes(
    result: CodeSearchResult,
    card_stats: dict[str, Any],
    concept_stats: dict[str, Any],
    *,
    card_gate: float,
) -> None:
    """After every other abstain rule: the off_domain gate on the nearest card
    (`card_gate` 0 = off) and the concept terms in the explanation. A card leg the
    deadline dropped leaves no gate (its half-read similarity is not a verdict), and the
    calibration says which legs the answer went without."""
    if "card" in result.legs_timed_out:
        card_stats["top_similarity"], concept_stats["terms"] = None, []
    if result.legs_timed_out:
        result.calibration_explanation += (
            f"; ranked without {', '.join(result.legs_timed_out)} (past the deadline)"
        )
    sim = card_stats.get("top_similarity")
    if not result.abstain and card_gate > 0.0 and sim is not None and sim < card_gate:
        # no file of this repository is about the question
        result.abstain = True
        result.abstain_code = "off_domain"
        result.abstain_reason = (
            f"no file card in this namespace is close to the question (best "
            f"{sim:.2f} < {card_gate:.2f}): the repository does "
            "not seem to cover it — check the namespace, or ask about what this code does"
        )
        result.retrieval_risk = "abstain"
    if concept_stats.get("terms"):
        extra = "concept=" + "|".join(concept_stats["terms"][:6])
        result.score_explanation_extra = (
            (result.score_explanation_extra + ", " + extra)
            if result.score_explanation_extra
            else extra
        )
