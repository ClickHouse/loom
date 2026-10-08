"""Generate `sdk/python/loom/ops.py` — the operation table and request /
response TypedDicts of the Python SDK — from the server's registry
(the TypeScript twin is sdk_ts_gen.py).

The Python SDK stays dict-in / dict-out. What this adds is
the *names*: `OpName` (a Literal of every served op), `OPS` (idempotency,
role, maturity, latency budget per op) and one TypedDict per input and
output model, so an editor completes `agent.call("memory.search", …)` and a
type checker sees the field names without the SDK growing a typed model
layer it would have to keep in step by hand. The dicts are `total=False`
with `Required[...]` on the fields the server requires.

    python scripts/codegen/sdk_py_gen.py           # write the file
    python scripts/codegen/sdk_py_gen.py --check   # exit 1 when the file is stale (CI)
"""

from __future__ import annotations

import hashlib
import json
import keyword
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import (  # noqa: E402
    GeneratedFile,
    OpFacts,
    generator_main,
    ident,
    pascal,
    ruff_format,
)

COMMAND = "python scripts/codegen/sdk_py_gen.py"


def _wrap(t: str, required: bool) -> str:
    return f"Required[{t}]" if required else f"NotRequired[{t}]"


#: Keys `Agent.call` / `Agent.identity` stamp on every request body; the
#: `<Op>Body` TypedDict makes them optional so a caller can type the body it
#: actually writes (the TypeScript SDK's `Body<K>`).
IDENTITY_KEYS = frozenset({"org", "namespace", "agent", "user_id", "session_id", "run_id"})


