"""The TypeScript SDK's operation table and types are generated from the
registry; this fails when the checked-in file is stale."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.codegen import sdk_ts_gen

ROOT = Path(__file__).resolve().parents[2]


def test_generated_ops_ts_is_current() -> None:
    expected = sdk_ts_gen.build()
    current = (ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts").read_text()
    assert current == expected, (
        "sdk/typescript/src/generated/ops.ts is stale: run python scripts/codegen/sdk_ts_gen.py"
    )


def test_generated_table_covers_every_operation() -> None:
    from server.operations.registry import ALL_OPERATIONS

    text = (ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts").read_text()
    for op in ALL_OPERATIONS:
        assert f'"{op.name}": {{ input:' in text, op.name
    assert f"GENERATED_OP_COUNT = {len(ALL_OPERATIONS)};" in text


def test_type_names_follow_the_model_not_the_op_order() -> None:
    """A model one op uses is `<Op>Input/Output`; a model several ops share is
    its class name, declared once."""
    from collections import Counter

    from scripts.codegen.common import pascal
    from server.operations.registry import ALL_OPERATIONS

    text = (ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts").read_text()
    uses = Counter(m for op in ALL_OPERATIONS for m in (op.input_model, op.output_model))
    for op in ALL_OPERATIONS:
        names = []
        for suffix, model in (("Input", op.input_model), ("Output", op.output_model)):
            names.append(model.__name__ if uses[model] > 1 else f"{pascal(op.name)}{suffix}")
        assert f'"{op.name}": {{ input: {names[0]}; output: {names[1]} }};' in text, op.name
    declared = re.findall(r"^export (?:interface|type) (\w+)", text, re.M)
    assert len(declared) == len(set(declared))
    assert not [n for n in declared if re.fullmatch(r"\w+\d", n) and n[:-1] in declared]
