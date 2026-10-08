"""Memory.ask carries the body mode onto its retrieval leg."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from server.memory.api.models_ask import MemoryAskInput
from server.memory.app.ask.handler import _body_kwargs


def test_ask_input_accepts_the_body_knobs_and_defaults_to_none() -> None:
    a = MemoryAskInput(org="o", namespace="n", agent="ag", query="q")
    assert a.bodies is None and a.body_budget_tokens is None and a.source_span_chars == 0
    b = MemoryAskInput(
        org="o",
        namespace="n",
        agent="ag",
        query="q",
        bodies="source",
        body_budget_tokens=8000,
        source_span_chars=600,
    )
    assert (b.bodies, b.body_budget_tokens, b.source_span_chars) == ("source", 8000, 600)
    with pytest.raises(ValidationError):
        MemoryAskInput(org="o", namespace="n", agent="ag", query="q", bodies="whole")


def test_body_kwargs_only_carry_what_was_set() -> None:
    # An unset `bodies` pins "full" on the leg — the reader
    # never synthesizes from the excerpt tier memory.search serves an unpinned
    # caller above memory_search_excerpt_bodies_k. The other two knobs still
    # ride only when set.
    assert _body_kwargs(
        SimpleNamespace(bodies=None, body_budget_tokens=None, source_span_chars=0)
    ) == {"bodies": "full"}
    assert _body_kwargs(
        SimpleNamespace(bodies="source", body_budget_tokens=8000, source_span_chars=0)
    ) == {
        "bodies": "source",
        "body_budget_tokens": 8000,
    }
    assert _body_kwargs(
        SimpleNamespace(bodies="source", body_budget_tokens=None, source_span_chars=600)
    ) == {
        "bodies": "source",
        "source_span_chars": 600,
    }