class Emitter:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.decls: dict[str, str] = {}
        self._by_hash: dict[str, str] = {}
        self._defs: dict[str, dict[str, Any]] = {}
        self.functional: list[str] = []  # TypedDicts whose keys are not identifiers
        self._names = names or {}  # model class name -> type name

    def _unique(self, wanted: str, schema: dict[str, Any]) -> str:
        # `$defs` is the envelope a top-level schema carries, not the model:
        # a model reached as a `$ref` elsewhere hashes the same.
        body = {k: v for k, v in schema.items() if k != "$defs"}
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
        if h in self._by_hash:
            return self._by_hash[h]
        name, n = wanted, 2
        while name in self.decls:
            name = f"{wanted}{n}"
            n += 1
        self._by_hash[h] = name
        return name

    def declare(self, name: str, schema: dict[str, Any]) -> str:
        self._defs = dict(schema.get("$defs") or {})
        final = self._unique(name, schema)
        if final not in self.decls:
            self.decls[final] = ""
            self.decls[final] = self._typed_dict(final, schema)
        return final

    def declare_body(self, name: str, schema: dict[str, Any]) -> str:
        """`<Op>Body`: the input TypedDict with the identity keys optional."""
        final = self._unique(
            name, {"__body_of__": {k: v for k, v in schema.items() if k != "$defs"}}
        )
        if final not in self.decls:
            self.decls[final] = ""
            self.decls[final] = self._typed_dict(final, schema, optional=IDENTITY_KEYS)
        return final

    def _declare_def(self, ref: str) -> str:
        key = ref.split("/")[-1]
        schema = self._defs.get(key)
        if schema is None:
            return "Any"
        final = self._unique(self._names.get(key) or ident(pascal(key)), schema)
        if final not in self.decls:
            self.decls[final] = ""
            if schema.get("type") == "object" or "properties" in schema:
                self.decls[final] = self._typed_dict(final, schema)
            else:
                self.decls[final] = f"{final} = {self.render(schema)}"
        return f'"{final}"'

    def _typed_dict(
        self, name: str, schema: dict[str, Any], optional: frozenset[str] = frozenset()
    ) -> str:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or []) - optional
        if not props:
            addl = schema.get("additionalProperties")
            inner = self.render(addl) if isinstance(addl, dict) else "Any"
            return f"{name} = dict[str, {inner}]"
        if any(not k.isidentifier() or keyword.iskeyword(k) for k in props):
            # functional syntax keeps non-identifier keys
            items = []
            for k, sub in props.items():
                t = self.render(sub)
                items.append(f"        {json.dumps(k)}: {_wrap(t, k in required)},")
            body = "\n".join(items)
            head = f"{name} = TypedDict(\n    {json.dumps(name)},\n    {{\n"
            return head + body + "\n    },\n    total=False,\n)"
        lines = [f"class {name}(TypedDict, total=False):"]
        doc = str(schema.get("description") or "").strip().split("\n")[0][:150]
        if doc:
            lines.append(f"    {json.dumps(doc)}")
        for k, sub in props.items():
            t = _wrap(self.render(sub), k in required)
            fmt = sub.get("format")
            note = f"  # {fmt} as ISO-8601 text" if fmt in ("date-time", "date") else ""
            lines.append(f"    {k}: {t}{note}")
        return "\n".join(lines)

    def render(self, s: Any) -> str:
        if not isinstance(s, dict):
            return "Any"
        if "$ref" in s:
            return self._declare_def(str(s["$ref"]))
        if "const" in s:
            return f"Literal[{json.dumps(s['const'])}]"
        if "enum" in s:
            vals = ", ".join(json.dumps(v) for v in s["enum"])
            return f"Literal[{vals}]" if vals else "Any"
        if "anyOf" in s or "oneOf" in s:
            parts: list[str] = []
            for m in s.get("anyOf") or s.get("oneOf") or []:
                r = self.render(m)
                if r not in parts:
                    parts.append(r)
            if not parts:
                return "Any"
            if "None" in parts and len(parts) == 2:
                other = parts[0] if parts[1] == "None" else parts[1]
                return f"Optional[{other}]"
            return f"Union[{', '.join(parts)}]" if len(parts) > 1 else parts[0]
        if "allOf" in s and len(s["allOf"]) == 1:
            return self.render(s["allOf"][0])
        t = s.get("type")
        if isinstance(t, list):
            return f"Union[{', '.join(self.render({**s, 'type': x}) for x in t)}]"
        if t == "string":
            return "str"
        if t == "integer":
            return "int"
        if t == "number":
            return "float"
        if t == "boolean":
            return "bool"
        if t == "null":
            return "None"
        if t == "array":
            if "prefixItems" in s:
                return f"tuple[{', '.join(self.render(i) for i in s['prefixItems'])}]"
            return f"list[{self.render(s.get('items', {}))}]"
        if t == "object" or "properties" in s:
            props = s.get("properties") or {}
            if not props:
                addl = s.get("additionalProperties")
                return f"dict[str, {self.render(addl) if isinstance(addl, dict) else 'Any'}]"
            # inline object with properties: hoist as an anonymous def
            name = self._unique("_Obj", s)
            if name not in self.decls:
                self.decls[name] = ""
                self.decls[name] = self._typed_dict(name, s)
            return f'"{name}"'
        return "Any"


def op_rows() -> tuple[Emitter, list[tuple[OpFacts, str, str, str]]]:
    """Every served op, sorted by name, with the TypedDict names of its input,
    body and output: `(facts, input, body, output)`. The Emitter holds the
    declarations those names refer to."""
    from server.operations.registry import ALL_OPERATIONS

    # A model one op uses is `<Op>Input/Body/Output`; a model several ops share
    # takes its class name (and `<Class minus Input>Body`), so a name never
    # depends on which op sorts first.
    ops = sorted(ALL_OPERATIONS, key=lambda o: o.name)
    uses = Counter(m for op in ops for m in (op.input_model, op.output_model))
    names: dict[str, str] = {}
    for op in ops:
        for suffix, model in (("Input", op.input_model), ("Output", op.output_model)):
            shared = uses[model] > 1
            names[model.__name__] = model.__name__ if shared else f"{pascal(op.name)}{suffix}"
    em = Emitter(names)
    rows: list[tuple[OpFacts, str, str, str]] = []  # (facts, input, body, output)
    for op in ops:
        in_schema = op.input_model.model_json_schema()
        in_name = em.declare(names[op.input_model.__name__], in_schema)
        body_name = em.declare_body(f"{in_name.removesuffix('Input')}Body", in_schema)
        out_name = em.declare(names[op.output_model.__name__], op.output_model.model_json_schema())
        rows.append((OpFacts.of(op), in_name, body_name, out_name))
    return em, rows


