"""Serialize ClickHouse head checks and memory inserts across serving processes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from server.core import lease_writes
from server.core.repo_base import _CHClient
from server.memory.store.pg_memory import AdmissionContendedError
from server.memory.store.reads import clickhouse_head_versions
from server.memory.store.rows import IdentityKey, identity_key


def _lease_client(ch: _CHClient) -> _CHClient:
    return ch


def lease_name(key: IdentityKey) -> str:
    encoded = json.dumps(key, ensure_ascii=True, separators=(",", ":")).encode()
    return "memory_insert/" + hashlib.sha256(encoded).hexdigest()


@asynccontextmanager
async def checked_write(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_heads: Mapping[IdentityKey, int | None] | None,
    ch: _CHClient,
) -> AsyncIterator[None]:
    """Every value writer takes these fences too, closing the check/insert window.

    A failed send retains its Keeper holders until all admitted inserts expire;
    an old process cannot send an insert after another holder starts its check.
    """
    keys = {identity_key(r) for r in rows}
    if expected_heads is not None and set(expected_heads) != keys:
        raise ValueError("expected heads must name exactly the inserted keys")
    names = [lease_name(key) for key in sorted(keys)]
    async with lease_writes.fenced_many(names, ch=_lease_client(ch)) as mark:
        if expected_heads is not None:
            current = await clickhouse_head_versions(list(keys), ch=ch)
            if any(current.get(key) != version for key, version in expected_heads.items()):
                raise AdmissionContendedError("the memory changed while it was read")
        mark(True)
        yield
        mark(False)
