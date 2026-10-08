"""Gate derivation for the embedder-calibration boot probe.

The read-time trigger gates (rerank_skip / hyde_fallback /
query_rewrite) must derive from the QUESTION→FACT cosine distribution,
not the paraphrase distribution: a retrieval top-hit is an
interrogative query against a declarative stored fact, which cosines
well below statement↔statement paraphrases on every real embedder.
Paraphrase-anchored gates sit above the whole realistic top-hit range
and never skip.
"""

from __future__ import annotations

import math
import random

import pytest

from server.memory.app import embedder_calibration as ec

# Synthetic probe distributions mirroring the measured shape on a real
# embedder: paraphrase pairs cosine high, question→fact pairs cosine in
# a clearly lower band, distractors near the floor.
SIMILAR = [0.69, 0.71, 0.73, 0.75, 0.77, 0.78, 0.80, 0.82, 0.84, 0.86]
QUESTION_FACT = [0.42, 0.46, 0.50, 0.55, 0.58, 0.60, 0.62, 0.64, 0.66, 0.70]
DISTRACTOR = [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.22, 0.24, 0.26]


def _derive() -> ec.CalibrationResult:
    return ec.derive_thresholds(
        SIMILAR, QUESTION_FACT, DISTRACTOR, embedder_model="stub-embedder-1"
    )


# ---- derivation from a synthetic probe result ----


def test_trigger_gates_derive_from_question_fact_distribution() -> None:
    r = _derive()
    assert r.rerank_skip_above == pytest.approx(ec._percentile(QUESTION_FACT, 75))
    assert r.hyde_fallback_threshold == pytest.approx(ec._percentile(QUESTION_FACT, 25))
    assert r.query_rewrite_threshold == pytest.approx(ec._percentile(QUESTION_FACT, 10))


def test_question_fact_gates_sit_between_distractor_ceiling_and_paraphrase_floor() -> None:
    r = _derive()
    paraphrase_floor = ec._percentile(SIMILAR, 10)
    for gate in (
        r.query_rewrite_threshold,
        r.hyde_fallback_threshold,
        r.rerank_skip_above,
    ):
        assert r.noise_floor < gate < paraphrase_floor


def test_gate_ladder_is_monotone() -> None:
    r = _derive()
    assert (
        r.noise_floor
        <= r.abstain_threshold
        <= r.query_rewrite_threshold
        <= r.hyde_fallback_threshold
        <= r.rerank_skip_above
        <= r.conflict_threshold
        <= r.duplicate_threshold
    )


def test_filter_and_write_gates_keep_their_anchors() -> None:
    # noise_floor/abstain stay distractor-anchored; conflict/duplicate
    # stay paraphrase-anchored (a racing write IS the statement-vs-
    # statement shape).
    r = _derive()
    assert r.noise_floor == pytest.approx(ec._percentile(DISTRACTOR, 90) + 0.02)
    assert r.abstain_threshold == pytest.approx(r.noise_floor + 0.05)
    assert r.conflict_threshold == pytest.approx(ec._percentile(SIMILAR, 50))
    assert r.duplicate_threshold == pytest.approx(ec._percentile(SIMILAR, 75))


def test_result_exposes_both_distributions() -> None:
    r = _derive()
    assert r.similar_pairs_n == len(SIMILAR)
    assert r.question_fact_pairs_n == len(QUESTION_FACT)
    assert r.distractor_pairs_n == len(DISTRACTOR)
    assert 0.0 < r.question_fact_p50 < r.similar_p50
    assert r.question_fact_p10 <= r.question_fact_p25 <= r.question_fact_p50 <= r.question_fact_p75


def test_degenerate_question_fact_distribution_clamps_to_noise_floor() -> None:
    # A question→fact distribution that collapses into the distractor
    # range (weak embedder) must not produce trigger gates below the
    # drop floor — a sub-floor trigger can never fire.
    qf_low = [0.08, 0.10, 0.12, 0.15, 0.18]
    r = ec.derive_thresholds(SIMILAR, qf_low, DISTRACTOR, embedder_model="stub-embedder-1")
    assert r.query_rewrite_threshold >= r.noise_floor
    assert r.query_rewrite_threshold <= r.hyde_fallback_threshold <= r.rerank_skip_above


def test_question_fact_corpus_shape() -> None:
    # Interrogative query on the left, declarative fact on the right.
    assert len(ec.QUESTION_FACT_PAIRS) >= 20
    for question, fact in ec.QUESTION_FACT_PAIRS:
        assert question.strip().endswith("?")
        assert not fact.strip().endswith("?")


# ---- probe plumbing (stub embedders; no network) ----


