"""Statements of the document-source registry (`loom.knowledge_sources`) and of
each source's sync state (a `loom.code_index_state` row under
`knowledge_source:<name>`).

Both tables are ReplacingMergeTree(version): every write is a full version row
and every read uses FINAL. A registry row changes only on add, rotate, remove
and a repository's removal (`code.repo.remove`), all under the namespace's lease,
and on `loom secrets reseal`, the one writer outside it: it writes the row it
read at version + 1, so any later write wins while the ClickHouse servers' clocks
differ by less than the time between the two writes. The sync state is a separate row,
so a sync never rewrites the row that holds the credential.

Which documents are a source's: see `SYNC_WORKER_ACTOR`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.knowledge.domain.sources import (
    SYNC_WORKER_ACTOR,
    KnowledgeSource,
    json_object,
    source_tag,
    state_key,
)

TABLE = "loom.knowledge_sources"
STATE_TABLE = "loom.code_index_state"
# Documents `knowledge.source.inspect` lists, at most.
# not a setting: the op's wire contract (its description says 200)
DOCUMENTS_LIMIT = 200
# Documents one source's revision read returns, at most.
# not a setting: a bound on one statement, far above what 20 shared folders hold
REVISIONS_LIMIT = 50_000

_COLUMNS = (
    "org, namespace, name, source_id, kind, config_json, credential_enc, created_by, active, "
    "pending_delete, created_at, updated_at"
)


def _source(row: dict[str, Any]) -> KnowledgeSource:
    created = row.get("created_at")
    updated = row.get("updated_at")
    return KnowledgeSource(
        org=str(row.get("org") or ""),
        namespace=str(row.get("namespace") or ""),
        name=str(row.get("name") or ""),
        kind=str(row.get("kind") or ""),
        source_id=str(row.get("source_id") or ""),
        config=json_object(str(row.get("config_json") or "")),
        credential_enc=str(row.get("credential_enc") or ""),
        created_by=str(row.get("created_by") or ""),
        active=bool(row.get("active")),
        pending_delete=bool(row.get("pending_delete")),
        created_at=created if isinstance(created, datetime) else None,
        updated_at=updated if isinstance(updated, datetime) else None,
    )


async def list_sources(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[KnowledgeSource]:
    """The namespace's active sources, by name."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND active "
        "ORDER BY name",
        params={"org": org, "ns": namespace},
    )
    return [_source(r) for r in rows]


async def all_active_sources(*, ch: _CHClient | None = None) -> list[KnowledgeSource]:
    """Every active source of every org, in (org, namespace, name) order: the sync
    worker's work list."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL WHERE active ORDER BY org, namespace, name"
    )
    return [_source(r) for r in rows]


async def pending_delete_sources(*, ch: _CHClient | None = None) -> list[KnowledgeSource]:
    """Removed sources whose documents a maintainer asked to retire."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL WHERE NOT active AND pending_delete "
        "ORDER BY org, namespace, name"
    )
    return [_source(r) for r in rows]


async def inactive_namespaces(
    pairs: list[tuple[str, str]], *, ch: _CHClient | None = None
) -> set[tuple[str, str]]:
    """The (org, namespace) pairs among `pairs` whose directory row is inactive."""
    if not pairs:
        return set()
    rows = await (ch or _default_client()).query_all(
        "SELECT org, namespace FROM loom.namespaces FINAL "
        "WHERE (org, namespace) IN {keys:Array(Tuple(String, String))} AND active = false",
        params={"keys": pairs},
    )
    return {(str(r["org"]), str(r["namespace"])) for r in rows}


async def get_source(
    org: str, namespace: str, name: str, *, ch: _CHClient | None = None
) -> KnowledgeSource | None:
    """The active source of that name, or None."""
    row = await (ch or _default_client()).query_one(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String} "
        "AND active LIMIT 1",
        params={"org": org, "ns": namespace, "name": name},
    )
    return _source(row) if row else None


