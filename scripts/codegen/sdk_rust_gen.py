"""Generate Rust serde contracts and typed clients from the operation registry.

Run from the repository root with python scripts/codegen/sdk_rust_gen.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, OpFacts, pascal  # noqa: E402

COMMAND = "python scripts/codegen/sdk_rust_gen.py"
TARGET = ROOT / "sdk/rust/src/wire_models.rs"
MEMORY_OPS = ("memory.set", "memory.get", "memory.search", "memory.delete")
CONVENIENCE_OPS = (
    *MEMORY_OPS,
    "context.pack",
    "knowledge.add",
    "knowledge.get",
    "knowledge.search",
    "ops.health",
    "ops.capabilities",
)

MODEL_NAMES = {
    "_MemorySearchHit": "MemorySearchHit",
    "_SearchHit": "KnowledgeSearchHit",
}

# These numeric types and string labels are part of the crate's public API.
FIELD_TYPES = {
    ("MemorySetInput", "ttl_seconds"): "i64",
    ("MemorySearchInput", "top_k"): "u32",
    **{
        (model, "version"): "u64"
        for model in ("MemorySetOutput", "MemoryGetOutput", "MemorySearchHit", "MemoryDeleteOutput")
    },
    **{
        ("MemorySearchHit", field): "u64"
        for field in (
            "reinforcement_count",
            "used_count",
            "used_cited_count",
            "harm_count",
            "evidence_count",
            "acted_count",
        )
    },
    **{
        ("MemorySearchOutput", field): "u64" for field in ("candidates_total", "pending_extraction")
    },
    **{
        ("MemorySearchInput", field): "String"
        for field in ("memory_type", "namespace_scope", "search_mode")
    },
    **{
        ("MemorySearchOutput", field): "String"
        for field in ("abstain_code", "retrieval_risk", "term_leg")
    },
    ("MemorySearchHit", "source"): "String",
    ("MemorySearchHit", "code_state"): "String",
}

FIELD_NAMES = {
    ("MemorySearchInput", "agent"): "agent_id",
    ("ProfileReplaceInput", "new"): "new_value",
    ("WorkingMemoryReplaceInput", "new"): "new_value",
}

KEYWORDS = set(
    "as async await break const continue crate dyn else enum extern false fn for if impl in "
    "let loop match mod move mut pub ref return self Self static struct super trait true type "
    "unsafe use where while abstract become box do final macro override priv typeof unsized "
    "virtual yield try gen".split()
)


def rust_ident(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not value or value[0].isdigit():
        value = "_" + value
    if value in {"self", "Self", "super", "crate"}:
        return value + "_"
    return "r#" + value if value in KEYWORDS else value


def type_name(name: str) -> str:
    return MODEL_NAMES.get(name, pascal(name))


def rust_string(value: str) -> str:
    escapes = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return (
        '"'
        + "".join(escapes.get(c, f"\\u{{{ord(c):x}}}" if ord(c) < 32 else c) for c in value)
        + '"'
    )


def factory_defaults() -> dict[tuple[str, str], object]:
    """Read constants without invoking model factories or validators."""
    from server.code.api.models_navigation import OUTLINE_DEFAULT_KINDS
    from server.memory.domain.webhook_events import EVENT_TYPES
    from server.tasks.api.models_task import Disclosure

    return {
        ("InspectSettings", "extraction_instructions_refs"): [
            "memory.set_from_messages",
            "extraction_instructions",
        ],
        ("TrackerSimilarInput", "kinds"): ["issue", "pr"],
        ("CodeBisectCandidatesInput", "wideners"): ["patterns", "component", "fix_candidate"],
        ("CodeOutlineInput", "kinds"): list(OUTLINE_DEFAULT_KINDS),
        ("WebhookRegisterInput", "events"): list(EVENT_TYPES),
        ("CostSummaryInput", "group_by"): ["operation"],
        ("InboxRegisterInput", "accept_disclosure"): list(get_args(Disclosure)),
    }


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Describe factory defaults without executing arbitrary Python code."""
    schema = model.model_json_schema()
    seen: set[type[BaseModel]] = set()
    known = factory_defaults()

    def visit(current: type[BaseModel], target: dict[str, Any]) -> None:
        if current in seen:
            return
        seen.add(current)
        for name, field in current.model_fields.items():
            prop = target["properties"][field.alias or name]

            def nested(annotation: Any) -> None:
                if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                    key = annotation.__name__
                    if key not in schema.get("$defs", {}):
                        raise ValueError(f"missing schema for {key}")
                    visit(annotation, schema["$defs"][key])
                for arg in get_args(annotation):
                    nested(arg)

            nested(field.annotation)
            factory = field.default_factory
            if factory is not None:
                if factory in (list, dict, set, tuple):
                    prop["default"] = [] if factory is not dict else {}
                elif (current.__name__, name) in known:
                    prop["default"] = known[current.__name__, name]
                elif isinstance(factory, type) and issubclass(factory, BaseModel):
                    definition = schema["$defs"][factory.__name__]
                    if definition.get("required"):
                        raise ValueError(f"{current.__name__}.{name}: required factory fields")
                    prop["default"] = {
                        key: value["default"]
                        for key, value in definition.get("properties", {}).items()
                        if "default" in value
                    }
                else:
                    raise ValueError(f"{current.__name__}.{name}: unsupported default factory")

    visit(model, schema)
    return schema


