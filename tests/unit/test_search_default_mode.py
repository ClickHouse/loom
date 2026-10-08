"""`memory_search_default_mode`: the deployment decides what an unpinned
memory.search runs; a caller's explicit search_mode or recipe always wins."""

from __future__ import annotations

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.query_plan import (
    _apply_default_search_mode,
    _apply_search_recipe,
)


def _set(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_search_default_mode", mode)


def test_unpinned_search_takes_the_deployment_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "semantic")
    out = _apply_default_search_mode(MemorySearchInput(org="o", namespace="n", query="q"))
    assert out.search_mode == "semantic"


def test_explicit_mode_survives_even_when_equal_to_the_wire_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, "semantic")
    pinned = MemorySearchInput(org="o", namespace="n", query="q", search_mode="rrf")
    assert _apply_default_search_mode(pinned).search_mode == "rrf"


def test_recipe_is_a_caller_choice_and_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "semantic")
    inp = _apply_search_recipe(
        MemorySearchInput(org="o", namespace="n", query="q", recipe="quality")
    )
    assert inp.search_mode == "bm25_hybrid"
    assert _apply_default_search_mode(inp).search_mode == "bm25_hybrid"


def test_an_unknown_setting_fails_at_load() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError, match="memory_search_default_mode"):
        Settings(_env_file=None, memory_search_default_mode="vibes")  # type: ignore[call-arg]


def test_default_setting_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "rrf")
    inp = MemorySearchInput(org="o", namespace="n", query="q")
    assert _apply_default_search_mode(inp) is inp


def test_the_setting_default_is_semantic() -> None:
    """An unconfigured deployment runs plain vector search.
    The wire default (`MemorySearchInput.search_mode`) stays "rrf"."""
    from server.settings import Settings

    assert Settings().memory_search_default_mode == "semantic"
    assert MemorySearchInput(org="o", namespace="n", query="q").search_mode == "rrf"


def test_a_fresh_settings_object_fills_semantic_on_an_unpinned_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import settings as settings_mod
    from server.settings import Settings

    monkeypatch.setattr(settings_mod, "settings", Settings())
    out = _apply_default_search_mode(MemorySearchInput(org="o", namespace="n", query="q"))
    assert out.search_mode == "semantic"


def test_defaulting_marks_the_input_even_when_the_value_does_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner asks whether the mode was the caller's."""
    _set(monkeypatch, "rrf")
    out = _apply_default_search_mode(MemorySearchInput(org="o", namespace="n", query="q"))
    assert out.search_mode == "rrf" and out._search_mode_defaulted is True
    _set(monkeypatch, "semantic")
    out = _apply_default_search_mode(MemorySearchInput(org="o", namespace="n", query="q"))
    assert out.search_mode == "semantic" and out._search_mode_defaulted is True
    pinned = _apply_default_search_mode(
        MemorySearchInput(org="o", namespace="n", query="q", search_mode="semantic")
    )
    assert pinned._search_mode_defaulted is False
