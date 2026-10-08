"""Shared failure-stage labels for Loom memory benchmarks."""

from __future__ import annotations

from typing import Literal

# Only the stages the two classifiers below actually emit. (The pipeline has
# more conceptual failure points, but until a classifier produces one it's
# just an unreachable label.)
FailureStage = Literal[
    "passed",
    "extraction",
    "graph_resolution",
    "retrieval_ranking",
    # Answer-level stages (classify_answer_failure): the gold text was in the
    # raw evidence behind a retrieved memory but in none of the excerpts the
    # reader saw (the extractor compressed it away), or it was in an excerpt
    # and the reader still answered wrong, or it is nowhere verbatim (a
    # computed or paraphrased answer the substring probe cannot see).
    "compression_loss",
    "reader",
    "not_verbatim",
]


def classify_extraction_failure(*, matched: bool, memory_count: int) -> FailureStage:
    if matched:
        return "passed"
    if memory_count == 0:
        return "extraction"
    return "retrieval_ranking"


def classify_graph_failure(*, matched: bool, observed_count: int) -> FailureStage:
    if matched:
        return "passed"
    if observed_count == 0:
        return "extraction"
    return "graph_resolution"


def classify_answer_failure(
    *,
    judged_correct: bool,
    recalled: bool,
    gold_in_excerpt: bool,
    gold_in_evidence: bool,
) -> FailureStage:
    """Stage of a judged QA miss, in the order the pipeline can lose it.

    `recalled`: a memory from a gold evidence session reached the top-k.
    `gold_in_excerpt`: the gold answer text is a substring of a retrieved
    excerpt. `gold_in_evidence`: it is a substring of the raw source turns
    linked to the retrieved memories (memory_belief_sources → memory_evidence).

    A miss splits into retrieval and over-compression; this computes that
    split per item from the store rather than estimating it.
    """
    if judged_correct:
        return "passed"
    if not recalled:
        return "retrieval_ranking"
    if gold_in_excerpt:
        return "reader"
    if gold_in_evidence:
        return "compression_loss"
    return "not_verbatim"
