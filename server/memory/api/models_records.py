"""Schema-grounded memory records.

These ops sit beside free-form `memory.*`: schemas define flat, agent-facing
record contracts; records store exact fields with explicit value/unknown/
rejected/absent states so reads can answer predicate/state questions without
re-parsing prose. Handlers live in `server/memory/app/records.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SchemaFieldType = Literal["string", "number", "integer", "boolean"]


def schema_field_type(name: str) -> SchemaFieldType:
    """`name` as the field type the schema ops accept; ValueError for any other."""
    match name:
        case "string" | "number" | "integer" | "boolean":
            return name
    raise ValueError(f"unsupported schema field type {name!r}")


class SchemaFieldInput(BaseModel):
    name: str
    type: SchemaFieldType = "string"
    required: bool = False
    # Identity fields derive the record's memory_key when the caller supplies
    # none: blake2b over their canonicalised values, per field kind
    # (memory/domain/record_identity.py). An identity field is implicitly
    # required with a concrete value; a record missing one is rejected, never
    # repaired.
    identity: bool = False
    enum_values: list[str] = Field(default_factory=list)
    # Constraints enforced at memory.record.write. Strings:
    # `pattern` (re.search, Python syntax), `min_length` / `max_length`.
    # Numbers and integers: `min_value` / `max_value` (inclusive). A
    # declaration on the wrong type, an uncompilable pattern, or inverted
    # bounds is rejected at register.
    pattern: str = ""
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


class MemorySchemaRegisterInput(BaseModel):
    org: str
    namespace: str
    # The schema's name; memory.record.write and queries address it by this name.
    name: str
    # Free text stored with the schema and returned by memory_schema.list.
    description: str = ""
    # The field contracts (1–64); a bad type, constraint or pattern is rejected at register.
    fields: list[SchemaFieldInput] = Field(min_length=1, max_length=64)
    # Author fallback, stored only when the request has no principal to stamp.
    created_by: str = ""
    # `active` is writable at once; `proposed` waits for memory_schema.approve.
    status: Literal["active", "proposed"] = "active"


class MemorySchemaRegisterOutput(BaseModel):
    # The new schema's id; every register call mints a fresh one.
    schema_id: str
    # The schema name, echoed.
    name: str
    # How many fields the schema declares.
    field_count: int
    # The status it was stored with: `active` or `proposed`.
    status: str


class MemorySchemaListInput(BaseModel):
    org: str
    namespace: str
    # False lists `active` schemas only; True lists every non-tombstoned status.
    include_proposed: bool = False
    # At most this many schemas (1–500), ordered by name.
    limit: int = Field(default=100, ge=1, le=500)


class SchemaFieldOutput(BaseModel):
    name: str
    type: str
    required: bool
    identity: bool = False
    enum_values: list[str] = Field(default_factory=list)
    # The stored constraints object; {} when the field declares none.
    constraints: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class MemorySchemaSummary(BaseModel):
    schema_id: str
    name: str
    description: str = ""
    status: str = "active"
    fields: list[SchemaFieldOutput] = Field(default_factory=list)


class MemorySchemaListOutput(BaseModel):
    # The namespace's schemas with their fields, ordered by name.
    schemas: list[MemorySchemaSummary]


class MemorySchemaDecisionInput(BaseModel):
    org: str
    namespace: str
    # The schema to decide on, by name.
    name: str
    # `approve` makes the schema `active`; `reject` marks it `rejected`.
    decision: Literal["approve", "reject"]
    # Reviewer fallback, stored only when the request has no principal to stamp.
    reviewed_by: str = ""


class MemorySchemaDecisionOutput(BaseModel):
    # The schema name, echoed.
    name: str
    # The status now stored (`active` or `rejected`); empty when no schema has the name.
    status: str
    # False when no schema has the name and nothing was written.
    updated: bool


class RecordFieldInput(BaseModel):
    value: str | int | float | bool | None = None
    state: Literal["value", "unknown", "rejected", "absent"] = "value"
    # A memory.evidence id (UUID). The column is Nullable(UUID), so an empty
    # or free-text id would fail the whole write in ClickHouse. Empty means
    # none; anything else must parse as a UUID and is refused at the wire.
    evidence_id: str | None = None

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _evidence_id_is_uuid_or_none(cls, v: object) -> str | None:
        if v is None:
            return None
        text = str(v).strip()
        if not text:
            return None
        try:
            return str(uuid.UUID(text))
        except (ValueError, AttributeError, TypeError) as e:
            raise ValueError(
                "evidence_id must be a UUID returned by memory.evidence, or omitted"
            ) from e


class MemoryRecordWriteInput(BaseModel):
    org: str
    namespace: str
    # An `active` schema of the namespace, else 404 SCHEMA_NOT_FOUND.
    schema_name: str
    # The logical key; empty derives it from identity fields, or leaves the record anonymous.
    memory_key: str = ""
    # The memory row this record cites; stored on the record and on its conflict rows.
    memory_id: str | None = None
    agent: str = ""
    user_id: str = "-"
    session_id: str = "-"
    # Field name → value and state; names outside the schema or missing required ones are 400.
    fields: dict[str, RecordFieldInput]
    # When the fact became true in the world; closes the superseded record's `valid_to`.
    valid_at: datetime | None = None
    # When the fact stops being true; None = open-ended.
    valid_to: datetime | None = None


class MemoryRecordWriteOutput(BaseModel):
    # The new record's id; every write mints a fresh one.
    record_id: str
    # Typed field rows written, including fields kept from the superseded record.
    field_count: int
    # The logical key the record landed on: the caller's, or one derived from
    # the schema's identity fields, or "" (anonymous — no supersession).
    memory_key: str = ""
    # Where `memory_key` came from: the caller, the identity fields, or none.
    key_source: Literal["caller", "identity", "anonymous"] = "anonymous"
    # Supersession is a field-level fold, not a whole-record replace:
    # {agreed, filled_from_prior, added, unioned,
    # changed:[{field, prior, incoming}]} against the record this write
    # superseded; empty when there was none. `filled_from_prior` names the
    # fields the incoming record left empty and the stored record kept from
    # its predecessor; `changed` are the real disagreements, also written
    # to loom.memory_record_conflicts (memory.record.conflicts lists them).
    fold: dict[str, Any] = Field(default_factory=dict)
    # The key's previously active record, now `superseded`; empty when there was none.
    superseded_record_id: str = ""
    # Field disagreements with the superseded record written to the conflicts table.
    conflicts: int = 0


class RecordPredicate(BaseModel):
    """Typed predicate over one record field, additive to `filters` (which
    stays exact string-equality). Comparison basis is chosen in priority
    order: number, then bool, then string; if none is set, `value_state`
    selects a state / negative query (e.g. the field is 'unknown' / 'absent').
    `op` applies to the value comparison and defaults to equality."""

    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"] = "eq"
    number: float | None = None
    boolean: bool | None = None
    string: str | None = None
    value_state: Literal["value", "unknown", "rejected", "absent"] | None = None


class MemoryRecordQueryInput(BaseModel):
    org: str
    namespace: str
    # The schema whose active records are read.
    schema_name: str
    # Field name → exact string value (state `value`); all must match.
    filters: dict[str, str] = Field(default_factory=dict)
    # Typed comparisons or state checks, AND-composed with `filters`.
    predicates: list[RecordPredicate] = Field(default_factory=list)
    # Only records whose `valid_at`..`valid_to` interval covers this instant; None = no bound.
    as_of: datetime | None = None
    # At most this many records (1–1000), most recently updated first.
    limit: int = Field(default=100, ge=1, le=1000)


class MemoryRecordRow(BaseModel):
    record_id: str
    memory_key: str = ""
    memory_id: str = ""
    record: dict[str, Any] = Field(default_factory=dict)
    field_states: dict[str, str] = Field(default_factory=dict)
    # tombstoned: a decision whose memory row was deleted.
    status: Literal["active", "superseded", "rejected", "tombstoned"] = "active"
    valid_at: datetime | None = None
    valid_to: datetime | None = None


class MemoryRecordQueryOutput(BaseModel):
    # Matching active records, most recently updated first.
    rows: list[MemoryRecordRow]
    # True when more records matched than `limit`.
    truncated: bool = False


class MemoryRecordHistoryInput(BaseModel):
    org: str
    namespace: str
    # The record whose stored versions are listed.
    record_id: str
    # Accepted and bounded (1–500), but the handler does not apply it.
    limit: int = Field(default=50, ge=1, le=500)


class MemoryRecordHistoryOutput(BaseModel):
    # One row per stored version of the record, newest first.
    rows: list[MemoryRecordRow]


class MemoryRecordConflictsInput(BaseModel):
    """Field-level disagreements recorded when a typed record superseded the
    active record for its logical key: which field, both
    values, both record ids and both citing memories. Filter by schema
    and/or key; open_only hides rows a later write resolved."""

    org: str
    namespace: str
    # Only this schema's conflicts; empty = every schema.
    schema_name: str = ""
    # Only this logical key's conflicts; empty = every key.
    memory_key: str = ""
    # True hides conflicts with a `resolved_at`.
    open_only: bool = True
    # At most this many rows (1–1000), newest detection first.
    limit: int = Field(default=100, ge=1, le=1000)


class MemoryRecordConflictRow(BaseModel):
    schema_name: str
    memory_key: str
    field_name: str
    prior_value: Any = None
    incoming_value: Any = None
    prior_record_id: str
    incoming_record_id: str
    prior_memory_id: str = ""
    incoming_memory_id: str = ""
    detected_at: datetime | None = None
    resolved_at: datetime | None = None


class MemoryRecordConflictsOutput(BaseModel):
    # Field-level disagreements, newest detection first.
    rows: list[MemoryRecordConflictRow]
    # True when more rows matched than `limit`.
    truncated: bool = False
