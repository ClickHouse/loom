"""--stale-check: a WRONG knowledge-update answer is asked one more question —
is it the SUPERSEDED value (an answer that recalls the past state)?"""

from __future__ import annotations

import pytest

from bench.longmemeval import _stale_answer_check, _stale_answer_prompt


def test_prompt_quotes_the_earlier_session_and_asks_yes_no() -> None:
    p = _stale_answer_prompt("What is my PB?", "25:50", "user: my PB is 27:10", "27:10")
    assert "Earlier conversation (superseded):\nuser: my PB is 27:10" in p
    assert "Correct current answer: 25:50" in p
    assert "Assistant response: 27:10" in p
    assert p.rstrip().endswith("Answer yes or no only.")


@pytest.mark.asyncio
async def test_check_is_unmeasured_when_the_earlier_session_is_not_in_the_haystack() -> None:
    item = {
        "question": "q",
        "answer": "a",
        "answer_session_ids": ["s_old", "s_new"],
        "haystack_session_ids": ["other", "s_new"],  # the superseded session was dropped
        "haystack_sessions": [[], []],
    }
    # No HTTP call is made: the function returns before building a request.
    assert await _stale_answer_check(None, item, "old value", "m", "k") == ""  # type: ignore[arg-type]
    assert (
        await _stale_answer_check(None, {**item, "answer_session_ids": ["only"]}, "x", "m", "k")
        == ""
    )  # type: ignore[arg-type]
