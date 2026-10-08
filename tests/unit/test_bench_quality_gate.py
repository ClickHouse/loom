"""The recall gate in bench/quality.py.

Running that bench needs a live control plane, a real embedder and a real
extraction LLM, so the gate cannot be exercised in CI without a paid provider
secret. Extracting the decision into a pure function makes it testable anyway —
an unexercised gate is a claim rather than a fact.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))

from quality import gate  # noqa: E402


def test_passes_when_recall_meets_the_bar() -> None:
    code, msg = gate(recalled=9, total=10, min_recall=0.9)
    assert code == 0 and msg == ""


def test_fails_when_recall_is_below_the_bar() -> None:
    code, msg = gate(recalled=8, total=10, min_recall=0.9)
    assert code == 1
    assert "0.800" in msg and "0.9" in msg


def test_default_threshold_is_report_only() -> None:
    """`--min-recall` defaults to 0: report only.

    Callers (Makefile targets, local runs) do not fail on recall unless they ask
    to; enforcement is opt-in per invocation.
    """
    code, _ = gate(recalled=0, total=10, min_recall=0.0)
    assert code == 0


def test_zero_questions_fails_rather_than_passing_vacuously() -> None:
    """0/0 must not read as "met the bar".

    A dataset that produced no questions has measured nothing. Returning success
    there is how a broken harness passes a release gate silently — the same class
    of bug as recall computed over a run where everything errored.
    """
    code, msg = gate(recalled=0, total=0, min_recall=0.9)
    assert code == 1
    assert "no measurement" in msg

    # And it fails even with the threshold disabled: nothing measured is nothing
    # measured, regardless of how lenient the bar is.
    code2, _ = gate(recalled=0, total=0, min_recall=0.0)
    assert code2 == 1


def test_exact_boundary_passes() -> None:
    """recall == min_recall is a pass, not a fail.

    Off-by-one here would make a gate set to the recorded baseline fail on the
    baseline itself, which teaches people to disable it.
    """
    code, _ = gate(recalled=5, total=10, min_recall=0.5)
    assert code == 0