def _unit_vec(c: float) -> list[float]:
    # Unit vector at cosine `c` from the base axis [1, 0, 0].
    return [c, math.sqrt(max(0.0, 1.0 - c * c)), 0.0]


class _TableEmbedder:
    model_name = "stub-embedder-1"
    dim = 3

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table

    async def embed(self, text: str) -> list[float]:
        return self._table[text]


class _HashEmbedder:
    model_name = "stub-embedder-1"
    dim = 8

    async def embed(self, text: str) -> list[float]:
        rnd = random.Random(text)
        v = [rnd.uniform(-1.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


class _FakeNamedEmbedder:
    model_name = "fake-ngram-1024"
    dim = 1024

    async def embed(self, text: str) -> list[float]:  # pragma: no cover
        raise AssertionError("fake embedder must be skipped before any embed call")


async def test_probe_end_to_end_with_stub_embedder(monkeypatch) -> None:
    para_cosines = [0.72, 0.76, 0.80, 0.84, 0.88]
    qf_cosines = [0.45, 0.52, 0.58, 0.63, 0.68]
    dist_cosines = [0.05, 0.12, 0.20, 0.24]

    table: dict[str, list[float]] = {"q": [1.0, 0.0, 0.0]}
    para, qf, dist = [], [], []
    for i, c in enumerate(para_cosines):
        table[f"p{i}"] = _unit_vec(c)
        para.append(("q", f"p{i}"))
    for i, c in enumerate(qf_cosines):
        table[f"f{i}"] = _unit_vec(c)
        qf.append(("q", f"f{i}"))
    for i, c in enumerate(dist_cosines):
        table[f"d{i}"] = _unit_vec(c)
        dist.append(("q", f"d{i}"))

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _TableEmbedder(table))
    r = await ec.calibrate_embedder(
        paraphrase_pairs=para, distractor_pairs=dist, question_fact_pairs=qf
    )
    assert r is not None
    assert r.embedder_model == "stub-embedder-1"
    assert not r.is_fake
    assert r.rerank_skip_above == pytest.approx(ec._percentile(qf_cosines, 75), abs=1e-6)
    assert r.hyde_fallback_threshold == pytest.approx(ec._percentile(qf_cosines, 25), abs=1e-6)
    assert r.noise_floor < r.query_rewrite_threshold <= r.rerank_skip_above < r.similar_p10


async def test_per_ns_probe_defaults_to_module_question_fact_corpus(monkeypatch) -> None:
    # The per-NS corpus table carries only similar/distractor pair
    # types; the probe must fall back to the module question→fact
    # corpus rather than reverting gates to paraphrase anchors.
    monkeypatch.setattr("server.embedding.embedder.get_embedder", _HashEmbedder)
    r = await ec.calibrate_embedder(
        paraphrase_pairs=[("a", "b"), ("c", "d"), ("e", "f")],
        distractor_pairs=[("g", "h"), ("i", "j")],
    )
    assert r is not None
    assert r.question_fact_pairs_n == len(ec.QUESTION_FACT_PAIRS)


async def test_probe_failure_returns_none(monkeypatch) -> None:
    def _broken() -> None:
        raise RuntimeError("no embedder configured")

    monkeypatch.setattr("server.embedding.embedder.get_embedder", _broken)
    assert await ec.calibrate_embedder() is None


async def test_fake_embedder_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("server.embedding.embedder.get_embedder", _FakeNamedEmbedder)
    assert await ec.calibrate_embedder() is None


async def test_empty_question_fact_corpus_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("server.embedding.embedder.get_embedder", _HashEmbedder)
    r = await ec.calibrate_embedder(
        paraphrase_pairs=[("a", "b")],
        distractor_pairs=[("c", "d")],
        question_fact_pairs=[],
    )
    assert r is None


# ---- scalar fallback when no calibration is installed ----


def test_effective_helpers_fall_back_to_scalars_without_calibration() -> None:
    old = ec.get_calibration()
    try:
        ec.set_calibration(None)
        assert ec.effective_noise_floor(0.22) == 0.22
        assert ec.effective_hyde_fallback_threshold(0.45) == 0.45
        assert ec.effective_abstain_threshold(0.30) == 0.30
    finally:
        ec.set_calibration(old)


def test_effective_helpers_read_calibrated_gates_when_installed() -> None:
    old = ec.get_calibration()
    try:
        r = _derive()
        ec.set_calibration(r)
        assert ec.effective_hyde_fallback_threshold(0.45) == pytest.approx(
            r.hyde_fallback_threshold
        )
    finally:
        ec.set_calibration(old)
