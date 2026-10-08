"""Durable identities for rebuild recovery; no credentials or user content."""

import re
from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import UUID

from server.insights.domain.provider_rebuild import REBUILD_TABLES


def _uuid(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError("Rebuild manifest requires nonzero table and job UUIDs")


def _fingerprint(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid rebuild manifest fingerprint")


@dataclass(frozen=True)
class RebuildTablePlan:
    table: str
    source_uuid: UUID
    engine: str
    columns: tuple[str, ...]
    schema_fingerprint: str
    snapshot_uuid: UUID
    target_uuid: UUID
    prepared_uuid: UUID | None = None
    verified_rows: int | None = None

    def __post_init__(self) -> None:
        if self.table not in REBUILD_TABLES:
            raise ValueError("Unknown rebuild manifest table")
        for identity in (self.source_uuid, self.snapshot_uuid, self.target_uuid):
            _uuid(identity)
        _fingerprint(self.schema_fingerprint)
        if not isinstance(self.engine, str) or not self.engine.endswith("MergeTree"):
            raise ValueError("Unsupported rebuild manifest engine")
        if (
            not isinstance(self.columns, tuple)
            or not self.columns
            or any(
                not isinstance(column, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column)
                for column in self.columns
            )
            or len(set(self.columns)) != len(self.columns)
            or "_loom_rebuild_ordinal" in self.columns
        ):
            raise ValueError("Invalid rebuild manifest columns")
        if self.prepared_uuid is not None:
            _uuid(self.prepared_uuid)
            if type(self.verified_rows) is not int or self.verified_rows < 0:
                raise ValueError("Prepared table requires a verified row count")
        elif self.verified_rows is not None:
            raise ValueError("Verified row count requires a prepared table")

    def unprepared(self) -> "RebuildTablePlan":
        return replace(self, prepared_uuid=None, verified_rows=None)


@dataclass(frozen=True)
class RebuildManifest:
    job_id: UUID
    embedding_fingerprint: str
    tables: tuple[RebuildTablePlan, ...]
    prefix_dimensions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.job_id)
        _fingerprint(self.embedding_fingerprint)
        if (
            not isinstance(self.prefix_dimensions, tuple)
            or len(self.prefix_dimensions) > 64
            or any(type(dim) is not int or not 1 <= dim <= 65535 for dim in self.prefix_dimensions)
            or len(set(self.prefix_dimensions)) != len(self.prefix_dimensions)
        ):
            raise ValueError("Invalid rebuild manifest prefix policy")
        if not isinstance(self.tables, tuple) or any(
            not isinstance(table, RebuildTablePlan) for table in self.tables
        ):
            raise ValueError("Invalid rebuild manifest tables")
        if tuple(table.table for table in self.tables) != REBUILD_TABLES:
            raise ValueError("Rebuild manifest must cover the ordered full inventory")
        identities = [
            identity
            for table in self.tables
            for identity in (
                table.source_uuid,
                table.snapshot_uuid,
                table.target_uuid,
                table.prepared_uuid,
            )
            if identity is not None
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("Rebuild manifest table identities must be distinct")

    def require_prepared(self) -> None:
        if any(table.prepared_uuid is None for table in self.tables):
            raise ValueError("Rebuild manifest has unprepared tables")

    def prepared_successor(self, prior: "RebuildManifest") -> None:
        self.require_prepared()
        if (
            self.job_id != prior.job_id
            or self.embedding_fingerprint != prior.embedding_fingerprint
            or self.prefix_dimensions != prior.prefix_dimensions
            or tuple(table.unprepared() for table in self.tables)
            != tuple(table.unprepared() for table in prior.tables)
            or any(
                old.prepared_uuid is not None and new != old
                for old, new in zip(prior.tables, self.tables, strict=True)
            )
        ):
            raise ValueError("Prepared manifest changed the frozen rebuild plan")

    def to_dict(self) -> dict[str, Any]:
        tables = []
        for table in self.tables:
            fields = asdict(table)
            for key in ("source_uuid", "snapshot_uuid", "target_uuid", "prepared_uuid"):
                fields[key] = str(fields[key]) if fields[key] is not None else None
            fields["columns"] = list(table.columns)
            tables.append(fields)
        return {
            "version": 2,
            "job_id": str(self.job_id),
            "embedding_fingerprint": self.embedding_fingerprint,
            "tables": tables,
            "prefix_dimensions": list(self.prefix_dimensions),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RebuildManifest":
        if (
            not isinstance(value, dict)
            or set(value)
            != {"version", "job_id", "embedding_fingerprint", "tables", "prefix_dimensions"}
            or type(value["version"]) is not int
            or value["version"] != 2
            or not isinstance(value["tables"], list)
            or not isinstance(value["prefix_dimensions"], list)
        ):
            raise ValueError("Unsupported rebuild manifest")
        try:
            tables = []
            for row in value["tables"]:
                if not isinstance(row, dict) or set(row) != {
                    "table",
                    "source_uuid",
                    "engine",
                    "columns",
                    "schema_fingerprint",
                    "snapshot_uuid",
                    "target_uuid",
                    "prepared_uuid",
                    "verified_rows",
                }:
                    raise ValueError("Malformed rebuild manifest table")
                fields = dict(row)
                if not isinstance(fields["columns"], list):
                    raise ValueError("Invalid rebuild manifest column list")
                fields["columns"] = tuple(fields["columns"])
                for key in ("source_uuid", "snapshot_uuid", "target_uuid", "prepared_uuid"):
                    fields[key] = UUID(fields[key]) if fields[key] is not None else None
                tables.append(RebuildTablePlan(**fields))
            return cls(
                UUID(value["job_id"]),
                value["embedding_fingerprint"],
                tuple(tables),
                tuple(value["prefix_dimensions"]),
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Malformed rebuild manifest") from error
