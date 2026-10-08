"""The below-threshold gate may read the best cosine in the window."""

from __future__ import annotations

from server.memory.app.search.verdict import _gate_top_sim


class _Hit:
    def __init__(self, distance: float) -> None:
        self.distance = distance


def test_default_scope_reads_the_first_row(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "first", raising=False)
    hits = [_Hit(0.74), _Hit(0.645)]  # rank-1 lexical match at 0.26, relevant row at 0.355
    assert abs(_gate_top_sim(hits) - 0.26) < 1e-9
    assert _gate_top_sim([]) is None


def test_window_scope_reads_the_best_row(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "window", raising=False)
    hits = [_Hit(0.74), _Hit(0.645)]
    assert abs(_gate_top_sim(hits) - 0.355) < 1e-9


def test_window_scope_keeps_an_exact_match(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "window", raising=False)
    assert _gate_top_sim([_Hit(0.5), _Hit(0.0)]) == 1.0
