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


def test_collection_delete_requires_target_namespace() -> None:
    from server.identity.api.models_collections import CollectionDeleteInput

    emitter = gen.Emitter()
    emitter.model(
        "CollectionDeleteInput",
        gen.schema_for(CollectionDeleteInput),
        request=True,
        operation=("collection.delete", "CollectionDeleteOutput"),
    )
    assert (
        "pub fn new(namespace: impl Into<String>)" in emitter.declarations["CollectionDeleteInput"]
    )


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


def test_unknown_factory_is_refused_without_executing_it() -> None:
    def refuse() -> list[str]:
        raise AssertionError("must not execute arbitrary factories")

    class Model(BaseModel):
        items: list[str] = Field(default_factory=refuse)

    with pytest.raises(ValueError, match="unsupported default factory"):
        gen.schema_for(Model)


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
        for model in (op.input_model, op.output_model):
            schema = gen.schema_for(model)
            for field, prop in schema.get("properties", {}).items():
                factory = model.model_fields[field].default_factory
                if factory is not None:
                    value = factory()
                    if isinstance(value, BaseModel):
                        value = value.model_dump(mode="json")
                    assert prop["default"] == value
                    checked.add((model.__name__, field))
    assert set(gen.factory_defaults()) <= checked | {
        ("InspectSettings", "extraction_instructions_refs")
    }
