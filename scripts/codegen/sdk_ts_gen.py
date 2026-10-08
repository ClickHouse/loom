"""Generate the TypeScript SDK's operation table and wire types from the
server's operation registry.

Every `Operation` in `server.operations.registry.ALL_OPERATIONS` declares a Pydantic
input and output model. This script turns those into
`sdk/typescript/src/generated/ops.ts`: one TypeScript interface per model
(shared `$defs` hoisted once), an `OPS` table with each op's name, group,
method, role, idempotency, maturity and latency budget, and an `OpMap` type
that binds the two so `client.call("memory.search", input)` is typed end to
end. The TypeScript client never hand-maintains a shape: what the server
serves is what the SDK types.

    python scripts/codegen/sdk_ts_gen.py           # write the file
    python scripts/codegen/sdk_ts_gen.py --check   # exit 1 when the file is stale (CI)

JSON Schema → TypeScript rules (the subset Pydantic v2 emits here):
  string/integer/number/boolean/null → string/number/boolean/null
  enum / const                      → literal union
  anyOf                             → union (a `{type: null}` member → `| null`)
  array (items)                     → T[]; prefixItems → tuple
  object (properties)               → interface / inline object; required set
                                      drives `?`; additionalProperties → Record
  $ref → #/$defs/Name               → hoisted named type
Type names: `<Op>Input` / `<Op>Output` for a model one op uses; the Pydantic
class name for a model several ops share, and for its `$defs` references, so
a name never depends on which op sorts first.
  format date-time / date           → string (documented in a comment)
  Any / unconstrained               → unknown
"""

from __future__ import annotations

import hashlib
import json
import re
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
)

COMMAND = "python scripts/codegen/sdk_ts_gen.py"


def _camel(name: str) -> str:
    p = pascal(name)
    return p[:1].lower() + p[1:]


def _quote_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key) else json.dumps(key)


def _declaration(name: str, body: str) -> str:
    if body.startswith("{"):
        return f"export interface {name} {body}"
    return f"export type {name} = {body};"


class Emitter:
    """Collects hoisted type declarations; renders inline types otherwise."""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.decls: dict[str, str] = {}  # ts name -> declaration body
        self._by_hash: dict[str, str] = {}  # schema hash -> ts name
        self._defs: dict[str, dict[str, Any]] = {}
        self._names = names or {}  # model class name -> ts name

    # -- naming -------------------------------------------------------------
    def _unique_name(self, wanted: str, schema: dict[str, Any]) -> str:
        # `$defs` is the envelope a top-level schema carries, not the model:
        # a model reached as a `$ref` elsewhere hashes the same.
        body = {k: v for k, v in schema.items() if k != "$defs"}
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
        if h in self._by_hash:
            return self._by_hash[h]
        name = wanted
        n = 2
        while name in self.decls and self.decls[name] != "":
            # a different schema already owns this name
            name = f"{wanted}{n}"
            n += 1
        self._by_hash[h] = name
        return name

    # -- entry points ---------------------------------------------------------
    def declare(self, name: str, schema: dict[str, Any]) -> str:
        """Declare `schema` under `name` (an interface when it is an object),
        hoisting its `$defs`. Returns the TS name used."""
        self._defs = dict(schema.get("$defs") or {})
        body = self.render(schema)
        final = self._unique_name(name, schema)
        if final not in self.decls:
            self.decls[final] = _declaration(final, body)
        return final

    def _declare_def(self, ref: str) -> str:
        key = ref.split("/")[-1]
        schema = self._defs.get(key)
        if schema is None:
            return "unknown"
        final = self._unique_name(self._names.get(key) or ident(pascal(key)), schema)
        if final not in self.decls:
            self.decls[final] = ""  # reserve: a recursive $ref renders the name
            self.decls[final] = _declaration(final, self.render(schema))
        return final

    # -- rendering ------------------------------------------------------------
    def render(self, s: Any, *, indent: int = 0) -> str:
        if not isinstance(s, dict):
            return "unknown"
        if "$ref" in s:
            return self._declare_def(str(s["$ref"]))
        if "const" in s:
            return json.dumps(s["const"])
        if "enum" in s:
            vals = [json.dumps(v) for v in s["enum"]]
            return " | ".join(vals) if vals else "never"
        if "anyOf" in s or "oneOf" in s:
            members = s.get("anyOf") or s.get("oneOf") or []
            parts: list[str] = []
            for m in members:
                r = self.render(m, indent=indent)
                if r not in parts:
                    parts.append(r)
            return " | ".join(parts) if parts else "unknown"
        if "allOf" in s:
            members = s["allOf"]
            if len(members) == 1:
                return self.render(members[0], indent=indent)
            return " & ".join(self.render(m, indent=indent) for m in members)
        t = s.get("type")
        if isinstance(t, list):
            return " | ".join(self.render({**s, "type": x}, indent=indent) for x in t)
        if t == "string":
            return "string"
        if t in ("integer", "number"):
            return "number"
        if t == "boolean":
            return "boolean"
        if t == "null":
            return "null"
        if t == "array":
            if "prefixItems" in s:
                return (
                    "[" + ", ".join(self.render(i, indent=indent) for i in s["prefixItems"]) + "]"
                )
            inner = self.render(s.get("items", {}), indent=indent)
            return f"Array<{inner}>" if (" " in inner or "|" in inner) else f"{inner}[]"
        if t == "object" or "properties" in s:
            props = s.get("properties") or {}
            required = set(s.get("required") or [])
            addl = s.get("additionalProperties")
            if not props:
                if isinstance(addl, dict):
                    return f"Record<string, {self.render(addl, indent=indent)}>"
                return "Record<string, unknown>"
            pad = "  " * (indent + 1)
            lines = ["{"]
            for key, sub in props.items():
                doc = str(sub.get("description") or "").strip()
                if doc:
                    doc1 = doc.split("\n")[0][:160]
                    lines.append(f"{pad}/** {doc1} */")
                opt = "" if key in required else "?"
                fmt = sub.get("format")
                ts = self.render(sub, indent=indent + 1)
                note = f" // {fmt}" if fmt in ("date-time", "date") else ""
                lines.append(f"{pad}{_quote_key(key)}{opt}: {ts};{note}")
            if isinstance(addl, dict):
                lines.append(f"{pad}[key: string]: {self.render(addl, indent=indent + 1)};")
            lines.append("  " * indent + "}")
            return "\n".join(lines)
        return "unknown"