def split_nullable(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    choices = schema.get("anyOf")
    if choices and any(c.get("type") == "null" for c in choices):
        remaining = [c for c in choices if c.get("type") != "null"]
        if not remaining:
            return {"type": "null"}, True
        return remaining[0] if len(remaining) == 1 else {"anyOf": remaining}, True
    return schema, schema.get("type") == "null"


SUPPORT = """
/// An integer JSON token, preserving precision beyond machine integer ranges.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct Integer(serde_json::Number);

impl Default for Integer {
    fn default() -> Self { Self(0.into()) }
}
impl<'de> Deserialize<'de> for Integer {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = serde_json::Number::deserialize(deserializer)?;
        if value.to_string().contains(['.', 'e', 'E']) {
            return Err(serde::de::Error::custom("expected an integer"));
        }
        Ok(Self(value))
    }
}
impl std::fmt::Display for Integer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}
macro_rules! integer_from {
    ($($ty:ty),*) => {$(
        impl From<$ty> for Integer {
            fn from(value: $ty) -> Self { Self(value.into()) }
        }
    )*};
}
integer_from!(i32, i64, u32, u64);

// Serde otherwise treats a missing required Option as an explicit null.
#[allow(dead_code)]
fn required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where D: serde::Deserializer<'de>, T: Deserialize<'de> {
    Option::<T>::deserialize(deserializer)
}
#[allow(dead_code)]
fn present_nullable<'de, D, T>(deserializer: D) -> Result<Option<Option<T>>, D::Error>
where D: serde::Deserializer<'de>, T: Deserialize<'de> {
    Option::<T>::deserialize(deserializer).map(Some)
}
#[allow(dead_code)]
fn present_value<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where D: serde::Deserializer<'de>, T: Deserialize<'de> {
    T::deserialize(deserializer).map(Some)
}
"""


@dataclass
class WireField:
    name: str
    raw: str
    ty: str
    nullable: bool
    required: bool
    attrs: list[str]


class Emitter:
    def __init__(self) -> None:
        self.declarations: dict[str, str] = {}
        self.shapes: dict[str, str] = {}
        self.defaults: list[str] = []
        self.defs: dict[str, Any] = {}
        self.request = False
        self.active: set[str] = set()
        self.ref_names: dict[str, str] = {}

    def claim(self, name: str, schema: dict[str, Any]) -> bool:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in KEYWORDS:
            raise ValueError(f"invalid Rust type name {name}")
        shape = json.dumps({k: v for k, v in schema.items() if k != "$defs"}, sort_keys=True)
        if name in self.shapes:
            if self.shapes[name] != shape:
                raise ValueError(f"conflicting Rust type {name}")
            return False
        self.shapes[name] = shape
        return True

    def wire_type(self, schema: dict[str, Any], hint: str) -> str:
        if any(k in schema for k in ("oneOf", "allOf", "if", "then", "else", "not")):
            raise ValueError(f"{hint}: unsupported composition")
        if "$ref" in schema:
            ref = schema["$ref"]
            if not ref.startswith("#/$defs/") or ref[8:] not in self.defs:
                raise ValueError(f"{hint}: unresolved ref {ref}")
            key = ref[8:]
            name = self.ref_names.get(key, type_name(key))
            if name in self.active:
                raise ValueError(f"{hint}: recursive ref {ref}")
            self.model(name, self.defs[key], request=self.request)
            return name
        base, nullable = split_nullable(schema)
        if nullable and base != schema:
            return f"Option<{self.wire_type(base, hint)}>"
        choices = schema.get("anyOf")
        if choices:
            types = {c.get("type") for c in choices}
            if types == {"object", "array", "string", "integer", "number", "boolean"} and all(
                (
                    c.get("type") != "object"
                    or ("properties" not in c and c.get("additionalProperties", True) in (True, {}))
                )
                and (c.get("type") != "array" or c.get("items", {}) == {})
                for c in choices
            ):
                return "Value"
            if self.claim(hint, schema):
                self.declarations[hint] = self.union(choices, hint)
            return hint
        kind = schema.get("type")
        if "const" in schema and kind != "string":
            return self.constant(schema, hint)
        if "enum" in schema or "const" in schema:
            return self.enum(schema, hint)
        if kind in {"string", "boolean", "integer", "number", "null"}:
            return {
                "string": "String",
                "boolean": "bool",
                "integer": "Integer",
                "number": "f64",
                "null": "()",
            }[kind]
        if kind == "array":
            if "prefixItems" in schema:
                items = schema["prefixItems"]
                if schema.get("minItems") != len(items) or schema.get("maxItems") != len(items):
                    raise ValueError(f"{hint}: only fixed tuples are supported")
                types = [self.wire_type(s, hint + str(i)) for i, s in enumerate(items)]
                return "(" + ", ".join(types) + ",)"
            return f"Vec<{self.wire_type(schema.get('items', {}), hint + 'Item')}>"
        if kind == "object":
            if "properties" in schema:
                self.model(hint, schema, request=self.request)
                return hint
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise ValueError(f"{hint}: closed maps are unsupported")
            value = (
                self.wire_type(additional, hint + "Value")
                if isinstance(additional, dict)
                else "Value"
            )
            return f"std::collections::HashMap<String, {value}>"
        if not schema or set(schema) <= {"title", "description", "default", "examples"}:
            return "Value"
        raise ValueError(f"{hint}: unsupported schema {schema}")

    def constant(self, schema: dict[str, Any], hint: str) -> str:
        if self.claim(hint, schema):
            literal = rust_string(json.dumps(schema["const"], separators=(",", ":")))
            self.declarations[hint] = (
                "#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]\n"
                f"pub struct {hint};\n"
                f"impl Serialize for {hint} {{\n"
                "fn serialize<S: serde::Serializer>(&self, serializer: S)"
                " -> Result<S::Ok, S::Error> {\n"
                f'let value: Value = serde_json::from_str({literal}).expect("schema constant");\n'
                "value.serialize(serializer) } }\n"
                f"impl<'de> Deserialize<'de> for {hint} {{\n"
                "fn deserialize<D: serde::Deserializer<'de>>(deserializer: D)"
                " -> Result<Self, D::Error> {\n"
                "let value = Value::deserialize(deserializer)?;\n"
                f"let expected: Value = serde_json::from_str({literal})"
                '.expect("schema constant");\n'
                "if value != expected {"
                ' return Err(serde::de::Error::custom("unexpected constant")); }\n'
                "Ok(Self) } }"
            )
        return hint

    def enum(self, schema: dict[str, Any], hint: str) -> str:
        values = schema.get("enum", [schema.get("const")])
        if schema.get("type") != "string" or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{hint}: only string enums are supported")
        if self.claim(hint, schema):
            names = [pascal(v) if v else "Empty" for v in values]
            names = [("V" + n if n[0].isdigit() or n in KEYWORDS else n) for n in names]
            if len(set(names)) != len(names):
                raise ValueError(f"{hint}: enum variant collision")
            if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in names):
                raise ValueError(f"{hint}: invalid enum variant")
            variants = [
                f"#[serde(rename = {rust_string(v)})]\n{n},"
                for n, v in zip(names, values, strict=True)
            ]
            self.declarations[hint] = (
                "#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]\n"
                f"#[non_exhaustive]\npub enum {hint} {{\n" + "\n".join(variants) + "\n}"
            )
        return hint

    def field(
        self, model: str, wire: str, prop: dict[str, Any], *, required: bool, request: bool
    ) -> WireField:
        field = rust_ident(FIELD_NAMES.get((model, wire), wire))
        base, nullable = split_nullable(prop)
        override = FIELD_TYPES.get((model, wire))
        if override:
            expected = "string" if override == "String" else "integer"
            choices = base.get("anyOf", [base])
            if not all(choice.get("type") == expected for choice in choices):
                raise ValueError(f"{model}.{wire}: public type override disagrees with schema")
        # JSON value uses Value::Null directly, without an extra Option.
        raw = override or self.wire_type(base, model + pascal(wire))
        nullable = nullable and raw != "Value"
        optional = request and not required
        ty = f"Option<{raw}>" if nullable else raw
        if optional or (not request and not required and "default" not in prop and not nullable):
            ty = f"Option<{ty}>"
        attrs = [f"rename = {rust_string(wire)}"] if field != wire else []
        if optional:
            attrs += [
                "default",
                'skip_serializing_if = "Option::is_none"',
                f'deserialize_with = "{"present_nullable" if nullable else "present_value"}"',
            ]
        elif not required:
            attrs += [self.default_attr(prop, ty)]
        if required and nullable:
            attrs += ['deserialize_with = "required_nullable"']
        return WireField(field, raw, ty, nullable, required, attrs)

    def default_attr(self, prop: dict[str, Any], ty: str) -> str:
        if "default" not in prop:
            return "default"
        fn = "default_" + str(len(self.defaults))
        literal = json.dumps(prop["default"], separators=(",", ":"), ensure_ascii=False)
        self.defaults.append(
            f"fn {fn}() -> {ty} {{ serde_json::from_str("
            f'{rust_string(literal)}).expect("schema default") }}'
        )
        return f'default = "{fn}"'

    def request_field(
        self,
        field: WireField,
        names: set[str],
        args: list[str],
        initial: list[str],
        methods: list[str],
    ) -> None:
        name = field.name
        arg_type, convert = self.argument(field.raw, name)
        if field.required:
            if field.nullable:
                arg_type, convert = field.ty, name
            args.append(f"{name}: {arg_type}")
            initial.append(f"{name}," if name == convert else f"{name}: {convert},")
            return
        initial.append(f"{name}: None,")
        assignment = f"Some({convert})"
        if field.nullable:
            assignment = f"Some({assignment})"
        methods.append(
            f"pub fn {name}(mut self, {name}: {arg_type}) -> Self {{ "
            f"self.{name} = {assignment}; self }}"
        )
        if field.nullable:
            null_method = name.removeprefix("r#") + "_null"
            if null_method in names:
                raise ValueError(f"setter collision {null_method}")
            names.add(null_method)
            methods.append(
                f"pub fn {null_method}(mut self) -> Self {{ self.{name} = Some(None); self }}"
            )

    def model(
        self,
        name: str,
        schema: dict[str, Any],
        *,
        request: bool,
        operation: tuple[str, str] | None = None,
    ) -> None:
        if not self.claim(name, {**schema, "_rust_request": request}):
            return
        previous_request = self.request
        self.request = request
        self.active.add(name)
        required = set(schema.get("required", []))
        fields: list[str] = []
        initial: list[str] = []
        args: list[str] = []
        methods: list[str] = []
        names: set[str] = {"new", "send"} if request else set()
        wire_fields: list[WireField] = []
        for wire, prop in schema.get("properties", {}).items():
            is_required = wire in required and not (
                operation is not None and wire in {"org", "namespace", "agent"}
            )
            field = self.field(name, wire, prop, required=is_required, request=request)
            if field.name in names:
                raise ValueError(f"{name}: field collision {field.name}")
            names.add(field.name)
            if field.attrs:
                fields.append("#[serde(" + ", ".join(field.attrs) + ")]")
            fields.append(f"pub {field.name}: {field.ty},")
            if request:
                self.request_field(field, names, args, initial, methods)
                wire_fields.append(field)
        decl = (
            "#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]\n"
            f"#[non_exhaustive]\npub struct {name} {{\n" + "\n".join(fields) + "\n}"
        )
        if request:
            if len(args) > 7:
                group = name + "Required"
                required_fields = [field for field in wire_fields if field.required]
                if not self.claim(group, {"required_fields": [f.name for f in required_fields]}):
                    raise ValueError(f"{name}: required field group collision")
                self.declarations[group] = (
                    "/// Required fields for the request constructor.\n"
                    "#[derive(Debug, Clone, PartialEq)]\n"
                    f"pub struct {group} {{\n"
                    + "\n".join(f"pub {f.name}: {f.ty}," for f in required_fields)
                    + "\n}"
                )
                args = [f"required: {group}"]
                initial = [
                    f"{field.name}: required.{field.name},"
                    if field.required
                    else f"{field.name}: None,"
                    for field in wire_fields
                ]
            methods.insert(
                0, f"pub fn new({', '.join(args)}) -> Self {{ Self {{ " + " ".join(initial) + " } }"
            )
            if not args:
                decl += f"\nimpl Default for {name} {{ fn default() -> Self {{ Self::new() }} }}"
            if operation:
                op, output = operation
                body = f'client.call("{op}", &self, options).await'
                methods.append(
                    f"pub async fn send(self, client: &crate::Client, options: crate::CallOptions)"
                    f" -> Result<crate::CallResult<{output}>, crate::CallFailure> {{ {body} }}"
                )
            decl += f"\nimpl {name} {{\n" + "\n".join(methods) + "\n}"
        self.declarations[name] = decl
        self.active.remove(name)
        self.request = previous_request

    def union(self, choices: list[dict[str, Any]], hint: str) -> str:
        types = [self.wire_type(choice, hint + str(i)) for i, choice in enumerate(choices)]
        variants = "\n".join(f"Variant{i}({ty})," for i, ty in enumerate(types))
        attempts = "\n".join(
            f"if let Ok(value) = serde_json::from_value::<{ty}>(value.clone()) "
            f"{{ return Ok(Self::Variant{i}(value)); }}"
            for i, ty in enumerate(types)
        )
        # Serde's untagged buffer loses arbitrary-precision decimal JSON tokens.
        return (
            "#[derive(Debug, Clone, PartialEq, Serialize)]\n"
            f"#[serde(untagged)]\npub enum {hint} {{\n{variants}\n}}\n"
            f"impl<'de> Deserialize<'de> for {hint} {{\n"
            "fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) "
            "-> Result<Self, D::Error> {\n"
            "let value = serde_json::Value::deserialize(deserializer)?;\n"
            f'{attempts}\nErr(serde::de::Error::custom("no matching variant for {hint}"))\n'
            "}\n}"
        )

    @staticmethod
    def argument(ty: str, field: str) -> tuple[str, str]:
        if ty in {"String", "Value", "Integer"}:
            return f"impl Into<{ty}>", f"{field}.into()"
        if ty == "Vec<String>":
            return (
                "impl IntoIterator<Item = impl Into<String>>",
                f"{field}.into_iter().map(Into::into).collect()",
            )
        return ty, field


