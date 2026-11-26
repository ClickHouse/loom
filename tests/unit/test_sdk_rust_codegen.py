"""Rust selection and schema errors must be detected before replacing an artifact."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

import scripts.codegen.rust_schema_fixtures as fixtures
import scripts.codegen.sdk_rust_gen as gen
from scripts.codegen.sdk_rust_gen import CONVENIENCE_OPS, MEMORY_OPS


class Child(BaseModel):
    items: list[str] = Field(default_factory=list)


class Parent(BaseModel):
    child: Child
    labels: dict[str, int] = Field(default_factory=dict)


def test_collection_factory_defaults_include_referenced_models() -> None:
    schema = gen.schema_for(Parent)
    assert schema["properties"]["labels"]["default"] == {}
    assert schema["$defs"]["Child"]["properties"]["items"]["default"] == []


def test_projection_and_secret_metadata_reach_nested_models() -> None:
    from server.knowledge.api.models_source_push import PushStatusOutput
    from server.memory.api.models_crud import MemoryListOutput

    schema = gen.schema_for(MemoryListOutput)
    assert schema["$defs"]["_MemoryListEntry"]["_rust_projected"]
    secret = gen.schema_for(PushStatusOutput)
    assert secret["properties"]["verification_token"]["_rust_secret"]


@pytest.mark.parametrize(
    "name",
    ["namespace.delete", "namespace.rollback", "collection.move", "collection.delete"],
)
def test_destructive_namespace_operations_require_target_namespace(name: str) -> None:
    op = gen.selected_operations((name,))[0]
    model = gen.operation_type(op, request=True)
    emitter = gen.Emitter()
    emitter.model(
        model,
        gen.schema_for(op.input_model),
        request=True,
        operation=(name, gen.operation_type(op, request=False)),
    )
    assert "pub fn new(namespace: impl Into<String>" in emitter.declarations[model]


def test_reader_filters_and_collection_targets_are_not_stamped() -> None:
    facts = gen.operation_facts()
    for op in (
        "namespace.delete",
        "namespace.rollback",
        "collection.create",
        "collection.move",
        "collection.delete",
    ):
        target = next(line for line in facts.splitlines() if line.startswith(f'("{op}"'))
        assert '&["org"], &[]' in target
    listing = next(line for line in facts.splitlines() if line.startswith('("memory.list"'))
    assert '&["namespace", "org"], &["agent", "session_id", "user_id"]' in listing
    extraction = next(
        line for line in facts.splitlines() if line.startswith('("memory.extraction_status"')
    )
    assert '&["namespace", "org"], &[]' in extraction


def test_namespace_targets_and_nullable_rollups_have_distinct_defaults() -> None:
    from scripts.codegen.rust_identity import NAMESPACE_FILTERS
    from server.operations.registry import ALL_OPERATIONS

    facts = gen.operation_facts().splitlines()
    for op in ALL_OPERATIONS:
        if "namespace" not in op.input_model.model_fields:
            continue
        if not op.name.startswith("namespace.") and op.name not in NAMESPACE_FILTERS:
            continue
        line = next(line for line in facts if line.startswith(f'("{op.name}",'))
        stamped = line.split("&[", 1)[1].split("]", 1)[0]
        assert '"namespace"' not in stamped, op.name
        if op.name in NAMESPACE_FILTERS:
            assert line.endswith("&[]),"), op.name
        elif (
            op.name not in {"namespace.delete", "namespace.rollback"}
            and op.input_model.model_fields["namespace"].is_required()
        ):
            assert line.endswith('&["namespace"]),'), op.name


def test_memory_selection_fields_are_defaults_instead_of_overrides() -> None:
    from scripts.codegen.rust_identity import PROCEDURE_TARGETS, TARGETED_MEMORY

    facts = gen.operation_facts().splitlines()
    for name in TARGETED_MEMORY | PROCEDURE_TARGETS:
        line = next(line for line in facts if line.startswith(f'("{name}",'))
        assert '&["namespace", "org"], &[' in line, name
    for name in ("memory.recall", "memory.brief"):
        line = next(line for line in facts if line.startswith(f'("{name}",'))
        assert '&["namespace", "org"], &["agent"]' in line


def test_unclassified_memory_operations_and_namespace_filters_refuse_generation() -> None:
    from types import SimpleNamespace

    from scripts.codegen.rust_identity import validate_roles
    from server.memory.api.models_common import _Identity
    from server.operations.registry import ALL_OPERATIONS

    with pytest.raises(ValueError, match="memory identity census changed"):
        validate_roles(
            [*ALL_OPERATIONS, SimpleNamespace(name="memory.unclassified", input_model=_Identity)]
        )

    class Rollup(BaseModel):
        org: str
        namespace: str | None = None

    with pytest.raises(ValueError, match="namespace filter census changed"):
        validate_roles(
            [*ALL_OPERATIONS, SimpleNamespace(name="cost.unclassified", input_model=Rollup)]
        )


def test_unknown_factory_is_refused_without_executing_it() -> None:
    def refuse() -> list[str]:
        raise AssertionError("must not execute arbitrary factories")

    class Model(BaseModel):
        items: list[str] = Field(default_factory=refuse)

    with pytest.raises(ValueError, match="unsupported default factory"):
        gen.schema_for(Model)


def test_data_dependent_defaults_are_omitted_only_in_requests() -> None:
    def refuse(data: dict[str, Any]) -> bool:
        raise AssertionError("must not execute arbitrary factories")

    class Model(BaseModel):
        mode: str = "matches"
        include_body: bool = Field(default_factory=refuse)

    schema = gen.schema_for(Model, request=True)
    assert "default" not in schema["properties"]["include_body"]
    assert "include_body" not in schema.get("required", [])
    with pytest.raises(ValueError, match="unsupported default factory"):
        gen.schema_for(Model)


@pytest.mark.parametrize("mode", ["matches", "files", "presence", "message"])
def test_grep_dynamic_default_preserves_omission_and_explicit_values(mode: str) -> None:
    from server.code.api.models_grep import CodeGrepInput

    schema = gen.schema_for(CodeGrepInput, request=True)
    assert "default" not in schema["properties"]["include_body"]
    base = {"org": "org", "namespace": "ns", "pattern": "needle", "mode": mode}
    assert CodeGrepInput.model_validate(base).include_body == (mode == "matches")
    for value in (True, False):
        assert CodeGrepInput.model_validate({**base, "include_body": value}).include_body == value


@pytest.mark.parametrize(
    ("name", "targets"),
    [
        ("memory.promote", {"agent", "user_id", "session_id"}),
        ("memory.verify", {"agent", "user_id", "session_id"}),
        ("conversation.update", {"agent"}),
        ("conversation.delete", {"agent"}),
        ("conversation.compact", {"agent"}),
        ("conversation.unsave_message", {"agent", "user_id"}),
    ],
)
def test_source_and_conversation_mutation_targets_are_defaulted(
    name: str, targets: set[str]
) -> None:
    op = gen.selected_operations((name,))[0]
    stamped, defaults = gen.operation_identity_fields(op)
    assert stamped == {"org", "namespace"}
    assert defaults == targets


@pytest.mark.parametrize(
    ("name", "expected_stamped", "expected_defaults"),
    [
        (
            "procedure.record_run",
            {"org", "namespace"},
            {"agent", "user_id", "session_id"},
        ),
        (
            "conversation.create",
            {"org", "namespace", "agent", "user_id", "run_id"},
            set(),
        ),
        (
            "conversation.append",
            {"org", "namespace", "agent", "user_id", "run_id"},
            set(),
        ),
    ],
)
def test_recording_policy_distinguishes_procedure_targets_from_conversation_authorship(
    name: str, expected_stamped: set[str], expected_defaults: set[str]
) -> None:
    op = gen.selected_operations((name,))[0]
    stamped, defaults = gen.operation_identity_fields(op)
    assert (stamped, defaults) == (expected_stamped, expected_defaults)


@pytest.mark.parametrize(
    "schema",
    [
        {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        {"allOf": [{"type": "string"}]},
        {"$ref": "#/$defs/Missing"},
        {"type": "object", "additionalProperties": False},
        {"type": "array", "prefixItems": [{"type": "string"}]},
        {"type": "string", "enum": ["foo-bar", "foo_bar"]},
    ],
)
def test_unsupported_or_ambiguous_schema_is_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        gen.Emitter().wire_type(schema, "Contract")


def test_required_nullable_default_and_keyword_fields() -> None:
    em = gen.Emitter()
    em.model(
        "Contract",
        {
            "properties": {
                "type": {"type": "string"},
                "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "count": {"type": "integer", "default": 5},
            },
            "required": ["type", "maybe"],
        },
        request=False,
    )
    text = em.declarations["Contract"]
    assert 'rename = "type"' in text
    assert "pub r#type: String" in text
    assert 'deserialize_with = "required_nullable"' in text
    assert "pub maybe: Option<String>" in text
    assert 'default = "default_0"' in text
    assert 'from_str("5")' in em.defaults[0]


def test_type_field_and_method_collisions_are_refused() -> None:
    em = gen.Emitter()
    em.wire_type({"type": "string", "enum": ["yes"]}, "Label")
    with pytest.raises(ValueError, match="conflicting Rust type"):
        em.wire_type({"type": "string", "enum": ["no"]}, "Label")
    for fields in [
        {"a-b": {}, "a_b": {}},
        {"maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "maybe_null": {}},
        {"send": {}},
    ]:
        with pytest.raises(ValueError, match="collision"):
            gen.Emitter().model("Request", {"properties": fields}, request=True)


def test_recursive_ref_is_refused() -> None:
    em = gen.Emitter()
    em.defs = {"Node": {"properties": {"child": {"$ref": "#/$defs/Node"}}}}
    with pytest.raises(ValueError, match="recursive ref"):
        em.wire_type({"$ref": "#/$defs/Node"}, "Root")


def test_string_literals_use_rust_escapes() -> None:
    assert gen.rust_string('é\n"\x08') == '"é\\n\\"\\u{8}"'


def test_structured_unions_keep_their_typed_members() -> None:
    em = gen.Emitter()
    choices = [
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        {"type": "array", "items": {"type": "string"}},
        *({"type": kind} for kind in ("string", "integer", "number", "boolean")),
    ]
    assert em.wire_type({"anyOf": choices}, "Union") == "Union"
    assert "pub name: String" in em.declarations["Union0"]
    assert "Variant1(Vec<String>)" in em.declarations["Union"]


@pytest.mark.parametrize(
    ("prop", "ty"),
    [
        ({"default": False}, "Option<bool>"),
        ({"default": 0}, "Option<Integer>"),
        ({"default": ""}, "Option<String>"),
        ({"default": []}, "Option<Vec<String>>"),
        ({"default": {}}, "Option<std::collections::HashMap<String, String>>"),
        ({"default": ""}, "Label"),
    ],
)
def test_zero_defaults_use_the_declared_wire_value(prop: dict[str, Any], ty: str) -> None:
    em = gen.Emitter()
    assert em.default_attr(prop, ty) == 'default = "default_0"'
    assert f"fn default_0() -> {ty}" in em.defaults[0]


def test_generated_fixture_has_its_own_check_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gen, "rustfmt", lambda text: text)
    monkeypatch.setattr(fixtures, "rustfmt", lambda text: text)
    target = tmp_path / "fixtures.rs"
    args = ["--fixtures", "--out", str(target)]
    assert gen.main(args) == 0
    assert "--fixtures; do not edit" in target.read_text()
    assert "required_nullable_and_union_contract" in target.read_text()
    assert gen.main([*args, "--check"]) == 0
    target.write_text("stale")
    assert gen.main([*args, "--check"]) == 1
    assert target.read_text() == "stale"


def test_subset_generation_requires_a_separate_output() -> None:
    with pytest.raises(SystemExit, match="2"):
        gen.main(["--ops", "memory.get"])


def test_every_registry_request_response_field_and_method_is_emitted(monkeypatch) -> None:
    from server.operations.registry import ALL_OPERATIONS

    monkeypatch.setattr(gen, "rustfmt", lambda text: text)
    source = gen.build()
    clients = [gen.build_client(), gen.build_client(blocking=True)]
    for op in ALL_OPERATIONS:
        for request, model in [(True, op.input_model), (False, op.output_model)]:
            name = gen.operation_type(op, request=request)
            declaration = source.split(f"pub struct {name} {{", 1)[1].split("\n}", 1)[0]
            for wire in model.model_json_schema()["properties"]:
                field = gen.rust_ident(gen.FIELD_NAMES.get((name, wire), wire))
                assert f"pub {field}:" in declaration, f"{op.name}: {wire}"
        request = gen.operation_type(op, request=True)
        response = gen.operation_type(op, request=False)
        for client in clients:
            method = op.name.replace(".", "_")
            assert f"fn {method}(&self, request: crate::{request})" in client
            assert f"Result<crate::{response}, crate::Error>" in client
            assert f"fn {method}_with_options(" in client
            assert f'self.call("{op.name}", &request, options)' in client
        send = source.split(f"impl {request} {{", 1)[1].split("\n}", 1)[0]
        assert f'client.call("{op.name}", &self, options)' in send


def test_selected_operations_and_check_never_overwrite_stale_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Formatting is checked in the Rust CI job; this test needs no toolchain.
    monkeypatch.setattr(gen, "rustfmt", lambda text: text)
    text = gen.build()
    assert "pub struct MemorySetInput" in text
    assert "pub struct MemoryGetInput" in text
    for public in (
        "SetRequest",
        "SetResponse",
        "SearchRequest",
        "SearchHit",
        "SearchResponse",
        "GetResponse",
        "DeleteResponse",
    ):
        assert f"pub struct {public} {{" not in text
    assert "pub struct KnowledgeAddInput" in text
    assert text == gen.build()
    preview = gen.build(MEMORY_OPS)
    assert "pub struct KnowledgeAddInput" not in preview
    preview = gen.build(CONVENIENCE_OPS)
    assert "pub struct KnowledgeAddInput" in preview
    assert "pub struct ContextPackInput" in preview
    assert "pub struct OpsCapabilitiesOutput" in preview
    output = tmp_path / "contracts.rs"
    assert gen.main(["--out", str(output)]) == 0
    assert gen.main(["--out", str(output), "--check"]) == 0
    output.write_text("stale")
    assert gen.main(["--out", str(output), "--check"]) == 1
    assert output.read_text() == "stale"
    for selection in [("missing.op",), ("memory.set", "memory.set"), ()]:
        with pytest.raises(ValueError, match="unknown or duplicate"):
            gen.build(selection)
    assert gen.main(["--out", str(output)]) == 0


def test_operation_policies_cover_registry_and_distinguish_filters() -> None:
    from server.operations.registry import ALL_OPERATIONS

    facts = gen.operation_facts()
    for op in ALL_OPERATIONS:
        assert f'("{op.name}", {str(op.idempotent).lower()}, {str(op.read_only).lower()},' in facts
    assert '("memory.search", true, true, &["namespace", "org"], &[])' in facts
    assert '("memory.update", false, false, &["namespace", "org", "run_id"], &[])' in facts
    assert (
        '("knowledge.add", true, false, &["agent", "namespace", "org", "session_id"], &[])' in facts
    )
    assert '("ops.capabilities", true, true, &["org"], &[])' in facts
    assert '("run.get", true, true, &["namespace", "org"], &[])' in facts
    assert '("conversation.search", true, true, &["namespace", "org"], &["agent"])' in facts
    assert '("conversation.threads", true, true, &["namespace", "org"], &[])' in facts
    assert (
        '("conversation.search_saved_messages", true, true, &["namespace", "org"], '
        '&["agent", "user_id"])'
    ) in facts
    for op in ("run.get", "run.complete", "memory.ask", "memory.search", "memory.list"):
        line = next(line for line in facts.splitlines() if line.startswith(f'("{op}",'))
        assert '"run_id"' not in line
    for op, expected in [
        ("observe.record", {"agent", "user_id", "session_id", "run_id"}),
        ("knowledge.upsert", {"agent", "session_id"}),
        ("run.begin", {"agent", "user_id", "session_id"}),
    ]:
        line = next(line for line in facts.splitlines() if line.startswith(f'("{op}",'))
        assert all(f'"{field}"' in line for field in expected)


def test_digest_session_is_a_filter_and_decision_run_is_authorship() -> None:
    facts = gen.operation_facts()
    digest = next(line for line in facts.splitlines() if line.startswith('("memory.digests",'))
    decision = next(line for line in facts.splitlines() if line.startswith('("decision.record",'))
    assert '"session_id"' not in digest
    assert '"run_id"' in decision


def test_registered_factory_defaults_match_server_values() -> None:
    from server.operations.registry import ALL_OPERATIONS

    checked = set()
    for op in ALL_OPERATIONS:
        for request, model in ((True, op.input_model), (False, op.output_model)):
            schema = gen.schema_for(model, request=request)
            for field, prop in schema.get("properties", {}).items():
                info = model.model_fields[field]
                factory = info.default_factory
                if factory is not None:
                    if info.default_factory_takes_validated_data:
                        assert request and "default" not in prop
                        continue
                    value = factory()
                    if isinstance(value, BaseModel):
                        value = value.model_dump(mode="json")
                    assert prop["default"] == value
                    checked.add((model.__name__, field))
    assert set(gen.factory_defaults()) <= checked | {
        ("InspectSettings", "extraction_instructions_refs")
    }
