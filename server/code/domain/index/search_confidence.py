"""code.search's retrieval confidence. Pure.

Two questions decide whether the first results hold the code the question is about.

Does the index hold an answer at all? The vector leg's best cosine says so, read on
the embedder's own scale: an absolute cosine means nothing across models (one scores
unrelated text 0.2, another 0.7), so the term runs from the middle of the range
between the calibrated abstain threshold and the calibrated cosine of a confident hit
(0) to that cosine (1). A question about another repository stays under it.

Is the best unit the right one? The lexical leg says so: it scores the question's
rare words, which the embedding blurs. When its rows hold the vector leg's first unit
both legs found the same code; when they hold only another unit of that file, the
file; when neither, the nearest unit shares no rare word with the question.

confidence = similarity x (UNCONFIRMED + (CONFIRMED - UNCONFIRMED) x agreement), an
estimate of the chance that the first three results hold a file the answer is in.
"""

from __future__ import annotations

from typing import Any

# The share of the range between the abstain threshold and the confident cosine below
# which the similarity term is 0.
RAMP_FROM = 0.5
# The confidence of a fully similar hit the lexical leg does not confirm, and of one it
# returns itself: the hit rates measured for the two cases.
UNCONFIRMED = 0.4
CONFIRMED = 0.9
# One leg did not run: neither a confirmation nor its absence.
AGREEMENT_UNKNOWN = 0.5
# No cosine to read (no vector leg): a medium risk, never an abstain and never low.
NO_COSINE_CONFIDENCE = 0.45


def similarity_term(
    top_cosine: float, *, abstain_threshold: float, confident_similarity: float
) -> float:
    """Where `top_cosine` sits between the two calibrated cosines, 0 to 1."""
    floor = abstain_threshold + RAMP_FROM * (confident_similarity - abstain_threshold)
    span = confident_similarity - floor
    if span <= 0.0:
        return 1.0 if top_cosine >= confident_similarity else 0.0
    return max(0.0, min((top_cosine - floor) / span, 1.0))


def leg_agreement(vector_rows: list[dict[str, Any]], lexical_rows: list[dict[str, Any]]) -> float:
    """How far the lexical leg's rows confirm the vector leg's first row: 1.0 the same
    unit, 0.5 another unit of its file, 0.0 neither. Both lists are a leg's own rows in
    its own order; an empty vector list confirms nothing."""
    if not vector_rows:
        return 0.0
    top = vector_rows[0]
    unit, path = str(top["chunk_id"]), top["path"]
    same_file = False
    for row in lexical_rows:
        if str(row["chunk_id"]) == unit:
            return 1.0
        same_file = same_file or row["path"] == path
    return 0.5 if same_file else 0.0


def confidence(similarity: float, agreement: float) -> float:
    """The calibrated confidence of a result whose best cosine reads `similarity` on
    the embedder's scale and whose legs agree by `agreement`."""
    return similarity * (UNCONFIRMED + (CONFIRMED - UNCONFIRMED) * agreement)


def risk_band(
    confidence: float, *, abstain_below: float, medium_from: float, low_from: float
) -> str:
    """`retrieval_risk` for a confidence: abstain, high, medium or low."""
    if confidence < abstain_below:
        return "abstain"
    if confidence < medium_from:
        return "high"
    return "medium" if confidence < low_from else "low"
