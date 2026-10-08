"""A base64-encoded value is stored as given and the write
answers with one advisory; plain text, JSON, hashes and short tokens are left
alone. The viewer's detector mirrors this module's two shapes."""

from __future__ import annotations

import base64
import json

import pytest

import server.memory.app.write as _mw
from server.memory.api.models_crud import MemorySetInput, MemorySetOutput
from server.memory.app import write as write_mod
from server.memory.domain.value_encoding import encoded_value_advisory
from server.observability.event_args import project_result

_REPORT = json.dumps(
    {
        "attempt_id": "sre-model-independent-use-86be1a4d",
        "markdown": "# Independent operational model\n\n" + "Row " * 200,
    }
)
_B64 = base64.b64encode(_REPORT.encode()).decode()


def test_custom_envelope_is_flagged_with_its_field_and_encoding() -> None:
    value = json.dumps({"encoding": "sre-json-base64-v1", "sha256": "9a1d" * 16, "payload": _B64})
    adv = encoded_value_advisory(value)
    assert adv is not None
    assert "envelope field 'payload'" in adv and "encoding='sre-json-base64-v1'" in adv
    assert "content_sha256" in adv and "plain text or JSON" in adv


def test_whole_value_base64_is_flagged() -> None:
    adv = encoded_value_advisory(_B64)
    assert adv is not None and "whole value" in adv


def test_urlsafe_base64_and_wrapped_lines_are_recognised() -> None:
    urlsafe = base64.urlsafe_b64encode(_REPORT.encode()).decode()
    wrapped = "\n".join(_B64[i : i + 76] for i in range(0, len(_B64), 76))
    assert encoded_value_advisory(urlsafe) is not None
    assert encoded_value_advisory(wrapped) is not None


@pytest.mark.parametrize(
    "value",
    [
        "Deploys happen on Thursday",
        json.dumps({"attempt_id": "x", "markdown": "# plain JSON report " * 20}),
        "9a1deea5fcc3a2b5c57cc373b51f040c728b8f0571b9ab23c771a6db15fe2610"
        * 2,  # hex, not base64 (odd charset fit but decodes to bytes)
        "abcd" * 15,  # 60 chars: under the floor
        json.dumps({"encoding": "utf-8", "payload": "short"}),
        "",
    ],
)
def test_plain_values_are_not_flagged(value: str) -> None:
    assert encoded_value_advisory(value) is None


def test_binary_base64_is_not_flagged_as_text() -> None:
    blob = base64.b64encode(bytes(range(256)) * 4).decode()
    assert encoded_value_advisory(blob) is None


def test_envelope_naming_base64_without_a_decodable_payload_is_still_flagged() -> None:
    value = json.dumps({"encoding": "application/base64", "payload": "not-really-base64!!"})
    adv = encoded_value_advisory(value)
    assert adv is not None and "encoding='application/base64'" in adv


@pytest.mark.asyncio
async def test_memory_set_carries_the_advisory_and_still_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[MemorySetInput] = []

    async def _inner(input: MemorySetInput) -> MemorySetOutput:
        seen.append(input)
        return MemorySetOutput(
            version=7, memory_id="m1", namespace=input.namespace, memory_key=input.memory_key
        )

    monkeypatch.setattr(_mw, "_handle_memory_set", _inner)
    inp = MemorySetInput(
        org="t",
        namespace="n",
        agent="a",
        memory_key="k",
        value=json.dumps({"encoding": "x-base64", "payload": _B64}),
        classify=False,
    )
    out = await write_mod.handle_memory_set(inp)
    assert seen and out.version == 7, "the write path ran unchanged"
    assert len(out.advisories) == 1 and "base64" in out.advisories[0]

    plain = await write_mod.handle_memory_set(
        MemorySetInput(
            org="t", namespace="n", agent="a", memory_key="k2", value="plain fact", classify=False
        )
    )
    assert plain.advisories == []


def test_advisory_field_is_serialised_on_the_wire() -> None:
    out = MemorySetOutput(version=1, advisories=["value is base64-encoded (whole value): …"])
    assert out.model_dump()["advisories"] == ["value is base64-encoded (whole value): …"]
    assert MemorySetOutput(version=1).model_dump()["advisories"] == []


def test_advisory_is_projected_into_the_ledger_args() -> None:
    out = MemorySetOutput(version=1, advisories=["value is base64-encoded (whole value): fix it"])
    args = project_result(out)
    assert args["out_advisories"].startswith("value is base64-encoded (whole value)")
    assert "out_advisories" not in project_result(MemorySetOutput(version=1))


@pytest.mark.parametrize("value", [True, False, 0, 12345, 3.14, ["a", "b"], None, {"k": "v"}])
def test_non_string_json_values_are_never_flagged(value: object) -> None:
    """memory.set accepts any JSON value; the detector never calls len() on a bool."""
    assert encoded_value_advisory(value) is None


def test_dict_envelope_passed_as_a_json_object_is_flagged() -> None:
    adv = encoded_value_advisory({"encoding": "sre-json-base64-v1", "payload": _B64})
    assert adv is not None and "envelope field 'payload'" in adv


@pytest.mark.asyncio
async def test_memory_set_with_a_boolean_value_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _inner(input: MemorySetInput) -> MemorySetOutput:
        return MemorySetOutput(version=1)

    monkeypatch.setattr(_mw, "_handle_memory_set", _inner)
    for v in (True, 7, 2.5, ["x"], {"k": "v"}):
        out = await write_mod.handle_memory_set(
            MemorySetInput(
                org="t", namespace="n", agent="a", memory_key="k", value=v, classify=False
            )
        )
        assert out.advisories == []
