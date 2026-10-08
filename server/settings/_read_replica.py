"""Read-replicas: more Looms serving the same ClickHouse as the main one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from server.settings import Settings


class ReadReplicaSettings(BaseModel):
    # A read-replica serves the same data, tokens and ops as the main Loom on
    # its ClickHouse, from its own endpoint, and does none of the work that
    # must happen once per service: no migrations, no background workers, no
    # git mirrors. It takes the main Loom's published settings
    # (`loom.deployment_config`) for every field its own env leaves unset. See
    # docs/operations/read-replicas.md.
    read_replica: bool = False
    # Seconds between re-reads of the main Loom's published settings (a code
    # repository it added, a tracker binding).
    read_replica_config_refresh_seconds: float = Field(default=60.0, ge=5.0)
    # The main Loom's base URL (`https://loom.example.com`). The ops that read
    # full git history (`server.core.read_replica.FORWARDED_OPS`) are sent there
    # with the caller's own token; empty = they answer that this Loom has no
    # git history.
    read_replica_main_url: str = ""
    # Seconds a forwarded op may take on the main Loom before this replica
    # answers it without git history.
    read_replica_forward_timeout_seconds: float = Field(default=60.0, gt=0.0)
    # Seconds an LLM completion this replica asks the main Loom for may take
    # (a reasoning model's synthesis can run minutes) before it fails here.
    read_replica_llm_timeout_seconds: float = Field(default=300.0, gt=0.0)
    # Seconds between this replica's checks that the main Loom answers
    # (`/v1/health` reports the last outcome as `replica.main`).
    read_replica_main_probe_seconds: float = Field(default=30.0, ge=5.0)


# The defaults a read-replica changes when its env leaves them alone, and the
# value each must hold there: the main Loom alone migrates the shared schema
# and runs the workers.
_REPLICA_IMPLIES: dict[str, object] = {
    "boot_migrate": False,
    "background_workers": "off",
}


def apply_read_replica_rules(s: Settings) -> None:
    """Pin the settings a read-replica implies. Raises ValueError naming the
    setting its env sets otherwise; the main Loom is left untouched."""
    if not s.read_replica:
        return
    explicit = s.model_fields_set
    for name, value in _REPLICA_IMPLIES.items():
        current = getattr(s, name)
        if current == value:
            continue
        if name in explicit:
            raise ValueError(
                f"LOOM_{name.upper()}={current!r} is not allowed with "
                f"LOOM_READ_REPLICA=true (a read-replica runs with {value!r})"
            )
        setattr(s, name, value)
