"""code.index_status carries a capability catalogue: the build commit and every
served code.* operation with the request fields its input model accepts, so a
downstream client stops probing for a route with a 404."""

from __future__ import annotations

from server.code.api.models_status import CodeIndexStatusOutput
from server.code.app.index_status import code_op_catalogue
from server.operations.registry import ALL_OPERATIONS


def test_catalogue_lists_every_served_code_op_with_its_request_fields() -> None:
    ops, fields, maturity, reach, toolsets = code_op_catalogue()
    registry = {op.name for op in ALL_OPERATIONS if op.name.startswith("code.")}
    assert set(ops) == registry and ops == sorted(ops)
    assert set(fields) == registry
    # the additive fields a client guards on are discoverable
    assert {"pattern", "path_globs", "langs", "cursor", "exclude_unit_ids"} <= set(
        fields["code.search"]
    )
    assert "neighbours" in fields["code.enclosing"]
    assert all(f == sorted(f) for f in fields.values())
    # additive: maturity and reach per served op
    assert set(maturity) == registry and set(reach) == registry
    assert set(maturity.values()) <= {"stable", "experimental"}
    assert "mcp_core" in reach["code.search"] and maturity["code.search"] == "stable"
    assert reach["code.reindex"] == ["http_only"] and maturity["code.reindex"] == "experimental"
    # additive: the toolsets whose listing carries each op
    assert set(toolsets) == registry
    assert (
        toolsets["code.search"] == ["all", "assistant", "coding", "core"]
        and toolsets["code.reindex"] == []
    )


def test_output_model_defaults_keep_old_clients_whole() -> None:
    out = CodeIndexStatusOutput(repo="r", branch="b", namespace="n")
    assert out.build_commit == "" and out.ops == [] and out.request_fields == {}
    assert out.op_maturity == {} and out.op_reach == {} and out.op_toolsets == {}
