"""Symbols the retrieved window names are code-grounded too."""

from __future__ import annotations

from types import SimpleNamespace as N

from server.memory.app.code_grounding import _hop_symbols

Q = "What do we know about the test_zookeeper_fallback_session flaky test and PR 114055?"


def test_hop_symbols_come_from_the_window_not_the_question() -> None:
    pool = [
        N(
            content_excerpt="The throw is in KeeperStateMachine::preprocess at KeeperStateMachine.cpp:1920"
        ),
        N(
            content_excerpt="SESSION_REFUSED came from ZooKeeperImpl; test_zookeeper_fallback_session ran 40x"
        ),
    ]
    syms = _hop_symbols(Q, pool, 4)
    assert syms[:2] == ["KeeperStateMachine::preprocess", "KeeperStateMachine"]
    assert "ZooKeeperImpl" in syms
    # the question's own identifier is not a hop
    assert not any(s.lower() == "test_zookeeper_fallback_session" for s in syms)


def test_hop_symbols_are_symbol_shaped_capped_and_off_at_zero() -> None:
    pool = [
        N(content_excerpt="lower_snake_only and plain words; FooBar BazQux QuxQuux Corge::Grault")
    ]
    assert _hop_symbols("what happened?", pool, 2) == ["FooBar", "BazQux"]
    assert _hop_symbols("what happened?", pool, 0) == []
    assert _hop_symbols("what happened?", [], 4) == []
