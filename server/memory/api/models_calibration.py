"""Read-only inspection of the threshold values in effect for a namespace.

Resolution chain: per-namespace override → cluster-wide singleton → scalar
settings. The `source` field reports which tier produced the response.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalibrationGetInput(BaseModel):
    org: str
    namespace: str


class CalibrationGetOutput(BaseModel):
    source: str  # "namespace" / "cluster" / "scalar"
    # The embedder the thresholds were derived for; with `scalar`, the configured model.
    embedder_model: str = ""

    # Cosine below which a search hit is dropped as noise (distractor P90 + 0.02 when probed).
    noise_floor: float
    # Top-hit cosine at or above which the LLM rerank is skipped (question-to-fact P75,
    # never below `hyde_fallback_threshold`).
    rerank_skip_above: float
    # The question-to-fact P25 gate, never below `query_rewrite_threshold`; `loom_search`
    # reads it as its precision floor.
    hyde_fallback_threshold: float
    # The question-to-fact P10 gate, held at least 0.01 above `noise_floor`.
    query_rewrite_threshold: float
    # Top-hit cosine below which a search abstains (`noise_floor` + 0.05 when probed).
    abstain_threshold: float
    # Cosine at or above which a write against an active peer is flagged `conflicted`
    # (paraphrase P50 when probed).
    conflict_threshold: float
    # Cosine at or above which a write with a canonical-form mismatch is marked `duplicate`
    # (paraphrase P75 when probed).
    duplicate_threshold: float

    # Probe percentiles; default 0.0 when source is 'scalar' (no probe ran).
    similar_p10: float = Field(default=0.0)
    # 25th percentile cosine of the paraphrase pairs.
    similar_p25: float = Field(default=0.0)
    # Median cosine of the paraphrase pairs.
    similar_p50: float = Field(default=0.0)
    # 75th percentile cosine of the paraphrase pairs.
    similar_p75: float = Field(default=0.0)
    # Median cosine of the unrelated (distractor) pairs.
    distractor_p50: float = Field(default=0.0)
    # 90th percentile cosine of the distractor pairs, the anchor of `noise_floor`.
    distractor_p90: float = Field(default=0.0)

    # Paraphrase pairs the probe embedded; 0 when `source` is `scalar`.
    similar_pairs_n: int = Field(default=0)
    # Distractor pairs the probe embedded; 0 when `source` is `scalar`.
    distractor_pairs_n: int = Field(default=0)