def _ts_bool(value: bool) -> str:
    return "true" if value else "false"


def build() -> str:
    from server.operations.registry import ALL_OPERATIONS

    ops = sorted(ALL_OPERATIONS, key=lambda o: o.name)
    uses = Counter(m for op in ops for m in (op.input_model, op.output_model))
    names: dict[str, str] = {}
    for op in ops:
        for suffix, model in (("Input", op.input_model), ("Output", op.output_model)):
            shared = uses[model] > 1
            names[model.__name__] = model.__name__ if shared else f"{pascal(op.name)}{suffix}"
    em = Emitter(names)
    rows: list[tuple[OpFacts, str, str]] = []  # (facts, input type, output type)
    for op in ops:
        in_name = em.declare(names[op.input_model.__name__], op.input_model.model_json_schema())
        out_name = em.declare(names[op.output_model.__name__], op.output_model.model_json_schema())
        rows.append((OpFacts.of(op), in_name, out_name))
    lines: list[str] = [
        "// GENERATED by scripts/codegen/sdk_ts_gen.py from server.operations.registry.ALL_OPERATIONS — do not edit.",  # noqa: E501
        f"// Regenerate: {COMMAND}   (CI: --check)",
        "// Dates ride the wire as ISO-8601 strings (Pydantic date-time / date); see the // notes.",
        "/* eslint-disable */",
        "",
    ]
    for decl in em.decls.values():
        lines += [decl, ""]
    lines += [
        "/** One served operation; the HTTP path is `/v1/${name}`. */",
        "export interface OpSpec {",
        "  readonly name: OpName;",
        "  readonly group: string;",
        "  readonly method: string;",
        "  readonly idempotent: boolean;",
        "  readonly role: string;",
        '  readonly maturity: "stable" | "experimental" | "";',
        "  readonly budgetP99Ms: number;",
        "  readonly audit: boolean;",
        "}",
        "",
        "export type OpName =",
    ]
    lines += [f"  | {json.dumps(f.name)}" for f, _, _ in rows]
    lines[-1] += ";"
    lines += [
        "",
        "/** Input/output type per operation; `call(name, input)` is typed through this map. */",
        "export interface OpMap {",
    ]
    lines += [
        f"  {json.dumps(f.name)}: {{ input: {in_name}; output: {out_name} }};"
        for f, in_name, out_name in rows
    ]
    lines += ["}", "", "export const OPS: readonly OpSpec[] = ["]
    for f, _, _ in rows:
        group, _, method = f.name.partition(".")
        fields = ", ".join(
            [
                f"name: {json.dumps(f.name)}",
                f"group: {json.dumps(group)}",
                f"method: {json.dumps(_camel(method))}",
                f"idempotent: {_ts_bool(f.idempotent)}",
                f"role: {json.dumps(f.role)}",
                f"maturity: {json.dumps(f.maturity)}",
                f"budgetP99Ms: {f.budget_p99_ms}",
                f"audit: {_ts_bool(f.audit)}",
            ]
        )
        lines.append(f"  {{ {fields} }},")
    lines += [
        "] as const;",
        "",
        "export const OP_BY_NAME: Readonly<Record<OpName, OpSpec>> = Object.fromEntries(",
        "  OPS.map((o) => [o.name, o]),",
        ") as Readonly<Record<OpName, OpSpec>>;",
        "",
        f"export const GENERATED_OP_COUNT = {len(rows)};",
        "",
    ]
    return "\n".join(lines)


TARGET = GeneratedFile(ROOT / "sdk" / "typescript" / "src" / "generated" / "ops.ts", COMMAND)


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, build, argv)


if __name__ == "__main__":
    raise SystemExit(main())
