"""The Python SDK's `loom.ops` (op table + request/response TypedDicts) is
generated from the registry; fails when the file is stale."""

from __future__ import annotations

from pathlib import Path

from scripts.codegen import sdk_py_gen

ROOT = Path(__file__).resolve().parents[2]


def test_generated_ops_py_is_current() -> None:
    expected = sdk_py_gen.build()
    current = (ROOT / "sdk" / "python" / "loom" / "ops.py").read_text()
    assert current == expected, (
        "sdk/python/loom/ops.py is stale: run python scripts/codegen/sdk_py_gen.py"
    )


def test_table_covers_every_operation_with_role_names() -> None:
    from loom import ops

    from server.operations.registry import ALL_OPERATIONS

    assert set(ops.OPS) == {o.name for o in ALL_OPERATIONS}
    assert ops.OP_NAMES == tuple(sorted(ops.OPS))
    assert ops.OPS["memory.search"].idempotent is True
    assert ops.OPS["memory.set_from_messages"].idempotent is False
    assert ops.OPS["knowledge.search"].role == "viewer"
    assert {s.role for s in ops.OPS.values()} <= {
        "viewer",
        "writer",
        "admin",
        "org_admin",
        "user",
        "subject",
    }
    assert ops.spec("nope") is None


def test_typed_dicts_carry_required_and_optional_keys() -> None:
    from loom import ops

    assert set(ops.MemorySearchInput.__required_keys__) == {"org", "namespace", "query"}
    assert "exclude_keys" in ops.MemorySearchInput.__optional_keys__
    assert "uri_prefix" in ops.KnowledgeSearchInput.__optional_keys__
    assert "results" in ops.MemorySearchOutput.__annotations__


def test_type_names_follow_the_model_not_the_op_order() -> None:
    """A model one op uses is `<Op>Input/Body/Output`; a model several ops
    share is its class name (and `<Class minus Input>Body`), declared once."""
    from collections import Counter

    from loom import ops

    from scripts.codegen.common import pascal
    from server.operations.registry import ALL_OPERATIONS

    uses = Counter(m for op in ALL_OPERATIONS for m in (op.input_model, op.output_model))

    def name(op_name: str, suffix: str, model: type) -> str:
        return model.__name__ if uses[model] > 1 else f"{pascal(op_name)}{suffix}"

    for op in ALL_OPERATIONS:
        spec = ops.OPS[op.name]
        in_name = name(op.name, "Input", op.input_model)
        assert spec.input == in_name, op.name
        assert spec.body == in_name.removesuffix("Input") + "Body", op.name
        assert spec.output == name(op.name, "Output", op.output_model), op.name
    text = (ROOT / "sdk" / "python" / "loom" / "ops.py").read_text()
    assert "MemoryGetOutput2" not in text and "MemoryPinInput" not in text
