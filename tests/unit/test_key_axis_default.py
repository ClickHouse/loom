"""The key axis is off by default; the knob stays."""

from __future__ import annotations

from server.settings import Settings


def test_key_axis_default_is_off_and_still_a_knob() -> None:
    assert Settings.model_fields["memory_search_key_axis"].default == "auto"
    assert Settings(memory_search_key_axis="ON").memory_search_key_axis == "on"
