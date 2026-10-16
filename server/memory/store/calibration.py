"""loom.embedder_calibration: the cluster-wide similarity calibration, one row
per embedding space, so every replica serves the same gates. `embedder_model`
holds the space's key (`embedder_calibration.shared_key`: the model and the
space's fingerprint).

`thresholds_json` is the CalibrationResult as JSON: a reader takes the fields
it knows and defaults the rest, so builds N-1 and N read each other's rows."""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.embedder_calibration"


async def fetch_global_calibration(
    key: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The newest row for the space `key`, or None. Raises on a failed read
    (the table missing included): the caller decides what that means."""
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT thresholds_json, calibration_hash, calibrated_by, calibrated_at, version
        FROM loom.embedder_calibration FINAL
        WHERE embedder_model = {m:String}
        LIMIT 1
        """,
        params={"m": key},
    )


async def write_global_calibration(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Insert one committed version row (embedder_model, thresholds_json,
    calibration_hash, calibrated_by, version)."""
    ch = ch or _default_client()
    await ch.insert(TABLE, [row], settings={"async_insert": 0, "wait_for_async_insert": 1})
