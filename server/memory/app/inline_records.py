"""Inline record_schema projection for memory.set_from_messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from server.memory.api.models_records import (
    MemoryRecordWriteInput,
    MemorySchemaRegisterInput,
    RecordFieldInput,
    SchemaFieldInput,
    schema_field_type,
)

if TYPE_CHECKING:
    from server.memory.app.extract.models import RecordValue


async def _write_inline_record_projection(
    input: Any,
    record: Mapping[str, RecordValue],
    schema: Mapping[str, object],
    *,
    evidence_ids: list[str],
) -> None:
    """Project ad-hoc record_schema extraction into memory_records."""
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import request_read_narrowed

    # memory_records is keyed without a user: a caller bounded to one user would
    # fold into or supersede another user's record of the same key.
    if request_read_narrowed(current_auth()):
        return

    from server.memory.app.records import (
        handle_memory_record_write,
        handle_memory_schema_register,
    )

    schema_name = str(input.memory_key or "inline_record")
    fields = [
        SchemaFieldInput(
            name=str(name),
            # An enum (a list of allowed values) is a string field; a type
            # name is one the extractor's schema check already accepted.
            type=("string" if isinstance(spec, list) else schema_field_type(str(spec))),
            # Inferred schema, not a contract: mark nothing required, or an
            # omitted field would 400 the projection at the write-side gate.
            required=False,
            enum_values=list(spec) if isinstance(spec, list) else [],
        )
        for name, spec in schema.items()
    ]
    try:
        await handle_memory_schema_register(
            MemorySchemaRegisterInput(
                org=input.org,
                namespace=input.namespace,
                name=schema_name,
                description="inline record_schema from memory.set_from_messages",
                fields=fields,
                created_by=input.agent,
            )
        )
    except Exception:  # noqa: BLE001 — an existing schema is fine; the write uses the latest
        # Pre-existing schema for this key is fine; the write below uses the
        # latest active schema.
        pass
    evidence_id = evidence_ids[0] if evidence_ids else None
    await handle_memory_record_write(
        MemoryRecordWriteInput(
            org=input.org,
            namespace=input.namespace,
            schema_name=schema_name,
            memory_key=schema_name,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            fields={
                str(name): RecordFieldInput(
                    value=value,
                    state="unknown" if value in ("", None) else "value",
                    evidence_id=evidence_id,
                )
                for name, value in record.items()
                # Only schema-declared fields; the write-side gate rejects
                # undeclared ones, and an off-schema key shouldn't fail it all.
                if str(name) in schema
            },
            valid_at=getattr(input, "valid_at", None),
        )
    )