def build() -> str:
    em, rows = op_rows()
    out: list[str] = [
        '"""GENERATED by scripts/codegen/sdk_py_gen.py from server.operations.registry.ALL_OPERATIONS — do not edit.',  # noqa: E501
        "",
        "Every served operation's name, spec and request/response field names",
        "`<Op>Body` is `<Op>Input` with the identity keys optional: the shape",
        "`Agent.call` takes. Regenerate:",
        f"{COMMAND}  (CI: --check).",
        "Dates ride the wire as ISO-8601 text; unconstrained values are Any.",
        '"""',
        "# ruff: noqa: E501, UP007, UP045",
        "# (Optional / Union: a union naming a later-declared type is evaluated at",
        '# import, where `"Name" | None` would raise)',
        "",
        "from dataclasses import dataclass",
        "from typing import Any, Literal, NotRequired, Optional, Required, TypedDict, Union",
        "",
        "__all__ = [",
        '    "OPS",',
        '    "OP_NAMES",',
        '    "OpName",',
        '    "OpSpec",',
        '    "spec",',
        "]",
        "",
        "",
        "@dataclass(frozen=True)",
        "class OpSpec:",
        '    """One served operation; the HTTP path is /v1/{name}."""',
        "",
        "    name: str",
        "    input: str",
        "    body: str",
        "    output: str",
        "    idempotent: bool",
        "    role: str",
        "    maturity: str",
        "    budget_p99_ms: int",
        "    audit: bool",
        "",
        "",
        "OpName = Literal[",
    ]
    names = [json.dumps(facts.name) for facts, *_ in rows]
    out += [f"    {n}," for n in names]
    out += [
        "]",
        "",
        "OP_NAMES: tuple[str, ...] = (",
        *[f"    {n}," for n in names],
        ")",
        "",
    ]
    out.append("OPS: dict[str, OpSpec] = {")
    for f, in_name, body_name, out_name in rows:
        out.append(
            f"    {json.dumps(f.name)}: OpSpec({json.dumps(f.name)}, "
            f"{json.dumps(in_name)}, {json.dumps(body_name)}, "
            f"{json.dumps(out_name)}, {f.idempotent}, {json.dumps(f.role)}, "
            f"{json.dumps(f.maturity)}, {f.budget_p99_ms}, {f.audit}),"
        )
    out += [
        "}",
        "",
        "",
        "def spec(name: str) -> Optional[OpSpec]:",
        '    """The declared spec of an op, or None for a name this SDK does not know."""',
        "    return OPS.get(name)",
        "",
        "",
        "# ---- request / response field names -------------------------------------",
        "",
    ]
    for name in em.decls:
        out.append(em.decls[name])
        out.append("")
        out.append("")
    return _ruff_format("\n".join(out).rstrip("\n") + "\n")


def _ruff_format(text: str) -> str:
    return ruff_format(text, "ops.py")


# Both sides are normalised with the local ruff, so a ruff version that formats
# a line differently from the one that wrote the file never reports drift that
# is not a registry change.
TARGET = GeneratedFile(ROOT / "sdk" / "python" / "loom" / "ops.py", COMMAND, _ruff_format)


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, build, argv)


if __name__ == "__main__":
    raise SystemExit(main())
