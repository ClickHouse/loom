"""code.search's retrieval confidence. Pure.

The confidence answers one question: does the index hold an answer at all? The vector
leg's best cosine says so, read on the embedder's own scale: an absolute cosine means
nothing across models (one scores unrelated text 0.2, another 0.7), so the confidence
runs from the middle of the range between the calibrated abstain threshold and the
calibrated cosine of a confident hit (0) to that cosine (1). A question about another
repository or another subject usually scores lower than one the index can answer. It
is a tendency, not a rule: measured on issue prompts with one embedding model, about
one prompt in five sent to another repository's index still read low risk.

A scale whose confident cosine is not above its abstain threshold has no such range:
the confidence then reads the middle of the scale, as it does with no cosine.

It does not say whether the first result is the right one: among questions the index
can answer, a hit and a miss score alike. It does not decide abstention.
"""

from __future__ import annotations

# The share of the range between the abstain threshold and the confident cosine below
# which the confidence is 0.
RAMP_FROM = 0.5
# No cosine to read (no vector leg, or one that returned no row) or no scale to read it
# on: the middle of the scale, a medium risk at the default bands, never low.
NO_COSINE_CONFIDENCE = 0.5


def has_range(*, abstain_threshold: float, confident_similarity: float) -> bool:
    """Whether the two calibrated cosines leave a range to read a cosine on.

    They leave none when the calibration's question-to-answer cosines fall into its
    distractor range, or when the scalar abstain threshold is set at or over the scalar
    confident cosine. Every cosine a served result can have is then at or over both, so
    it says nothing about the index."""
    return confident_similarity > abstain_threshold


def similarity_term(
    best_cosine: float, *, abstain_threshold: float, confident_similarity: float
) -> float:
    """Where `best_cosine` sits between the two calibrated cosines, 0 to 1;
    `NO_COSINE_CONFIDENCE` when they leave no range (`has_range`)."""
    if not has_range(
        abstain_threshold=abstain_threshold, confident_similarity=confident_similarity
    ):
        return NO_COSINE_CONFIDENCE
    position = (best_cosine - abstain_threshold) / (confident_similarity - abstain_threshold)
    return max(0.0, min((position - RAMP_FROM) / (1.0 - RAMP_FROM), 1.0))


def risk_band(confidence: float, *, medium_from: float, low_from: float) -> str:
    """`retrieval_risk` of a result the search does not abstain on: high, medium or low."""
    if confidence < medium_from:
        return "high"
    return "medium" if confidence < low_from else "low"
