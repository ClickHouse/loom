"""A set-shaped identifier question reads the whole set."""

from __future__ import annotations

from server.memory.app.ask.identifiers import _injection_limits


def test_directive_and_set_questions_get_the_set_limits() -> None:
    assert _injection_limits(
        "What are @SamRivera's known review expectations and directives?", 8, 4, 24
    ) == (24, 8)
    assert _injection_limits(
        "How should the GitHub inbox cron handle a new human directive?", 8, 4, 24
    ) == (24, 8)
    assert _injection_limits("Which flaky tests do we track for PR 114055?", 8, 4, 24) == (24, 8)


def test_single_fact_questions_keep_the_defaults_and_zero_disables() -> None:
    assert _injection_limits("Was PR 116990 merged, and when?", 8, 4, 24) == (8, 4)
    assert _injection_limits("What are @SamRivera's review expectations?", 8, 4, 0) == (8, 4)