async def latest_source(
    org: str, namespace: str, name: str, *, ch: _CHClient | None = None
) -> KnowledgeSource | None:
    """The latest row of that name, active or not, or None."""
    row = await (ch or _default_client()).query_one(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String} "
        "LIMIT 1",
        params={"org": org, "ns": namespace, "name": name},
    )
    return _source(row) if row else None


async def active_count(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    row = await (ch or _default_client()).query_one(
        f"SELECT count() AS n FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND active",
        params={"org": org, "ns": namespace},
    )
    return int((row or {}).get("n") or 0)


async def insert_source(src: KnowledgeSource, *, ch: _CHClient | None = None) -> None:
    """Write `src` as a new full version row; `created_at` is carried forward. The
    version is left to the column's DEFAULT, the ClickHouse servers' clocks, not the
    writing replica's: the lease-serialized writes stay ordered while those clocks
    differ by less than the lease's handoff between writes."""
    now = naive_utc_now_seconds()
    row = {
        "org": src.org,
        "namespace": src.namespace,
        "name": src.name,
        "source_id": src.source_id,
        "kind": src.kind,
        "config_json": json.dumps(src.config, sort_keys=True),
        "credential_enc": src.credential_enc,
        "created_by": src.created_by,
        "active": src.active,
        "pending_delete": src.pending_delete,
        "created_at": src.created_at or now,
        "updated_at": now,
    }
    await (ch or _default_client()).insert(TABLE, [row])


async def read_states(
    org: str, namespace: str, names: list[str], *, ch: _CHClient | None = None
) -> dict[str, dict[str, str]]:
    """{name: {state_value, detail}} of the sources that have a sync state."""
    if not names:
        return {}
    keys = {state_key(n): n for n in names}
    rows = await (ch or _default_client()).query_all(
        f"SELECT state_key, state_value, detail FROM {STATE_TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND state_key IN {keys:Array(String)}",
        params={"org": org, "ns": namespace, "keys": list(keys)},
    )
    return {
        keys[str(r["state_key"])]: {
            "state_value": str(r.get("state_value") or ""),
            "detail": str(r.get("detail") or ""),
        }
        for r in rows
    }


async def write_state(
    org: str,
    namespace: str,
    name: str,
    *,
    state_value: str,
    detail: dict[str, Any],
    ch: _CHClient | None = None,
) -> None:
    """Write the source's sync state as a new full version row. Its version is the
    column's DEFAULT, the ClickHouse servers' clocks, like the registry's; every
    writer of the `knowledge_source:*` keys leaves it out, and their rows stay
    ordered while those clocks differ by less than the lease's handoff between writes."""
    await (ch or _default_client()).insert(
        STATE_TABLE,
        [
            {
                "org": org,
                "namespace": namespace,
                "state_key": state_key(name),
                "state_value": state_value,
                "detail": json.dumps(detail, sort_keys=True),
                "updated_at": naive_utc_now_seconds(),
            }
        ],
    )


async def source_documents(
    org: str, namespace: str, name: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The source's live documents, newest first, at most DOCUMENTS_LIMIT.
    A tag filter over the namespace's key prefix: `metadata` is a JSON string
    the table's key does not cover."""
    rows = await (ch or _default_client()).query_all(
        "SELECT source_uri, title, "
        "JSONExtractString(metadata, 'source_revision') AS source_revision, ingested_at "
        "FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND has(tags, {tag:String}) "
        "AND actor = {actor:String} AND superseded_at IS NULL "
        "ORDER BY ingested_at DESC, source_uri "
        f"LIMIT {DOCUMENTS_LIMIT}",
        params={"org": org, "ns": namespace, "tag": source_tag(name), "actor": SYNC_WORKER_ACTOR},
    )
    return [dict(r) for r in rows]


# A reference definition whose destination is a picture's bytes, as a native
# document's raw export holds it (`convert.exported_markdown` removes them).
_INLINE_PICTURE = r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*<?data:"


async def source_revisions(
    org: str,
    namespace: str,
    name: str,
    *,
    searchable_only: bool = True,
    pictures: bool = False,
    ch: _CHClient | None = None,
) -> list[dict[str, str]]:
    """Each live document of the source: its address, the store's file id it came
    from, the revision stamp of the ingested copy, and for a kind that keeps them
    (else "") when the copy was read (`read_at`) and the exact hash of what was
    written (`text_sha256`). With `pictures`, also whether the stored text still
    holds a picture's bytes as a reference definition (`inline_pictures`, "1" or
    ""), as a native document stored by an earlier conversion does, and the
    SHA-256 of the stored text (`text_hash`): that reads every document's text, so
    only a full listing asks; the hash is computed only for a flagged copy. One
    statement, a tag filter over the namespace's key prefix like `source_documents`.

    With `searchable_only`, only a version whose chunks are live counts: a write cut
    between its document row and its chunks leaves a version nobody can search, and
    its revision must not let the next turn skip the file. Without it every live
    document counts, which is what removal needs."""
    chunks = (
        "AND (source_uri, version) IN ("
        "SELECT source_uri, doc_version FROM loom.chunks "
        "WHERE org = {org:String} AND namespace = {ns:String} AND has(tags, {tag:String}) "
        f"AND {LIVE_CHUNK_SQL}) "
        if searchable_only
        else ""
    )
    checks = (
        "match(content, {pic:String}) AS inline_pictures, "
        "if(inline_pictures, lower(hex(SHA256(content))), '') AS text_hash"
        if pictures
        else "0 AS inline_pictures, '' AS text_hash"
    )
    rows = await (ch or _default_client()).query_all(
        "SELECT source_uri, JSONExtractString(metadata, 'ingested_from') AS file_id, "
        "JSONExtractString(metadata, 'source_revision') AS source_revision, "
        "JSONExtractString(metadata, 'read_at') AS read_at, "
        "JSONExtractString(metadata, 'text_sha256') AS text_sha256, "
        f"{checks} "
        "FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND has(tags, {tag:String}) "
        "AND actor = {actor:String} AND superseded_at IS NULL "
        f"{chunks}LIMIT {REVISIONS_LIMIT}",
        params={
            "org": org,
            "ns": namespace,
            "tag": source_tag(name),
            "actor": SYNC_WORKER_ACTOR,
            **({"pic": _INLINE_PICTURE} if pictures else {}),
        },
    )
    return [
        {
            "source_uri": str(r.get("source_uri") or ""),
            "file_id": str(r.get("file_id") or ""),
            "source_revision": str(r.get("source_revision") or ""),
            "read_at": str(r.get("read_at") or ""),
            "text_sha256": str(r.get("text_sha256") or ""),
            "inline_pictures": "1" if r.get("inline_pictures") else "",
            "text_hash": str(r.get("text_hash") or ""),
        }
        for r in rows
    ]


async def count_documents(
    org: str, namespace: str, name: str, *, ch: _CHClient | None = None
) -> int:
    """How many live documents the source has."""
    row = await (ch or _default_client()).query_one(
        "SELECT count() AS n FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND has(tags, {tag:String}) "
        "AND actor = {actor:String} AND superseded_at IS NULL",
        params={"org": org, "ns": namespace, "tag": source_tag(name), "actor": SYNC_WORKER_ACTOR},
    )
    return int((row or {}).get("n") or 0)


__all__ = [
    "DOCUMENTS_LIMIT",
    "REVISIONS_LIMIT",
    "TABLE",
    "active_count",
    "all_active_sources",
    "count_documents",
    "get_source",
    "inactive_namespaces",
    "insert_source",
    "latest_source",
    "list_sources",
    "pending_delete_sources",
    "read_states",
    "source_documents",
    "source_revisions",
    "write_state",
]
