"""A card written by an upgradable model is rewritten by a
pass on another model although the file is unchanged; every other unchanged
card keeps its text."""

from __future__ import annotations

import pytest

from server.code.workers.summary import upgradable
from server.settings import settings


def _live(model: str) -> dict:
    return {"summary": "what the file does", "summary_model": model, "inputs_hash": "abc"}


def test_only_listed_models_are_upgraded_and_only_by_another_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_summary_model", "gpt-4o")
    monkeypatch.setattr(settings, "code_summary_upgrade_from_models", "gpt-4o-mini, gpt-3.5-turbo")
    assert upgradable(_live("gpt-4o-mini")) is True
    assert upgradable(_live("gpt-4o")) is False  # already the configured model
    assert upgradable(_live("claude-haiku-4-5")) is False  # not listed
    assert upgradable(_live("")) is False  # a placeholder is not an upgrade, it is a first card
    monkeypatch.setattr(settings, "code_summary_upgrade_from_models", "")
    assert upgradable(_live("gpt-4o-mini")) is False
    monkeypatch.setattr(settings, "code_summary_model", "")
    monkeypatch.setattr(settings, "code_summary_upgrade_from_models", "gpt-4o-mini")
    assert upgradable(_live("gpt-4o-mini")) is False
