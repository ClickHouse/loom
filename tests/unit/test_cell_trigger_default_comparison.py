"""Default-field proof checks keep their refusals without per-field JSON setup."""

from copy import deepcopy

import pytest

from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_ci_creator import protocol_reader as protocol_reader
from tests.unit.test_cell_trigger_provenance_reuse import _case


def _expected(native, specification, context, row, audit, api):
    native.Codec(specification).expected(
        row,
        audit,
        context,
        api=api,
        metadata={"summary": {"jti": "b" * 32}},
        driver={"host": "proof-host", "pid": 701},
    )


@pytest.mark.parametrize("api", [False, True], ids=["delivery", "api"])
def test_default_proof_bounds_json_encoder_setup(
    keeper_config_case, protocol_reader, monkeypatch, api
):
    native, *_ = keeper_config_case
    specification, context, _, _, memories, audits, _ = _case(native, 1, api)
    calls = 0
    original = native.json.dumps

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(native.json, "dumps", counted)
    _expected(native, specification, context, memories[0], audits[0], api)
    # Encoder setup stays bounded as the protocol adds default fields.
    assert calls <= 16


@pytest.mark.parametrize("index", range(44))
def test_default_proof_refuses_each_changed_field(keeper_config_case, protocol_reader, index):
    native, *_ = keeper_config_case
    specification, context, _, _, memories, audits, _ = _case(native, 1, False)
    field, default = list(specification["sparse_defaults"].items())[index]
    assert default != {"unexpected": True}
    row = deepcopy(memories[0])
    row[field] = {"unexpected": True}
    with pytest.raises(native.Refused):
        _expected(native, specification, context, row, audits[0], False)


@pytest.mark.parametrize("replacement", [False, 0.0])
def test_default_proof_keeps_json_numeric_type_refusals(
    keeper_config_case, protocol_reader, replacement
):
    native, *_ = keeper_config_case
    specification, context, _, _, memories, audits, _ = _case(native, 1, False)
    assert specification["sparse_defaults"]["pinned"] == 0
    row = deepcopy(memories[0])
    row["pinned"] = replacement
    assert row["pinned"] == 0
    with pytest.raises(native.Refused):
        _expected(native, specification, context, row, audits[0], False)
