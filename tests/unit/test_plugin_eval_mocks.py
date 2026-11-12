"""The plugin eval's fixed loom replies are valid answers of the tools they
stand in for.

`integrations/claude-code-plugin/evals/mocks/loom/<tool>.md` answers the
eval agent in place of the loom MCP server. A mock that drifts from the wire
contract (a renamed field, a field the output model no longer has) grades
the skill against a server that does not exist. Each reply must therefore
be:

- for a registry op: a valid instance of the op's output model, with no key
  outside the model;
- for a synthetic tool (`loom_search`, `loom_activate`): an object matching
  the `outputSchema` its MCP descriptor declares: required keys present,
  declared types, no undeclared key.

`{{input.<field>}}` placeholders, which the eval runner fills from the
call, are validated as strings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from bench.skill_eval import parse_frontmatter
from server.operations.registry import ALL_OPERATIONS
from server.transport.mcp_tools import _synthetic_tool_descriptors, listed_tool_names

MOCKS = Path(__file__).resolve().parents[2] / "integrations/claude-code-plugin/evals/mocks/loom"
_OPS = {op.mcp_tool_name: op for op in ALL_OPERATIONS}
_SYNTHETIC = {
    d["name"]: d.get("outputSchema") or {}
    for d in _synthetic_tool_descriptors("core", listed_tool_names("core"))
}
_PLACEHOLDER = re.compile(r"\{\{\s*input\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}")
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "string": str,
    "array": list,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}
_FILES = sorted(MOCKS.glob("*.md"))


def _reply(path: Path) -> tuple[str, Any]:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta.get("type", ""), json.loads(_PLACEHOLDER.sub("placeholder", body.strip()))


def _model_keys(model: type[BaseModel]) -> set[str]:
    return {f.alias or name for name, f in model.model_fields.items()} | set(model.model_fields)


def _schema_problems(value: Any, schema: dict[str, Any]) -> list[str]:
    want = schema.get("type")
    if want and not isinstance(value, _JSON_TYPES[want]):
        return [f"expected {want}, got {type(value).__name__}"]
    if not isinstance(value, dict):
        return []
    props = schema.get("properties") or {}
    out = [f"missing required {k!r}" for k in schema.get("required", []) if k not in value]
    out += [f"undeclared key {k!r}" for k in value if props and k not in props]
    for k, sub in props.items():
        if k in value:
            out += [f"{k}: {p}" for p in _schema_problems(value[k], sub)]
    return out


def test_there_are_mocks_and_each_names_a_tool() -> None:
    assert _FILES, f"no mocks under {MOCKS}"
    unknown = [p.stem for p in _FILES if p.stem not in _OPS and p.stem not in _SYNTHETIC]
    assert not unknown, f"mocks for tools the server does not expose: {unknown}"


@pytest.mark.parametrize("path", _FILES, ids=[p.stem for p in _FILES])
def test_a_fixed_reply_is_a_valid_answer_of_its_tool(path: Path) -> None:
    kind, reply = _reply(path)
    assert kind == "fixed", f"{path.name}: only fixed replies are validated, got {kind!r}"
    op = _OPS.get(path.stem)
    if op is None:
        problems = _schema_problems(reply, _SYNTHETIC[path.stem])
        assert not problems, f"{path.name} vs {path.stem} outputSchema: {problems}"
        return
    try:
        op.output_model.model_validate(reply)
    except ValidationError as e:
        pytest.fail(f"{path.name} is not a {op.output_model.__name__}: {e}")
    extra = sorted(set(reply) - _model_keys(op.output_model))
    assert not extra, f"{path.name}: keys {op.output_model.__name__} does not have: {extra}"


def test_the_schema_check_refuses_an_undeclared_or_mistyped_key() -> None:
    schema = _SYNTHETIC["loom_activate"]
    assert _schema_problems({"primer": "p"}, schema) == []
    assert _schema_problems({}, schema) == ["missing required 'primer'"]
    assert _schema_problems({"primer": 1, "x": 0}, schema) == [
        "undeclared key 'x'",
        "primer: expected string, got int",
    ]
