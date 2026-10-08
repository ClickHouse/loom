"""Durable, one-step-at-a-time orchestration of an embedding-space rebuild.

The caller owns the exclusive coordinator lease for the entire step. Backend
effects must be idempotent by configuration ID and cursor: a process can die
after an effect succeeds but before its checkpoint reaches ClickHouse.
"""

import json
from dataclasses import dataclass
from time import time_ns
from typing import Any, Protocol
from uuid import UUID

from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest
from server.insights.store.provider_configuration import write_configuration

_PHASES = ("drain", "snapshot", "rebuild", "verify", "publish", "cleanup", "activate")


@dataclass(frozen=True)
class BatchCheckpoint:
    cursor: int
    rows_written: int
    done: bool


class RebuildBackend(Protocol):
    """Storage operations under a live, fenced coordinator lease.

    drain must include other processes and deferred writes, not just the local
    admission counter. snapshot freezes input; rebuild writes staging tables;
    verify checks completeness; publish resumes any partly completed exchange;
    cleanup removes only this job's staging/backup data. None may reopen traffic.
    """

    async def drain(self, config_id: UUID) -> bool: ...
    async def snapshot(self, config_id: UUID) -> RebuildManifest: ...
    async def rebuild(
        self, config_id: UUID, table: str, cursor: int, manifest: RebuildManifest
    ) -> BatchCheckpoint: ...
    async def verify(self, config_id: UUID, manifest: RebuildManifest) -> RebuildManifest: ...
    async def publish(self, config_id: UUID, manifest: RebuildManifest) -> None: ...
    async def cleanup(self, config_id: UUID, manifest: RebuildManifest) -> None: ...


def _progress(row: dict[str, Any]) -> dict[str, Any]:
    progress = json.loads(row["progress"])
    if not isinstance(progress, dict):
        raise ValueError("Invalid rebuild progress")
    progress.setdefault("plan_version", 1)
    progress.setdefault("phase", "drain")
    progress.setdefault("table_index", 0)
    progress.setdefault("cursor", 0)
    progress.setdefault("rows_done", 0)
    if (
        type(progress["plan_version"]) is not int
        or progress["plan_version"] != 1
        or progress["phase"] not in _PHASES
    ):
        raise ValueError("Unsupported rebuild checkpoint")
    if any(
        type(progress[name]) is not int or progress[name] < 0
        for name in ("table_index", "cursor", "rows_done")
    ):
        raise ValueError("Invalid rebuild cursor")
    if progress["table_index"] > len(REBUILD_TABLES):
        raise ValueError("Invalid rebuild table index")
    if progress["phase"] in ("verify", "publish", "cleanup", "activate") and (
        progress["table_index"] != len(REBUILD_TABLES) or progress["cursor"] != 0
    ):
        raise ValueError("Rebuild checkpoint has unfinished tables")
    if progress["phase"] not in ("drain", "snapshot"):
        manifest = RebuildManifest.from_dict(progress.get("manifest"))
        if manifest.job_id != UUID(str(row["config_id"])):
            raise ValueError("Rebuild manifest belongs to another configuration")
        if progress["phase"] in ("publish", "cleanup", "activate"):
            manifest.require_prepared()
    return progress


async def _checkpoint(
    row: dict[str, Any], progress: dict[str, Any], state: str, error: str = ""
) -> dict[str, Any]:
    updated = {
        **row,
        "state": state,
        "progress": json.dumps(progress, sort_keys=True),
        "error_code": error,
        "version": max(time_ns(), int(row["version"]) + 1),
    }
    await write_configuration(updated)
    return updated


async def _rebuild_page(config_id: UUID, progress: dict[str, Any], backend: RebuildBackend) -> None:
    index = progress["table_index"]
    if index == len(REBUILD_TABLES):
        progress["phase"] = "verify"
        return
    result = await backend.rebuild(
        config_id,
        REBUILD_TABLES[index],
        progress["cursor"],
        RebuildManifest.from_dict(progress["manifest"]),
    )
    if (
        type(result.cursor) is not int
        or type(result.rows_written) is not int
        or type(result.done) is not bool
        or result.cursor < progress["cursor"]
        or result.rows_written < 0
        or (not result.done and result.cursor == progress["cursor"])
    ):
        raise ValueError("Rebuild backend did not advance its cursor")
    progress["rows_done"] += result.rows_written
    progress["cursor"] = 0 if result.done else result.cursor
    progress["table_index"] += int(result.done)
    progress["table"] = REBUILD_TABLES[index]
    if progress["table_index"] == len(REBUILD_TABLES):
        progress["phase"] = "verify"


async def _advance(config_id: UUID, progress: dict[str, Any], backend: RebuildBackend) -> bool:
    # Repeat the barrier on recovery and before every side effect. A restarted
    # process or a new replica must not be mistaken for an earlier drain ACK.
    if not await backend.drain(config_id):
        return False
    phase = progress["phase"]
    if phase == "rebuild":
        await _rebuild_page(config_id, progress, backend)
    elif phase == "drain":
        progress["phase"] = "snapshot"
    elif phase == "snapshot":
        manifest = await backend.snapshot(config_id)
        if manifest.job_id != config_id:
            raise ValueError("Snapshot manifest belongs to another configuration")
        progress["manifest"] = manifest.to_dict()
        progress["phase"] = "rebuild"
    elif phase == "verify":
        prior = RebuildManifest.from_dict(progress["manifest"])
        manifest = await backend.verify(config_id, prior)
        manifest.prepared_successor(prior)
        progress["manifest"] = manifest.to_dict()
        progress["phase"] = "publish"
    elif phase != "activate":
        operation = {
            "publish": backend.publish,
            "cleanup": backend.cleanup,
        }[phase]
        await operation(config_id, RebuildManifest.from_dict(progress["manifest"]))
        progress["phase"] = _PHASES[_PHASES.index(phase) + 1]
    return True


async def run_rebuild_step(row: dict[str, Any], backend: RebuildBackend) -> dict[str, Any]:
    """Run at most one bounded step; failed jobs require an explicit retry.

    Persist failures with a fixed code, never provider response text or source
    data. Storage-write errors propagate without overwriting an ambiguous
    successful checkpoint (particularly the final activation).
    """
    if row["state"] not in ("queued", "rebuilding", "validating"):
        return row
    progress = _progress(row)
    if row["state"] == "queued":
        return await _checkpoint(row, progress, "rebuilding")
    phase = progress["phase"]
    try:
        advanced = await _advance(UUID(str(row["config_id"])), progress, backend)
    except Exception:  # noqa: BLE001 — retain the last cursor and expose no provider/source secrets
        return await _checkpoint(row, _progress(row), "failed", f"rebuild_{phase}_failed")
    if not advanced:
        return row
    if phase == "activate":
        state = "environment" if progress.get("action") == "environment" else "active"
    else:
        state = "validating" if progress["phase"] == "verify" else "rebuilding"
    return await _checkpoint(row, progress, state)
