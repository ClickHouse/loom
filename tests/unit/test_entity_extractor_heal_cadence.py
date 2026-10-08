"""The mirror divergence heal runs on its own clock: once per
LOOM_ENTITY_HEAL_INTERVAL_SECONDS, the first pass after boot always due."""

from __future__ import annotations

from server.memory.workers import entity_extractor as ee
from server.settings import settings


def test_heal_is_due_once_per_interval(monkeypatch) -> None:
    monkeypatch.setattr(settings, "entity_heal_interval_seconds", 900)
    ee._HEAL_LAST_RUN["at"] = 0.0
    assert ee._heal_due(now=1000.0) is True  # first pass after boot
    assert ee._heal_due(now=1500.0) is False  # 500 s later: not yet
    assert ee._heal_due(now=1900.0) is True  # 900 s later: due again
    assert ee._heal_due(now=1901.0) is False


def test_zero_interval_means_every_pass(monkeypatch) -> None:
    monkeypatch.setattr(settings, "entity_heal_interval_seconds", 0)
    ee._HEAL_LAST_RUN["at"] = 0.0
    assert ee._heal_due(now=10.0) and ee._heal_due(now=11.0)
