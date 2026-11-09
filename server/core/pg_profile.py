"""Boot checks of the storage profile: which store is the memory authority, and
whether Postgres acknowledges a commit as durably as LOOM_PG_DURABILITY says.

The authority is recorded once per deployment, in ClickHouse
(`loom.storage_authority`, which every replica of either profile reads) and, for
the Postgres profile, in Postgres (`loom.authority`) with the same deployment id.
A replica whose configuration disagrees with the record refuses to start, so an
outage or a wrong setting never moves writes from one authority to the other.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from clickhouse_connect.driver.exceptions import DatabaseError

from server.core import pg
from server.core.ch_errors import error_code
from server.core.ch_writes import committed_insert_settings
from server.core.error_summary import error_summary
from server.core.pg import AuthorityMismatch
from server.settings import settings

log = logging.getLogger(__name__)


class DurabilityRefused(RuntimeError):
    """Postgres would acknowledge commits less durably than LOOM_PG_DURABILITY requires."""


@dataclass(frozen=True)
class Durability:
    refusal: str = ""
    warnings: list[str] = field(default_factory=list)


def durability_verdict(profile: str, standby_names: str, sync_standbys: int | None) -> Durability:
    """What boot does about the server's standbys. Loom's own sessions set
    `synchronous_commit = on`, so a commit is acknowledged once every standby
    `synchronous_standby_names` requires has flushed it. `sync_standbys` is the
    number pg_stat_replication shows as sync or quorum, None when this role cannot
    see them."""
    names = standby_names.strip()
    if not names:
        msg = (
            "synchronous_standby_names is empty: an acknowledged commit is on the primary "
            "alone and is lost with it"
        )
        if profile == "synchronous":
            return Durability(refusal=msg + " (LOOM_PG_DURABILITY=synchronous)")
        return Durability(warnings=[msg])
    if sync_standbys is None:
        return Durability(warnings=["cannot see pg_stat_replication; standbys are unverified"])
    if sync_standbys < required_standbys(names):
        return Durability(
            warnings=[
                "fewer synchronous standbys are attached than required: commits wait until "
                f"they are (synchronous_standby_names = {names!r})"
            ]
        )
    return Durability()


def required_standbys(standby_names: str) -> int:
    """The standbys a commit waits for: `n` of `FIRST n (...)`, `ANY n (...)` or
    `n (...)`, else 1 (a bare list waits for its first)."""
    m = re.match(r"\s*(?:(?:FIRST|ANY)\s+)?(\d+)\s*\(", standby_names, re.IGNORECASE)
    return int(m.group(1)) if m else 1


async def standby_state() -> tuple[str, int | None]:
    """(synchronous_standby_names, the synchronous standbys attached, None unseen)."""
    row = await pg.fetchrow(
        "SELECT current_setting('synchronous_standby_names') AS names, "
        "(SELECT count(*) FILTER (WHERE sync_state IN ('sync', 'quorum')) "
        "   FROM pg_stat_replication) AS sync, "
        "(SELECT count(*) FILTER (WHERE sync_state IS NULL) FROM pg_stat_replication) AS hidden"
    )
    return str(row["names"]), None if row["hidden"] else int(row["sync"])


async def check_durability() -> Durability:
    # A durable re-read (`pg.durable_transaction`) commits a logical message: a role that
    # may not write one refuses the boot, not every publish that re-reads a head.
    await pg.check_durable_message()
    names, sync = await standby_state()
    verdict = durability_verdict(settings.pg_durability, names, sync)
    if verdict.refusal:
        raise DurabilityRefused(verdict.refusal)
    for w in verdict.warnings:
        log.warning("Postgres durability: %s", w)
    return verdict


async def standby_now() -> Literal["synchronous", "primary_only", "waiting"] | None:
    """How a commit made now is acknowledged, what `/v1/health` reports: "synchronous" (the
    standbys it waits for are attached), "primary_only" (none is named: an HA manager that
    is not strict empties `synchronous_standby_names` when its standby drops), "waiting"
    (fewer are attached than it waits for, so commits wait), None when the standbys cannot
    be seen."""
    names, sync = await standby_state()
    if not names.strip():
        return "primary_only"
    if sync is None:
        return None
    return "synchronous" if sync >= required_standbys(names) else "waiting"


_CH_RECORD = (
    "SELECT authority, epoch, deployment_id FROM loom.storage_authority FINAL "
    "SETTINGS select_sequential_consistency = 1"
)


async def _ch_record(ch: Any) -> dict[str, Any] | None:
    row = await ch.query_one(_CH_RECORD)
    return dict(row) if row else None


async def _ch_record_if_migrated(ch: Any, *, warn: bool = True) -> dict[str, Any] | None:
    """`_ch_record`, or None before the migration that adds its table has run. `warn`:
    log that the table is missing (a boot); a CLI command passes False."""
    try:
        return await _ch_record(ch)
    except DatabaseError as e:
        if error_code(e) != 60:  # UNKNOWN_TABLE
            raise
        if warn:
            log.warning("loom.storage_authority is not migrated yet; the authority is not recorded")
        return None


async def check_configured(ch: Any) -> None:
    """Refuse a CLI command configured for another deployment than the one recorded: an
    erasure or an import run against the wrong store would leave or write another's rows.
    ClickHouse's record answers the authority now; under Postgres the deployment its
    `LOOM_PG_DSN` names is checked on the command's first connection
    (`pg.expect_deployment`), so a command that never touches memory (a token
    revocation) runs while Postgres is down."""
    recorded = await _recorded_matching(ch, warn=False)
    if settings.storage_authority == "postgres":
        if not settings.pg_dsn:
            raise AuthorityMismatch(pg.NO_DSN)
        if recorded is None:
            raise AuthorityMismatch(
                "this ClickHouse records no Postgres authority: it is a ClickHouse "
                "deployment, or one not booted yet with LOOM_BOOT_MIGRATE on"
            )
        pg.expect_deployment((str(recorded["deployment_id"]), int(recorded["epoch"])))


async def _ch_holds_memory(ch: Any) -> bool:
    row = await ch.query_one(
        "SELECT count() AS n FROM (SELECT 1 FROM loom.memory LIMIT 1)"
        " SETTINGS select_sequential_consistency = 1"
    )
    return bool(row and int(row["n"]))


async def _ch_write(ch: Any, authority: str, epoch: int, deployment_id: str) -> None:
    await ch.insert(
        "loom.storage_authority",
        [{"authority": authority, "epoch": epoch, "deployment_id": deployment_id}],
        settings=committed_insert_settings(),
    )


async def _pg_record() -> dict[str, Any] | None:
    row = await pg.fetchrow("SELECT authority, epoch, deployment_id FROM loom.authority")
    return dict(row) if row else None


async def require_authority(ch: Any) -> None:
    """Record the authority at a deployment's first boot, or refuse a replica whose
    LOOM_STORAGE_AUTHORITY (or LOOM_PG_DSN) disagrees with what is recorded."""
    from server.core import leases

    if not settings.boot_migrate:
        await _check_recorded(ch)
        return
    recorded = await _ch_record(ch)
    if recorded is not None and (
        settings.storage_authority == "clickhouse" or await _pg_record() is not None
    ):
        await _require_authority(ch)  # both records written: a check, no lease
        return
    # Waits past the lease's life, so a holder that died before writing is outlived.
    async with leases.holding("storage_authority", ttl_s=60, wait_s=120, ch=ch) as held:
        if not held:
            raise AuthorityMismatch("another replica held the authority record past 120 s")
        await _require_authority(ch)


async def _check_recorded(ch: Any) -> None:
    """For a process that runs no migrations (`LOOM_BOOT_MIGRATE=0`): it serves the
    schema it finds and writes no record into a service that never asked for one. It
    checks the record there is; under Postgres one must be there, written by a boot
    that ran its migrations."""
    recorded = await _recorded_matching(ch)
    if settings.storage_authority == "clickhouse":
        return
    import asyncpg

    try:
        held = await _pg_record()
    except asyncpg.exceptions.UndefinedTableError:
        held = None  # this Postgres is not migrated yet
    if held is None:
        raise AuthorityMismatch(
            "no Postgres authority is recorded yet: boot once with LOOM_BOOT_MIGRATE on"
        )
    if recorded is None:
        raise AuthorityMismatch(
            f"Postgres records deployment {held['deployment_id']}, and this ClickHouse "
            "records no authority: boot once with LOOM_BOOT_MIGRATE on to record it, or "
            "check that the ClickHouse settings name this deployment"
        )
    pg.same_deployment(held, recorded)


async def _recorded_matching(ch: Any, *, warn: bool = True) -> dict[str, Any] | None:
    """ClickHouse's record (`_ch_record_if_migrated`), refused when it names another
    authority than the configured one."""
    recorded = await _ch_record_if_migrated(ch, warn=warn)
    if recorded is not None and recorded["authority"] != settings.storage_authority:
        raise _other_authority(recorded)
    return recorded


def _other_authority(recorded: dict[str, Any]) -> AuthorityMismatch:
    return AuthorityMismatch(
        f"this deployment's memory authority is {recorded['authority']} "
        f"(epoch {recorded['epoch']}); set LOOM_STORAGE_AUTHORITY to match"
    )


async def _require_authority(ch: Any) -> None:
    configured = settings.storage_authority
    recorded = await _ch_record(ch)
    if configured == "clickhouse":
        if recorded is None:
            await _ch_write(ch, "clickhouse", 1, str(uuid.uuid4()))
        elif recorded["authority"] != "clickhouse":
            raise _other_authority(recorded)
        return
    held = await _pg_record()
    if recorded is not None and recorded["authority"] != "postgres":
        raise AuthorityMismatch(
            "this ClickHouse holds memory under the ClickHouse authority; moving it to "
            "Postgres needs a migration this build does not provide"
        )
    if recorded is None:
        if await _ch_holds_memory(ch):
            raise AuthorityMismatch(
                "this ClickHouse already holds memory under the ClickHouse authority; "
                "moving it to Postgres needs a migration this build does not provide"
            )
        if held is None:
            # Postgres first: a boot cut off between the two completes the record next time.
            held = {"authority": "postgres", "epoch": 1, "deployment_id": str(uuid.uuid4())}
            # In a transaction: its commit waits for the standby, which a single statement's
            # timeout would cut short.
            async with pg.transaction() as conn:
                await conn.execute(
                    "INSERT INTO loom.authority (authority, epoch, deployment_id) "
                    "VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
                    held["authority"],
                    held["epoch"],
                    held["deployment_id"],
                )
            held = await _pg_record() or held
        elif await pg.fetchrow("SELECT 1 FROM loom.memory_current LIMIT 1"):
            raise AuthorityMismatch(
                f"this Postgres holds the memory of deployment {held['deployment_id']}, "
                "and this ClickHouse records no authority: it is another deployment's, "
                "or it was not restored"
            )
        await _ch_write(ch, "postgres", int(held["epoch"]), str(held["deployment_id"]))
        return
    if held is None:
        raise AuthorityMismatch(
            f"ClickHouse records the Postgres authority of deployment {recorded['deployment_id']}, "
            "but this Postgres records none: LOOM_PG_DSN names another database, or it was "
            "not restored"
        )
    pg.same_deployment(held, recorded)


LEDGER_UNREADABLE = "<ledger unreadable>"
_ledger_unread = False  # the last read of the ledger failed


async def pending_files() -> list[str]:
    """As the ClickHouse boot step does for a process that applies no migrations: the
    files this service lacks, logged. A ledger that disagrees with this build
    (`MigrationDrift`) refuses the boot; one that cannot be read is listed as
    `LEDGER_UNREADABLE`, so readiness reads it again rather than report it current."""
    from server.core.schema import pg_migrate

    global _ledger_unread
    try:
        names = await pg_migrate.pending()
    except pg_migrate.MigrationDrift:
        raise
    except Exception as e:  # noqa: BLE001 — the boot goes on; readiness reads it again
        if not _ledger_unread:  # once, not on every readiness probe
            log.warning("Postgres pending-migration check failed: %s", error_summary(e))
        _ledger_unread = True
        return [LEDGER_UNREADABLE]
    _ledger_unread = False
    if names:
        log.warning("Postgres migrations skipped (LOOM_BOOT_MIGRATE=false): %s", ", ".join(names))
    return names


async def start() -> list[str]:
    """Open Postgres, apply its migrations and check its durability: the Postgres
    profile's boot steps before `require_authority`. Returns the Postgres migration
    files a process that applies none skipped."""
    from server.core.schema import pg_migrate

    if (
        settings.retention_memory_days
        or settings.retention_memory_embeddings_days
        or settings.retention_documents_days
    ):
        raise RuntimeError(
            "LOOM_RETENTION_MEMORY_DAYS, LOOM_RETENTION_MEMORY_EMBEDDINGS_DAYS and "
            "LOOM_RETENTION_DOCUMENTS_DAYS are ClickHouse TTLs; under the Postgres "
            "authority they would delete ClickHouse's copies alone. Unset them."
        )
    await pg.open_pool()
    pending: list[str] = []
    if settings.boot_migrate:
        await pg_migrate.migrate()
    else:
        pending = await pending_files()
    await check_durability()
    return pending
