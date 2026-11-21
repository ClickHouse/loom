"""Deployment privacy state; an absent row needs a fresh ordered Keeper proof."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from server.core.ch import CHClient, get_client, query_tag
from server.core.ch_names import database, qualify
from server.core.leases import LOCK_INSERT, LOCK_RELEASE, key_exists

_LATCH = (
    "SELECT toString(serverUUID()) AS server_id, count() AS enabled "
    "FROM loom.knowledge_mirror_latch WHERE id = 0 LIMIT 1"
)
_SET = (
    "INSERT INTO loom.knowledge_mirror_latch (id, set_at) "
    "SETTINGS keeper_map_strict_mode = 1, async_insert = 0 "
    "SELECT throwIf(toString(serverUUID()) != {backend:String}, "
    "'Session protection writer changed'), now64(3)"
)
_ENGINES = (
    "SELECT toString(serverUUID()) AS server_id, groupArray((name, engine_full)) AS engines "
    "FROM system.tables WHERE database = {database:String} "
    "AND name IN ('knowledge_mirror_latch', '_migration_lock') LIMIT 1"
)
_KEEPER = (
    "SELECT toString(serverUUID()) AS server_id, "
    "groupArray((client_id, is_expired, isNotNull(index))) AS clients "
    "FROM system.zookeeper_connection WHERE name = 'default' LIMIT 1"
)
_BARRIER = LOCK_INSERT.replace("{commit:String}", "toString(serverUUID())")
_NONCE = (
    "SELECT toString(serverUUID()) AS server_id, "
    "groupArray((holder, renewed_at + toIntervalSecond(ttl_s) > now64(3), build_commit)) "
    "AS holders FROM loom._migration_lock WHERE name = {name:String} LIMIT 1"
)


class LatchUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Session protection state could not be confirmed")


async def _one(
    ch: CHClient,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    backend: uuid.UUID | None = None,
) -> tuple[uuid.UUID, dict[str, Any]]:
    row = await ch.query_one(sql, params)
    if row is None:
        raise LatchUnavailable()
    actual = uuid.UUID(str(row["server_id"]))
    if not actual.int or (backend is not None and actual != backend):
        raise LatchUnavailable()
    return actual, row


async def _engines(ch: CHClient) -> uuid.UUID:
    backend, row = await _one(ch, _ENGINES, {"database": database()})
    expected = {
        "knowledge_mirror_latch": qualify(
            "KeeperMap('/loom/knowledge_mirror_latch') PRIMARY KEY id"
        ),
        "_migration_lock": qualify("KeeperMap('/loom/migration_lock') PRIMARY KEY name"),
    }
    if len(row["engines"]) != 2 or dict(row["engines"]) != expected:
        raise LatchUnavailable()
    return backend


async def _latched(ch: CHClient, backend: uuid.UUID) -> bool:
    _, row = await _one(ch, _LATCH, backend=backend)
    if type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
        raise LatchUnavailable()
    return bool(row["enabled"])


async def _keeper(ch: CHClient, backend: uuid.UUID) -> int:
    _, row = await _one(ch, _KEEPER, backend=backend)
    if len(row["clients"]) != 1:
        raise LatchUnavailable()
    client_id, expired, connected = row["clients"][0]
    if type(client_id) is not int or not client_id or expired != 0 or connected != 1:
        raise LatchUnavailable()
    return client_id


async def _confirm(ch: CHClient, backend: uuid.UUID, params: dict[str, Any]) -> None:
    _, row = await _one(ch, _NONCE, params, backend=backend)
    if row["holders"] != [(params["me"], 1, str(backend))]:
        raise LatchUnavailable()


async def _clear(ch: CHClient, backend: uuid.UUID, params: dict[str, Any]) -> None:
    await ch.execute(LOCK_RELEASE, params)
    _, row = await _one(ch, _NONCE, params, backend=backend)
    if row["holders"]:
        raise LatchUnavailable()


async def enabled(*, ch: CHClient | None = None) -> bool:
    """Never cache an absence; only a confirmed synchronous write orders that read."""
    ch = ch or get_client()
    params = {
        "name": "knowledge_mirror_admission:" + uuid.uuid4().hex,
        "me": uuid.uuid4().hex,
        "ttl": 30,
    }
    sent = False
    with query_tag("knowledge.mirror.latch"):
        try:
            # not a setting: the privacy proof must end within its fixed security deadline.
            async with asyncio.timeout(5), ch.coordination_session():
                backend = await _engines(ch)
                if await _latched(ch, backend):
                    return True
                before = await _keeper(ch, backend)
                # The writer attests itself; a replayed bootstrap may exist on two backends.
                sent = True
                await ch.execute(_BARRIER, params)
                latched = await _latched(ch, backend)
                if await _keeper(ch, backend) != before:
                    raise LatchUnavailable()
                await _confirm(ch, backend, params)
                await _clear(ch, backend, params)
                if await _keeper(ch, backend) != before:
                    raise LatchUnavailable()
                sent = False
                return latched
        except Exception:  # noqa: BLE001 — uncertain privacy state refuses without backend diagnostics
            raise LatchUnavailable() from None
        finally:
            if sent:
                try:
                    # A failed proof never admits; only its own expiring nonce is disposable.
                    # not a setting: refused requests spend at most one second on own-key cleanup.
                    async with asyncio.timeout(1):
                        await ch.execute(LOCK_RELEASE, params)
                except Exception:  # noqa: BLE001 — the refused attempt leaves only its expiring own key
                    pass


async def ensure(*, ch: CHClient | None = None) -> None:
    """Set the real positive state; a racing identical insert is confirmed afresh."""
    ch = ch or get_client()
    with query_tag("knowledge.mirror.latch_set"):
        try:
            # not a setting: positive confirmation shares the fixed privacy-proof deadline.
            async with asyncio.timeout(5):
                async with ch.coordination_session():
                    backend = await _engines(ch)
                try:
                    await ch.execute(_SET, {"backend": str(backend)})
                except Exception as error:
                    if not key_exists(error):
                        raise
                async with ch.coordination_session():
                    if not await _latched(ch, await _engines(ch)):
                        raise LatchUnavailable()
        except Exception:  # noqa: BLE001 — uncertain privacy state refuses without backend diagnostics
            raise LatchUnavailable() from None
