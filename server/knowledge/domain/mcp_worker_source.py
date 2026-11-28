"""Opaque worker registrations in the existing source registry."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from server.knowledge.domain.mcp_identity import ResourceIdentity
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource

WORKER_SOURCE_VERSION = 2
WORKER_REGISTRATION = "worker_registration"
_MAX_CONFIG_BYTES = 16_384  # not a setting: bounds a stored worker source registration
_MAX_SELECTION_BYTES = 131_072  # not a setting: bounds a stored worker source selection
_Generation = Annotated[int, Field(ge=1, le=2**63 - 1)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Items: TypeAdapter[tuple[ResourceIdentity, ...]] = TypeAdapter(
    Annotated[tuple[ResourceIdentity, ...], Field(min_length=1, max_length=1000)]
)


class WorkerSourceRefused(ValueError):
    """A source is not a supported current worker registration."""


class WorkerSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    version: Annotated[int, Field(ge=2, le=2)]
    worker_id: UUID
    profile_digest: _Digest
    approval_generation: _Generation
    owner_connection_id: UUID
    owner_connection_generation: _Generation
    selection_generation: _Generation
    mode: Literal["live", "snapshot", "selected_refresh"]
    reader_policy: Literal["owner_only", "current_team_access"]
    interval_seconds: Annotated[int, Field(ge=300, le=86_400)] | None = None
    paused: bool = False

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        if (
            (self.interval_seconds is None) == (self.mode == "selected_refresh")
            or self.worker_id.int == 0
            or self.owner_connection_id.int == 0
        ):
            raise ValueError("invalid worker source mode or binding")
        return self


def _matches_row(source: KnowledgeSource, config: WorkerSourceConfig) -> bool:
    try:
        return (
            UUID(source.source_id).int != 0
            and str(UUID(source.source_id)) == source.source_id
            and source.connection_id == str(config.owner_connection_id)
        )
    except (ValueError, AttributeError):
        return False


def encode_worker_source(
    *,
    org: str,
    namespace: str,
    name: str,
    source_id: UUID,
    owner: str,
    config: WorkerSourceConfig,
    selection: tuple[ResourceIdentity, ...],
) -> KnowledgeSource:
    """Encode an authorized registration without an upstream credential or endpoint."""
    if not org or not namespace or not owner or owner == "-" or not name:
        raise WorkerSourceRefused("worker source unavailable")
    if source_id.int == 0:
        raise WorkerSourceRefused("worker source unavailable")
    try:
        items = _Items.validate_python(selection, strict=True)
        row = KnowledgeSource(
            org=org,
            namespace=namespace,
            name=name,
            kind="mcp",
            source_id=str(source_id),
            access=MIRRORED_ACCESS,
            credential_mode="worker",
            owner_subject=owner,
            created_by=owner,
            connection_id=str(config.owner_connection_id),
            config={WORKER_REGISTRATION: config.model_dump_json()},
            items=[item.model_dump(mode="json") for item in items],
        )
        decode_worker_source(row)
        return row
    except (ValueError, TypeError, RecursionError):
        raise WorkerSourceRefused("worker source unavailable") from None


def decode_worker_source(
    source: KnowledgeSource,
) -> tuple[WorkerSourceConfig, tuple[ResourceIdentity, ...]]:
    if (
        source.kind != "mcp"
        or source.credential_mode != "worker"
        or source.access != MIRRORED_ACCESS
        or source.active is not True
        or source.pending_delete
        or not source.owner_subject
        or source.credential_enc
        or source.secret_enc
        or set(source.config) != {WORKER_REGISTRATION}
        or not 0 < len(source.items) <= 1000
    ):
        raise WorkerSourceRefused("worker source unavailable")
    try:
        payload = source.config[WORKER_REGISTRATION]
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise WorkerSourceRefused("worker source unavailable")
        config = WorkerSourceConfig.model_validate_json(payload)
        items = _Items.validate_python(tuple(source.items), strict=True)
        if len(_Items.dump_json(items)) > _MAX_SELECTION_BYTES:
            raise WorkerSourceRefused("worker source unavailable")
        if not _matches_row(source, config) or len({item.id for item in items}) != len(items):
            raise WorkerSourceRefused("worker source unavailable")
        return config, items
    except (ValueError, TypeError, RecursionError):
        raise WorkerSourceRefused("worker source unavailable") from None
