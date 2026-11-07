"""A loom.memory row as the full FINAL projection returns it, for stubs of the
readers that hydrate full rows (`memory_rows_by_identity_keys`, `get_by_key`)."""

from __future__ import annotations

from typing import Any


def projection_row(
    *,
    org: str,
    namespace: str,
    agent: str = "-",
    user_id: str = "-",
    session_id: str = "-",
    **cols: Any,
) -> dict[str, Any]:
    """Every persisted column (schema defaults, then `cols`), plus the
    projection's `value_text` alias."""
    from server.memory import store as mr

    base = mr.MemoryRow.from_snapshot(
        {
            "memory_key": cols.get("memory_key", "k"),
            "agent": agent,
            "user_id": user_id,
            "session_id": session_id,
        },
        org=org,
        namespace=namespace,
    )
    row = mr.build_memory_version_row(
        base, **{k: v for k, v in cols.items() if k in mr.MEMORY_PERSISTED_COLS}
    )
    row["value_text"] = row["value"]
    return row


def memory_head(*, org: str, namespace: str, **cols: Any) -> Any:
    """The `MemoryRow` a head read returns for `projection_row(...)`."""
    from server.memory import store as mr

    return mr._hydrate(projection_row(org=org, namespace=namespace, **cols))
