"""Unit tests for tracker/domain/llm_reply.py helpers.

Pins clip (strip, cut with ellipsis, None → "") and json_object
(prose/code-fence wrapping, None on missing/invalid/two objects).
"""

from __future__ import annotations

from server.tracker.domain.llm_reply import clip, json_object


def test_clip_returns_stripped_input_within_limit() -> None:
    assert clip("  hello world  ", 20) == "hello world"
    assert clip("exact", 5) == "exact"
    assert clip("", 10) == ""


def test_clip_cuts_to_limit_ending_with_ellipsis() -> None:
    # Longer than limit: t[:limit-1].rstrip() + "…"
    long = "abcdefghijk"  # 11 chars
    result = clip(long, 10)
    assert result == "abcdefghi…"
    assert len(result) == 10
    assert result.endswith("…")

    # Exactly at limit is unchanged (after strip)
    assert clip("abcdefghij", 10) == "abcdefghij"


def test_clip_maps_none_and_falsy_to_empty() -> None:
    # text or "" treats None, 0, False, "" as empty after strip
    assert clip(None, 10) == ""
    assert clip(0, 10) == ""
    assert clip(False, 5) == ""
    assert clip("", 5) == ""


def test_json_object_reads_json_wrapped_in_prose() -> None:
    text = 'Here is the answer: {"verdict": "related", "score": 0.9} done.'
    result = json_object(text)
    assert result == {"verdict": "related", "score": 0.9}


def test_json_object_reads_json_in_code_fence() -> None:
    text = """Sure.
```json
{"label": "Duplicate", "number": 42}
```
"""
    result = json_object(text)
    assert result == {"label": "Duplicate", "number": 42}


def test_json_object_returns_none_for_no_braces() -> None:
    assert json_object("no json here") is None
    assert json_object("") is None
    assert json_object(None) is None


def test_json_object_returns_none_for_invalid_json() -> None:
    assert json_object("{invalid: json}") is None
    assert json_object("{unclosed") is None


def test_json_object_returns_none_for_two_separate_objects() -> None:
    # Greedy \{.*\} spans both objects; the match is invalid JSON → None.
    # Documents today's behaviour.
    text = '{"a": 1} some prose {"b": 2}'
    assert json_object(text) is None
