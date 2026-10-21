"""Attribute stored-memory model calls to their principal writer."""

from collections.abc import Mapping, Sequence
from typing import Any

import server.memory.store as memory_repo
from server.core.ch_params import chunks
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import customer, identity
from server.memory.domain.trust import subject_of

IDENTITY_COLUMNS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


async def memory_writers(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, ...], str]:
    """Read writers for rows that identify a complete primary memory."""
    if not identity.reads_writer():
        return {}
    by_org: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        if all(c in row for c in IDENTITY_COLUMNS):
            by_org.setdefault(str(row["org"]), set()).add(
                (str(row["namespace"]), str(row["memory_key"]))
            )
    out: dict[tuple[str, ...], str] = {}
    for org, keys in by_org.items():
        for part in chunks(sorted(keys)):
            found = await memory_repo.actors_by_key(org, part, scope=WHOLE_NAMESPACE)
            for (namespace, key), heads in found.items():
                for head in heads:
                    out[
                        (org, namespace, head["agent"], head["user_id"], head["session_id"], key)
                    ] = head["actor"]
    return out


def attribution_for(
    row: Mapping[str, Any], writers: Mapping[tuple[str, ...], str]
) -> customer.Attribution:
    """Use the row's writer, falling back to its namespace only when none is recorded."""
    key = tuple(str(row.get(c) or "") for c in IDENTITY_COLUMNS)
    actor = str(row.get("actor") or writers.get(key, ""))
    return customer.Attribution(
        org=str(row.get("org") or ""),
        namespace=str(row.get("namespace") or ""),
        subject=subject_of(actor),
        user_id=str(row.get("user_id") or ""),
    )
