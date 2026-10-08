"""`mcp_schemas.model_schema` generates an op model's JSON Schema once and
hands out copies: the tools/list build reads it for every listed op on every
request, and a caller that edits its copy must not change the next listing."""

from __future__ import annotations

from pydantic import BaseModel, Field

from server.transport import mcp_schemas


class _Probe(BaseModel):
    name: str = Field(default="", max_length=8)
    tags: list[str] = Field(default_factory=list)


def test_model_schema_equals_pydantic_and_is_generated_once() -> None:
    class _Once(BaseModel):
        n: int = 0

    misses = mcp_schemas._generated_schema.cache_info().misses
    first = mcp_schemas.model_schema(_Once)
    second = mcp_schemas.model_schema(_Once)
    assert first == second == _Once.model_json_schema()
    assert mcp_schemas._generated_schema.cache_info().misses == misses + 1


def test_a_mutated_copy_does_not_reach_the_next_caller() -> None:
    got = mcp_schemas.model_schema(_Probe)
    got["properties"]["name"]["maxLength"] = 999
    got["properties"].pop("tags")
    again = mcp_schemas.model_schema(_Probe)
    assert again["properties"]["name"]["maxLength"] == 8
    assert "tags" in again["properties"]
