"""One fence stripper for JSON-shaped completions: Anthropic's JSON mode and
the memory parsers read completions through it."""

from __future__ import annotations

import json

import pytest

from server.llm.json_text import strip_code_fence

OBJ = '{"a": 1}'


@pytest.mark.parametrize(
    "text",
    [
        OBJ,
        f"  {OBJ}\n",
        f"```json\n{OBJ}\n```",
        f"```\n{OBJ}\n```",
        f"```json{OBJ}```",
        f"```json {OBJ} ```",
        f"\n```json\n{OBJ}\n```\n",
        f"{OBJ}\n```",
    ],
)
def test_fenced_and_bare_json_parse_the_same(text: str) -> None:
    assert strip_code_fence(text) == OBJ
    assert json.loads(strip_code_fence(text)) == {"a": 1}


def test_edge_inputs() -> None:
    assert strip_code_fence("```") == ""
    assert strip_code_fence("") == ""
    assert strip_code_fence(None) == ""  # type: ignore[arg-type]
    # A backtick inside the payload survives.
    assert strip_code_fence('```json\n{"a": "`x`"}\n```') == '{"a": "`x`"}'