def rustfmt(text: str) -> str:
    return subprocess.run(
        ["rustfmt", "--edition", "2021", "--emit", "stdout"],
        input=text,
        text=True,
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout


def operation_facts() -> str:
    from server.code.api.models_common import _CodeIdentity
    from server.memory.api.models_common import _Identity
    from server.operations.registry import ALL_OPERATIONS
    from server.tasks.api.models_task import _Caller

    facts = []
    for op in ALL_OPERATIONS:
        keys = set(op.input_model.model_fields)
        identity = keys & {"org", "namespace"}
        fact = OpFacts.of(op)
        if issubclass(op.input_model, (_Identity, _CodeIdentity, _Caller)):
            identity |= keys & {"agent", "user_id", "session_id", "run_id"}
        # These models declare authorship directly rather than through a base.
        if op.name in {
            "memory.promote",
            "memory.verify",
            "memory.import_evidence",
            "memory.record.write",
            "knowledge.add",
            "knowledge.upsert",
            "knowledge.bulk_add",
            "knowledge.ingest",
            "knowledge.source.push_configure",
            "knowledge.synthesize",
            "decision.record",
            "decision.outcome",
            "observe.record",
            "observe.crystallize",
            "run.begin",
            "cost.record_turns",
            "procedure.add",
            "procedure.record_run",
            "procedure.evolve",
            "procedure.revise",
            "procedure.fork",
        } or op.name.startswith("conversation."):
            identity |= keys & {"agent", "user_id", "session_id"}
        if op.name in {
            "memory.set",
            "memory.pin",
            "memory.update",
            "memory.set_from_messages",
            "memory.bulk_set",
            "memory.link",
            "decision.record",
            "observe.record",
            "working_memory.set",
            "working_memory.append",
        }:
            identity |= keys & {"run_id"}
        if op.name in {"memory.search", "memory.search_trace", "memory.ask", "memory.list"}:
            identity.discard("run_id")
        if op.name in {"memory.ask", "memory.search", "memory.search_trace"}:
            identity -= {"agent", "user_id", "session_id"}
        if op.name == "memory.digests":
            identity.discard("session_id")
        defaults = set()
        if op.name.startswith("conversation.") and fact.read_only:
            identity -= {"agent", "user_id", "session_id"}
            defaults = {
                key
                for key in keys & {"agent", "user_id"}
                if op.input_model.model_fields[key].is_required()
            }
        fields = ", ".join(rust_string(key) for key in sorted(identity))
        default_fields = ", ".join(rust_string(key) for key in sorted(defaults))
        facts.append(
            f'("{op.name}", {str(fact.idempotent).lower()}, '
            f"{str(fact.read_only).lower()}, &[{fields}], &[{default_fields}]),"
        )
    return "\n".join(facts)


def selected_operations(selected: tuple[str, ...] | None = None) -> list[Any]:
    from server.operations.registry import ALL_OPERATIONS

    registry = {op.name: op for op in ALL_OPERATIONS}
    if selected is None:
        selected = tuple(registry)
    unknown = set(selected) - registry.keys()
    if unknown or not selected or len(set(selected)) != len(selected):
        raise ValueError(f"unknown or duplicate operations: {sorted(unknown) or selected}")
    return [registry[key] for key in selected]


def operation_type(op: Any, *, request: bool) -> str:
    from server.operations.registry import ALL_OPERATIONS

    model = op.input_model if request else op.output_model
    if request:
        owner = next(item for item in ALL_OPERATIONS if item.input_model is model)
        if owner.name != op.name:
            return pascal(op.name.replace(".", "_")) + "Input"
    return type_name(model.__name__)


def build(selected: tuple[str, ...] | None = None) -> str:
    em = Emitter()
    operations = selected_operations(selected)
    roots = {operation_type(op, request=request) for op in operations for request in (False, True)}
    # Preserve public convenience names while qualifying conflicting nested shapes.
    operations.sort(key=lambda op: (op.name not in CONVENIENCE_OPS, op.name))
    for op in operations:
        key = op.name
        for request, model in [(False, op.output_model), (True, op.input_model)]:
            schema = schema_for(model)
            em.defs = schema.get("$defs", {})
            root = operation_type(op, request=request)
            em.ref_names = {}
            for ref, definition in em.defs.items():
                name = type_name(ref)
                shape = json.dumps(
                    {
                        k: v
                        for k, v in {**definition, "_rust_request": request}.items()
                        if k != "$defs"
                    },
                    sort_keys=True,
                )
                if (name in roots and name != root) or (
                    name in em.shapes and em.shapes[name] != shape
                ):
                    name = root + type_name(ref)
                em.ref_names[ref] = name
            em.model(
                root,
                schema,
                request=request,
                operation=(key, operation_type(op, request=False)) if request else None,
            )
    # Helpers are shared even when a selection happens not to need one of them.
    text = (
        f"// Generated by {COMMAND}; do not edit.\n"
        "use serde::{Deserialize, Serialize};\nuse serde_json::Value;\n"
        + SUPPORT
        + "\npub(crate) type OperationPolicy = "
        "(&'static str, bool, bool, &'static [&'static str], &'static [&'static str]);\n"
        + "\npub(crate) const OP_POLICIES: &[OperationPolicy] = &[\n"
        + operation_facts()
        + "\n];\n"
        + "\n\n".join(em.declarations.values())
        + "\n"
        + "\n".join(em.defaults)
        + "\n"
    )
    return rustfmt(text)


def build_client(*, blocking: bool = False, selected: tuple[str, ...] | None = None) -> str:
    prefix = "" if blocking else "async "
    wait = "" if blocking else ".await"
    target = "crate::blocking::Client" if blocking else "crate::Client"
    methods = []
    for op in selected_operations(selected):
        method = rust_ident(op.name.replace(".", "_"))
        request = "crate::" + operation_type(op, request=True)
        response = "crate::" + operation_type(op, request=False)
        methods.append(
            f"/// Call `{op.name}`.\n"
            f"pub {prefix}fn {method}(&self, request: {request})"
            f" -> Result<{response}, crate::Error> {{\n"
            f"self.{method}_with_options(request, crate::CallOptions::default()){wait}"
            ".map(|result| result.data).map_err(|failure| failure.error) }\n"
            f"/// Call `{op.name}` with per-request options and metadata.\n"
            f"pub {prefix}fn {method}_with_options(&self, request: {request},"
            " options: crate::CallOptions)"
            f" -> Result<crate::CallResult<{response}>, crate::CallFailure> {{\n"
            f"self.call({rust_string(op.name)}, &request, options){wait} }}"
        )
    return rustfmt(
        f"// Generated by {COMMAND}; do not edit.\nimpl {target} {{\n"
        + "\n".join(methods)
        + "\n}\n"
    )


def schema_sample(schema: dict[str, Any], definitions: dict[str, Any]) -> Any:
    """Build a minimal JSON value for the serde contract, without model execution."""
    if "$ref" in schema:
        return schema_sample(definitions[schema["$ref"][8:]], definitions)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return schema_sample(schema["anyOf"][0], definitions)
    kind = schema.get("type")
    if kind == "object" and "properties" in schema:
        return {
            key: schema_sample(prop, definitions)
            for key, prop in schema["properties"].items()
            if key in schema.get("required", [])
        }
    if kind == "array":
        if "prefixItems" in schema:
            return [schema_sample(prop, definitions) for prop in schema["prefixItems"]]
        return []
    return {"string": "sample", "integer": 1, "number": 1.0, "boolean": False, "null": None}.get(
        kind, {}
    )


def build_contract_tests(selected: tuple[str, ...] | None = None) -> str:
    tests = []
    for op in selected_operations(selected):
        lines = []
        for request, model in [(True, op.input_model), (False, op.output_model)]:
            schema = schema_for(model)
            payload = json.dumps(
                schema_sample(schema, schema.get("$defs", {})), separators=(",", ":")
            )
            name = operation_type(op, request=request)
            lines.append(
                f"let value: clickhouse_loom::{name} ="
                f" serde_json::from_str({rust_string(payload)}).unwrap();\n"
                "let encoded = serde_json::to_value(value).unwrap();\n"
                "assert!(encoded.is_object());"
            )
            if not request:
                for key, prop in schema["properties"].items():
                    if key not in schema.get("required", []) and "default" in prop:
                        value = prop["default"]
                        # JSON Schema may describe a float default with an integer token.
                        if split_nullable(prop)[0].get("type") == "number" and value is not None:
                            value = float(value)
                        default = rust_string(json.dumps(value, separators=(",", ":")))
                        lines.append(
                            f"assert_eq!(encoded[{rust_string(key)}], "
                            f"serde_json::from_str::<serde_json::Value>({default}).unwrap(), "
                            f"{rust_string(op.name + ':' + key)});"
                        )
        tests.append(f"#[test]\nfn {op.name.replace('.', '_')}() {{\n" + "\n".join(lines) + "\n}")
    return rustfmt(f"// Generated by {COMMAND}; do not edit.\n" + "\n".join(tests) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--fixtures", action="store_true", help="generate schema mapping test cases"
    )
    selection.add_argument("--ops", nargs="+")
    args = parser.parse_args(argv)
    if args.ops is not None and args.out is None:
        parser.error("--ops requires --out; SDK artifacts always cover the full registry")
    if args.fixtures:
        from scripts.codegen.rust_schema_fixtures import build_fixture

        target = args.out or ROOT / "sdk/rust/tests/schema_mapping.rs"
        return GeneratedFile(target, COMMAND + " --fixtures").sync(
            build_fixture(), check=args.check
        )
    selected = tuple(args.ops) if args.ops is not None else None
    artifacts = [(args.out or TARGET, build(selected))]
    if args.out is None:
        artifacts += [
            (ROOT / "sdk/rust/src/operations.rs", build_client(selected=selected)),
            (
                ROOT / "sdk/rust/src/blocking_operations.rs",
                build_client(blocking=True, selected=selected),
            ),
            (ROOT / "sdk/rust/tests/registry_contracts.rs", build_contract_tests(selected)),
        ]
    return max(
        GeneratedFile(path, COMMAND).sync(text, check=args.check) for path, text in artifacts
    )


if __name__ == "__main__":
    raise SystemExit(main())
