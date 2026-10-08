"""loom.session_outcomes_by_user: one outcome label per session, written by the
distiller's judge and by a client's `run.complete`."""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

# Outcomes keyed by (org, namespace, agent, user_id, session_id).
OUTCOMES_TABLE = "loom.session_outcomes_by_user"


async def insert_outcomes(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> None:
    """Commit outcome rows (built by `build_outcome_row`) before returning: the
    contrast pass and procedure readouts read them back."""
    await (ch or _default_client()).insert(
        OUTCOMES_TABLE, rows, settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
