"""The assistant's service row, `loom.assistant_service`: every statement on it."""

from __future__ import annotations

from dataclasses import dataclass, replace

from server.core.ch import own_write_reads
from server.core.repo_base import _CHClient, _default_client


@dataclass(frozen=True)
class ServiceRow:
    """The service user's state; `sealed` opens to its password and API key."""

    email: str
    sealed: str
    agent_id: str = ""
    definition_version: int = 0
    definition_model: str = ""
    version: int = 0

    def advanced(self, **changes: object) -> ServiceRow:
        """This row with `changes`, at the next version."""
        return replace(self, **changes, version=self.version + 1)  # type: ignore[arg-type]


async def read(*, ch: _CHClient | None = None) -> ServiceRow | None:
    """The row, or None before a boot wrote it. Read with read-your-writes, so a row
    another replica just wrote is seen."""
    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one(
            "SELECT email, sealed, agent_id, definition_version, definition_model, version "
            "FROM loom.assistant_service FINAL WHERE id = 0"
        )
    if row is None:
        return None
    return ServiceRow(
        email=str(row["email"]),
        sealed=str(row["sealed"]),
        agent_id=str(row["agent_id"]),
        definition_version=int(row["definition_version"]),
        definition_model=str(row["definition_model"]),
        version=int(row["version"]),
    )


async def write(row: ServiceRow, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.assistant_service",
        [
            {
                "id": 0,
                "email": row.email,
                "sealed": row.sealed,
                "agent_id": row.agent_id,
                "definition_version": row.definition_version,
                "definition_model": row.definition_model,
                "version": row.version,
            }
        ],
        settings={"async_insert": 0},
    )
