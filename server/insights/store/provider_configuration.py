"""Complete-row writes for operator configuration and resumable rebuilds.

Only ciphertext enters this repository. Admission, writer serialization and
activation policy belong to the app layer; this store is not a CAS primitive.
"""

from typing import Any
from uuid import UUID

from server.core.repo_base import _CHClient, _default_client

_COLUMNS = (
    "config_id",
    "parent_id",
    "state",
    "envelope",
    "progress",
    "error_code",
    "created_at",
    "version",
)


async def configurations(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Return live row versions, newest first, for activation/recovery policy."""
    client = ch or _default_client()
    return await client.query_all(
        "SELECT config_id, parent_id, state, envelope, progress, error_code, created_at, version "
        "FROM loom.provider_configuration FINAL ORDER BY version DESC"
    )


async def configuration(config_id: UUID, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    client = ch or _default_client()
    return await client.query_one(
        "SELECT config_id, parent_id, state, envelope, progress, error_code, created_at, version "
        "FROM loom.provider_configuration FINAL WHERE config_id = {id:UUID}",
        {"id": str(config_id)},
    )


async def write_configuration(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Require the entire persisted row on every transition (RMT invariant)."""
    if set(row) != set(_COLUMNS):
        raise ValueError("provider configuration writes must carry every persisted column")
    client = ch or _default_client()
    await client.insert("loom.provider_configuration", [row])
