"""`memory_schema.propose` registers the writer's schema as `proposed`, for an
admin to accept or reject."""

from __future__ import annotations

import asyncio

import pytest

from server.memory.api.models_records import (
    MemorySchemaRegisterInput,
    MemorySchemaRegisterOutput,
    SchemaFieldInput,
)
from server.memory.app import records as h


def test_propose_registers_the_schema_as_proposed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[MemorySchemaRegisterInput] = []

    async def fake_register(inp: MemorySchemaRegisterInput) -> MemorySchemaRegisterOutput:
        seen.append(inp)
        return MemorySchemaRegisterOutput(
            schema_id="s", name=inp.name, field_count=len(inp.fields), status=inp.status
        )

    async def no_schema(*_a: object) -> None:
        return None

    monkeypatch.setattr(h, "_schema_by_name", no_schema)
    monkeypatch.setattr(h, "handle_memory_schema_register", fake_register)
    out = asyncio.run(
        h.handle_memory_schema_propose(
            MemorySchemaRegisterInput(
                org="o",
                namespace="n",
                name="deploy",
                fields=[SchemaFieldInput(name="service", type="string")],
            )
        )
    )
    assert [i.status for i in seen] == ["proposed"]
    assert out.status == "proposed" and out.name == "deploy"


def test_propose_refuses_the_name_of_an_active_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposal under an active schema's name would replace it (one row per name
    under ReplacingMergeTree); a writer's proposal never demotes an admin's schema."""
    from fastapi import HTTPException

    async def active(*_a: object) -> dict[str, str]:
        return {"schema_id": "s1", "name": "deploy", "status": "active"}

    async def never(inp: MemorySchemaRegisterInput) -> MemorySchemaRegisterOutput:
        raise AssertionError("nothing is written")

    monkeypatch.setattr(h, "_schema_by_name", active)
    monkeypatch.setattr(h, "handle_memory_schema_register", never)
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            h.handle_memory_schema_propose(
                MemorySchemaRegisterInput(
                    org="o",
                    namespace="n",
                    name="deploy",
                    fields=[SchemaFieldInput(name="service", type="string")],
                )
            )
        )
    assert e.value.status_code == 409 and e.value.detail["code"] == "SCHEMA_ACTIVE"
