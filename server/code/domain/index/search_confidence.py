"""code.search's retrieval confidence. Pure.

The confidence answers one question: does the index hold an answer at all? The vector
leg's best cosine says so, read on the embedder's own scale: an absolute cosine means
nothing across models (one scores unrelated text 0.2, another 0.7), so the confidence
runs from the middle of the range between the calibrated abstain threshold and the
calibrated cosine of a confident hit (0) to that cosine (1). A question about another
repository stays near 0.

It does not say whether the first result is the right one: among questions the index
can answer, a hit and a miss score alike.
"""

from __future__ import annotations

# The share of the range between the abstain threshold and the confident cosine below
# which the confidence is 0.
RAMP_FROM = 0.5
# No cosine to read (no vector leg): the middle of the scale, a medium risk at the
# default bands, never an abstain and never low.
NO_COSINE_CONFIDENCE = 0.5


def similarity_term(
    top_cosine: float, *, abstain_threshold: float, confident_similarity: float
) -> float:
    """Where `top_cosine` sits between the two calibrated cosines, 0 to 1."""
    floor = abstain_threshold + RAMP_FROM * (confident_similarity - abstain_threshold)
    span = confident_similarity - floor
    if span <= 0.0:
        return 1.0 if top_cosine >= confident_similarity else 0.0
    return max(0.0, min((top_cosine - floor) / span, 1.0))


def risk_band(
    confidence: float, *, abstain_below: float, medium_from: float, low_from: float
) -> str:
    """`retrieval_risk` for a confidence: abstain, high, medium or low."""
    if confidence < abstain_below:
        return "abstain"
    if confidence < medium_from:
        return "high"
    return "medium" if confidence < low_from else "low"
