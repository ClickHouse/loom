"""Stopped-cell store identity, schema and direct access probes for metadata restore."""

from __future__ import annotations

import contextlib
import hashlib
import secrets
from typing import Any

from server.cli import cell_restore_pg as local
from server.core import cell, cell_probe, instances, leases, object_store, pg, pg_profile
from server.settings import settings


async def identity(ch: Any, conn: Any) -> dict[str, Any]:
    recorded = await pg_profile.recorded_authority(ch)
    held = await local.identity(conn)
    if recorded is None or recorded["authority"] != "postgres":
        raise ValueError("ClickHouse records no Postgres authority")
    pg.same_deployment(held, recorded)
    return held


def descriptor(value: dict[str, Any], recorded: dict[str, Any]) -> None:
    from server.code.domain.index import repos

    if value["cell"] != settings.cell or value["deployment_id"] != recorded["deployment_id"]:
        raise ValueError("directory cell identity differs from this configuration")
    if (value["bucket"], value["bucket_prefix"]) != (
        settings.document_s3_bucket,
        settings.document_s3_prefix if settings.document_s3_bucket else "",
    ):
        raise ValueError("directory bucket name or prefix differs from this configuration")
    cell.set_code_orgs(repos.operator_orgs)
    mine = cell.fingerprint(recorded["deployment_id"])
    if value["current_fp"] is not None and not any(
        cell.matches(mine, cell.json_object(value[key])) for key in ("current_fp", "next_fp")
    ):
        raise ValueError("the CLI configuration matches neither directory fingerprint")


def configuration_recovery_required(own: dict[str, Any] | None, deployment_id: str) -> bool:
    """Report retained acceptance separately from the missing-own prepare step."""
    if own is None:
        return False
    mine = cell.fingerprint(deployment_id)
    return not any(cell.matches(mine, own[key]) for key in ("current_fp", "next_fp"))


async def stopped_and_schema(ch: Any) -> None:
    from server.core.paths import MIGRATIONS_DIR
    from server.core.schema.journal import migration_number, sorted_migrations
    from server.core.schema.migrate import ledger_report

    if await instances.read_live(ch, all_deployments=True):
        raise ValueError(
            "a cell process reports live; stop every process before restore "
            "(restored beats may be stale)"
        )
    recent = await ch.query_one(
        "SELECT count() AS n FROM loom.worker_status "
        "WHERE pass_finished > now64(3) - toIntervalSecond({s:UInt32})",
        {"s": 2 * settings.supervisor_lease_ttl_s},
    )
    if recent and int(recent["n"]):
        raise ValueError("a worker reports a recent pass; stop every worker before restore")
    rows = await ch.query_all(
        "SELECT filename, toString(checksum) AS checksum FROM loom._migrations FINAL"
    )
    files = sorted_migrations(MIGRATIONS_DIR.glob("*.sql"))
    report = ledger_report({r["filename"]: r["checksum"] for r in rows}, files)
    last = max((migration_number(f.name) or 0 for f in files), default=0)
    older_unknown = [name for name in report.ledger_only if (migration_number(name) or 0) <= last]
    if report.pending or report.drift or report.stranded or older_unknown:
        raise ValueError(
            "ClickHouse schema is not current: run offline loom migrate before cell restore"
        )


async def nonces(ch: Any, conn: Any) -> None:
    nonce = secrets.token_hex(16)
    name = cell_probe.NONCE_NAME
    bucket = settings.document_s3_bucket
    location = object_store.Location(bucket, object_store.root_prefix() + cell_probe.OBJECT_NAME)
    try:

        async def postgres_probe() -> None:
            await conn.execute(
                "INSERT INTO loom.cell_probe (name, nonce) VALUES ($1, $2) "
                "ON CONFLICT (name) DO UPDATE SET nonce=EXCLUDED.nonce, written_at=now()",
                name,
                nonce,
            )
            if (
                await conn.fetchval("SELECT nonce FROM loom.cell_probe WHERE name=$1", name)
                != nonce
            ):
                raise ValueError("Postgres did not return the fresh restore nonce")
            await conn.execute(
                "DELETE FROM loom.cell_probe WHERE name=$1 AND nonce=$2", name, nonce
            )

        await local.transaction(conn, postgres_probe)
        await ch.execute(
            f"INSERT INTO {leases.LOCK_TABLE} "
            "(name, holder, acquired_at, renewed_at, ttl_s, build_commit) "
            "SETTINGS keeper_map_strict_mode=0, async_insert=0 "
            "VALUES ({name:String}, {nonce:String}, now64(3), now64(3), 300, '')",
            {"name": name, "nonce": nonce},
        )
        found = await ch.query_one(
            f"SELECT holder FROM {leases.LOCK_TABLE} WHERE name={{name:String}}", {"name": name}
        )
        if found is None or found["holder"] != nonce:
            raise ValueError("ClickHouse did not return the fresh restore nonce")
        if bucket:
            data = nonce.encode()
            async with object_store.transfer() as lease:
                await object_store.put(location, data, hashlib.sha256(data).hexdigest(), lease)
            if await object_store.read(location) != data:
                raise ValueError("the bucket did not return the fresh restore nonce")
    except BaseException:  # Nonce cleanup preserves the first failure or cancellation.
        with contextlib.suppress(BaseException):
            await _cleanup(ch, location, nonce)
        raise
    else:
        await _cleanup(ch, location, nonce)


async def _cleanup(ch: Any, location: object_store.Location, nonce: str) -> None:
    import sys

    try:
        await ch.execute(leases.LOCK_RELEASE, {"name": cell_probe.NONCE_NAME, "me": nonce})
    except Exception as error:  # noqa: BLE001 — a probe cleanup cannot replace the repair result
        print(
            f"loom cell restore: ClickHouse nonce cleanup failed ({type(error).__name__})",
            file=sys.stderr,
        )
    if location.bucket:
        try:
            if await object_store.read(location) == nonce.encode():
                await object_store.delete(location.bucket, [location.key])
        except Exception as error:  # noqa: BLE001 — a probe cleanup cannot replace the repair result
            print(
                f"loom cell restore: bucket nonce cleanup failed ({type(error).__name__})",
                file=sys.stderr,
            )
