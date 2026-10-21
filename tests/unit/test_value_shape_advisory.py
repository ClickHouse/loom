"""A double-encoded JSON value and a value over the embed cap
are stored as given and the write answers with one advisory line per shape;
plain strings, genuine objects and non-searchable types are left alone."""

from __future__ import annotations

import json

import pytest

import server.memory.app.write as _mw
from server.memory.api.models_crud import MemorySetInput, MemorySetOutput
from server.memory.app import write as write_mod
from server.memory.domain.value_shape import (
    double_encoded_json_advisory,
    oversized_value_advisory,
    value_shape_advisories,
)
from server.settings import settings

_ALERT = {
    "excerpt": "CPU steal time above 40% on three nodes for eleven minutes " * 20,
    "severity": "page",
    "symbol": "cpu_steal_high",
}
_LONG_TEXT = "Deploys happen on Thursday after the on-call handover. " * 750  # ~40 KB


def test_double_encoded_dict_is_flagged() -> None:
    adv = double_encoded_json_advisory(json.dumps(_ALERT))
    assert adv is not None
    assert "JSON object" in adv and "double-encoded" in adv
    assert "1200-char excerpt" in adv and "memory.set accepts any JSON value" in adv
    assert "`tags` / `entities`" in adv


def test_double_encoded_list_is_flagged_as_an_array() -> None:
    adv = double_encoded_json_advisory(json.dumps([_ALERT, _ALERT]))
    assert adv is not None and "JSON array" in adv


@pytest.mark.parametrize(
    "value",
    [
        "{ this looks like an object but is prose about braces } " * 3,
        "[bracketed] heading followed by a paragraph of plain text " * 3,
        "Deploys happen on Thursday after the on-call handover. " * 3,
        json.dumps("a JSON string that is not an object" * 4),
        json.dumps({"k": "v"}),  # 10 chars: under the floor
        "",
    ],
)
def test_strings_that_are_not_json_documents_are_not_flagged(value: str) -> None:
    assert double_encoded_json_advisory(value) is None


@pytest.mark.parametrize("value", [_ALERT, [_ALERT], True, 7, 2.5, None])
def test_genuine_json_values_are_not_flagged(value: object) -> None:
    """A dict sent as a dict is the fix, not the mistake."""
    assert double_encoded_json_advisory(value) is None


def test_oversized_semantic_string_is_flagged_with_its_size() -> None:
    adv = oversized_value_advisory(_LONG_TEXT, "semantic")
    assert adv is not None
    assert f"{len(json.dumps(_LONG_TEXT)):,} characters" in adv
    assert "first 1200 are embedded" in adv and "knowledge.add" in adv


def test_oversized_dict_is_measured_on_its_serialization() -> None:
    assert oversized_value_advisories_for({"doc": _LONG_TEXT}) is not None
    assert oversized_value_advisories_for({"doc": "short"}) is None


def oversized_value_advisories_for(value: object) -> str | None:
    return oversized_value_advisory(value, "semantic")


@pytest.mark.parametrize("memory_type", ["episodic", "profile"])
def test_non_searchable_types_are_never_flagged_as_oversized(memory_type: str) -> None:
    assert oversized_value_advisory(_LONG_TEXT, memory_type) is None


def test_value_at_the_cap_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_value_advisory_chars", len(json.dumps(_LONG_TEXT)))
    assert oversized_value_advisory(_LONG_TEXT, "semantic") is None
    assert oversized_value_advisory(_LONG_TEXT + "x", "semantic") is not None


def test_setting_zero_disables_the_oversized_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_value_advisory_chars", 0)
    assert oversized_value_advisory(_LONG_TEXT, "semantic") is None


def test_entry_point_stacks_double_encoded_and_oversized() -> None:
    value = json.dumps({"excerpt": _LONG_TEXT, "symbol": "cpu_steal_high"})
    lines = value_shape_advisories(value, "semantic")
    assert len(lines) == 2
    assert "double-encoded" in lines[0] and "knowledge.add" in lines[1]
    assert value_shape_advisories(value, "episodic") == [lines[0]]
    assert value_shape_advisories("plain fact", "semantic") == []


def test_entry_point_keeps_the_base64_advisory() -> None:
    import base64

    blob = base64.b64encode(json.dumps(_ALERT).encode()).decode()
    lines = value_shape_advisories(blob, "semantic")
    assert len(lines) == 1 and "base64" in lines[0]


def test_base64_envelope_sent_as_a_string_earns_the_base64_line_alone() -> None:
    """The envelope string is also a double-encoded object; the base64 line
    names the root cause, so it wins (exactly one line)."""
    import base64

    blob = base64.b64encode(json.dumps(_ALERT).encode()).decode()
    envelope = json.dumps({"encoding": "x-base64", "payload": blob})
    lines = value_shape_advisories(envelope, "semantic")
    assert len(lines) == 1 and "base64" in lines[0] and "double-encoded" not in lines[0]


@pytest.mark.asyncio
async def test_memory_set_returns_both_lines_for_a_large_double_encoded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[MemorySetInput] = []

    async def _inner(input: MemorySetInput, **_: object) -> MemorySetOutput:
        seen.append(input)
        return MemorySetOutput(
            version=7, memory_id="m1", namespace=input.namespace, memory_key=input.memory_key
        )

    monkeypatch.setattr(_mw, "_handle_memory_set", _inner)
    value = json.dumps({"excerpt": _LONG_TEXT, "severity": "page", "symbol": "cpu_steal_high"})
    assert len(value) > 40_000
    out = await write_mod.handle_memory_set(
        MemorySetInput(
            org="t", namespace="n", agent="a", memory_key="k", value=value, classify=False
        )
    )
    assert seen and seen[0].value == value, "the write path ran with the value as given"
    assert len(out.advisories) == 2
    assert "double-encoded" in out.advisories[0]
    assert "knowledge.add" in out.advisories[1]

    fixed = await write_mod.handle_memory_set(
        MemorySetInput(
            org="t",
            namespace="n",
            agent="a",
            memory_key="k2",
            value={"severity": "page", "symbol": "cpu_steal_high"},
            classify=False,
        )
    )
    assert fixed.advisories == []
