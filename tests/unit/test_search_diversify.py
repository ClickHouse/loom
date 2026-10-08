"""The per-session cap is the request's; the default cap follows top_k;
aggregational shapes take none."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.app.search.assemble import _diversify_session_cap


def test_default_is_off_and_request_can_turn_it_on() -> None:
    inp = SimpleNamespace(diversify="", diversify_session_cap=None, top_k=10)
    assert _diversify_session_cap(inp, "lookup") == 0
    inp.diversify = "session"
    assert _diversify_session_cap(inp, "lookup") == 3  # max(2, 10 // 3)
    inp.top_k = 4
    assert _diversify_session_cap(inp, "lookup") == 2
    inp.diversify_session_cap = 5
    assert _diversify_session_cap(inp, "lookup") == 5


def test_request_none_is_off() -> None:
    inp = SimpleNamespace(diversify="none", diversify_session_cap=4, top_k=12)
    assert _diversify_session_cap(inp, "lookup") == 0


def test_aggregational_shapes_take_no_cap() -> None:
    inp = SimpleNamespace(diversify="session", diversify_session_cap=3, top_k=10)
    assert _diversify_session_cap(inp, "aggregational") == 0
    assert _diversify_session_cap(inp, "temporal_diff") == 0


def test_input_model_accepts_the_field() -> None:
    from server.memory.api.models_search import MemorySearchInput

    base = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "query": "q",
    }
    assert MemorySearchInput(**base).diversify == ""
    assert (
        MemorySearchInput(
            **base, diversify="session", diversify_session_cap=2
        ).diversify_session_cap
        == 2
    )
