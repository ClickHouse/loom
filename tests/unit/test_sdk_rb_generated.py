"""The Ruby SDK's operation table, per-op methods and error classes are generated from
the registry and the server's error codes; this fails when a checked-in file is stale."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.codegen import sdk_rb_gen
from server.operations.registry import ALL_OPERATIONS


@pytest.mark.parametrize(
    "target,build", sdk_rb_gen.TARGETS, ids=lambda t: getattr(t, "display", "")
)
def test_generated_ruby_file_is_current(
    target: sdk_rb_gen.GeneratedFile, build: Callable[[], str]
) -> None:
    assert target.is_current(build()), f"{target.display} is stale: run {sdk_rb_gen.COMMAND}"


def test_check_mode_reports_a_stale_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, build = sdk_rb_gen.TARGETS[0]
    stale = replace(target, path=tmp_path / "ops.rb")
    stale.path.write_text("# an older table\n", encoding="utf-8")
    assert stale.sync(build(), check=True) == 1
    assert "is stale: run python scripts/codegen/sdk_rb_gen.py" in capsys.readouterr().err


def test_generated_table_covers_every_operation() -> None:
    text = (sdk_rb_gen.OUT_DIR / "ops.rb").read_text(encoding="utf-8")
    for op in ALL_OPERATIONS:
        group, method = sdk_rb_gen.split_op(op.name)
        assert f'"{op.name}" => OpSpec.new("{op.name}", "{group}", "{method}",' in text, op.name
    assert f"OP_COUNT = {len(ALL_OPERATIONS)}" in text


def test_ruby_method_names_follow_the_python_sdk_and_skip_object_methods() -> None:
    assert sdk_rb_gen.split_op("tracker.source.add") == ("tracker", "source_add")
    assert sdk_rb_gen.split_op("memory.import") == ("memory", "import")
    assert sdk_rb_gen.split_op("memory.inspect") == ("memory", "inspect_")


def test_an_error_class_never_hides_a_constant_the_gem_relies_on() -> None:
    names = {name for _code, name, *_ in sdk_rb_gen.error_codes()}
    assert not names & sdk_rb_gen.RESERVED_CONSTANTS


def test_every_documented_error_code_has_a_class() -> None:
    from scripts.codegen.docs_reference import render_errors

    documented = {line[6:-1] for line in render_errors().splitlines() if line.startswith("#### `")}
    classes = {code for code, *_ in sdk_rb_gen.error_codes()}
    assert documented == classes
    assert ("PINNED_CONFLICT", "PinnedConflict") in {
        (c, n) for c, n, *_ in sdk_rb_gen.error_codes()
    }
