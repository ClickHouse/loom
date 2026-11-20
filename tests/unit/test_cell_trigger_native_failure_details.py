"""Native failure details expose bounded numbers and classes without payloads."""

import json
import sys
from copy import deepcopy

import pytest
from clickhouse_connect.driver.exceptions import OperationalError

from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case


def detail():
    return {
        "error_type": "OperationalError",
        "site": {"file": "native.py", "line": 702, "function": "connections"},
        "outcome": "error",
        "driver": {
            "code": 159,
            "causes": [{"error_type": "ConnectionRefusedError", "errno": 111}],
        },
    }


@pytest.mark.parametrize("shape", ["transport", "bounded-cycle"])
def test_native_sidecar_keeps_only_numeric_code_and_bounded_causes(
    keeper_config_case, monkeypatch, tmp_path, shape
):
    native, instance, _, _, _ = keeper_config_case
    context, output = tmp_path / "failure-context.json", tmp_path / "pg_cell-created.json"
    native.write(
        context,
        instance.s | {"root": str(tmp_path), "databases": {"pg_cell": "bench_cell"}},
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    primary = OperationalError("private-SQL-URL-settings-token", code=159)
    cause = ConnectionRefusedError(111, "private-network-address")
    primary.__cause__ = cause
    expected = detail()["driver"]
    if shape == "bounded-cycle":
        previous = cause
        for _ in range(6):
            child = OSError(5, "private-payload")
            previous.__cause__ = child
            previous = child
        previous.__cause__ = cause
        expected["causes"] += [{"error_type": "OSError", "errno": 5}] * 3

    async def failed(*args, **kwargs):
        raise primary

    monkeypatch.setattr(native, "stores", failed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native.py",
            "--task",
            "create",
            "--role",
            "pg_cell",
            "--context",
            str(context),
            "--output",
            str(output),
        ],
    )
    assert native.main() == 1
    assert not output.exists()
    raw = output.with_name(output.name + ".failure.json").read_bytes()
    metadata = native.read(output.with_name(output.name + ".failure.json"), limit=4096)
    assert metadata.get("driver") == expected
    assert metadata["error_type"] == "OperationalError"
    assert len(raw) <= 4096 and b"private-" not in raw


def test_valid_driver_detail_reaches_the_original_command_failure(keeper_config_case):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    metadata = detail()
    native.write(instance.w / "pg_cell-created.json.failure.json", metadata)
    original = native.Refused("private-command-failure")

    def refused(*args, **kwargs):
        raise original

    owner.command = refused
    with pytest.raises(native.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original and original.native == metadata
    assert "private-" not in json.dumps(original.native)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", True),
        ("code", -1),
        ("code", 2**31),
        ("code", "private-code"),
        ("causes", [{"error_type": "OSError", "errno": 5}] * 5),
        ("class", "private-class-name"),
        ("class", "A" * 65),
        ("errno", True),
        ("errno", 2**31),
        ("extra", "private-message"),
    ],
)
def test_unsafe_driver_detail_cannot_replace_the_command_failure(keeper_config_case, field, value):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    metadata = deepcopy(detail())
    driver = metadata["driver"]
    if field in {"code", "causes"}:
        driver[field] = value
    elif field in {"class", "errno"}:
        driver["causes"][0]["error_type" if field == "class" else "errno"] = value
    else:
        driver["message"] = value
    native.write(instance.w / "pg_cell-created.json.failure.json", metadata)
    original = native.Refused("original")

    def refused(*args, **kwargs):
        raise original

    owner.command = refused
    with pytest.raises(native.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original and original.native is None
