"""The synthesis output budget follows the evidence."""

from __future__ import annotations

from server.memory.app.ask.synthesis import synth_token_budget


def test_window_rule_is_the_floor() -> None:
    assert synth_token_budget(window=15, evidence_tokens=200, ratio=0.5) == 600
    assert synth_token_budget(window=25, evidence_tokens=200, ratio=0.5) == 900


def test_budget_grows_with_the_evidence() -> None:
    # a 25-row window with ~4k evidence tokens at ratio 0.5 -> ~2k tokens
    assert synth_token_budget(window=25, evidence_tokens=4000, ratio=0.5) == 2000


def test_ceiling_bounds_the_adaptive_budget() -> None:
    assert synth_token_budget(window=25, evidence_tokens=100_000, ratio=0.5, ceiling=8000) == 8000
    # a ceiling below the floor cannot starve the answer
    assert synth_token_budget(window=25, evidence_tokens=100_000, ratio=0.5, ceiling=100) == 900


def test_ratio_zero_is_the_old_rule_and_fixed_wins() -> None:
    assert synth_token_budget(window=25, evidence_tokens=4000, ratio=0.0) == 900
    assert synth_token_budget(window=25, evidence_tokens=4000, ratio=0.5, fixed=1234) == 1234
