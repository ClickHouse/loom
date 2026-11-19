"""Read evidence for checked mirror legs; every key is scoped by org and namespace."""

from __future__ import annotations

from typing import Any, NamedTuple

from server.core import pg
from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.serving import Serving
from server.knowledge.store.pg_documents import decode_row

# Content and arbitrary metadata are read only after admission, at the checked version.
_METADATA = (
    "org",
    "namespace",
    "source_uri",
    "doc_id",
    "title",
    "tags",
    "version",
    "revision",
    "access_json",
    "superseded_at",
    "ingested_at",
    "content_sha256",
    "format",
    "parser",
    "coverage",
    "ingestion_id",
    "artifact_sha256",
    "blob_path",
    "blob_size",
    "blob_content_type",
    "original_filename",
    "blob_erased_at",
)


class ReadHead(NamedTuple):
    row: dict[str, Any]
    serving: Serving


async def active_sources(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    if not namespaces:
        return []
    return await (ch or _default_client()).query_all(
        "SELECT namespace, name, credential_mode, toString(source_id) AS source_id "
        "FROM loom.knowledge_sources FINAL "
        "WHERE org = {org:String} AND namespace IN {names:Array(String)} "
        "AND active AND access = 'mirrored' ORDER BY namespace, name",
        params={"org": org, "names": sorted(set(namespaces))},
    )


async def listings(
    org: str, pairs: list[tuple[str, str]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    return await (ch or _default_client()).query_all(
        "SELECT namespace, source_id, started_at FROM loom.knowledge_source_listings FINAL "
        "WHERE org = {org:String} AND (namespace, source_id) "
        "IN {pairs:Array(Tuple(String, String))} "
        "LIMIT {limit:UInt32}",
        params={"org": org, "pairs": sorted(set(pairs)), "limit": len(set(pairs))},
    )


async def metadata(
    org: str, pairs: list[tuple[str, str]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    return await (ch or _default_client()).query_all(
        f"SELECT {', '.join(_METADATA)} FROM loom.documents FINAL "
        "WHERE org = {org:String} AND (namespace, source_uri) "
        "IN {pairs:Array(Tuple(String, String))} "
        "LIMIT {limit:UInt32}",
        params={"org": org, "pairs": sorted(set(pairs)), "limit": len(set(pairs))},
    )


async def group_members(
    org: str, groups: list[tuple[str, str, str]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    if not groups:
        return []
    return await (ch or _default_client()).query_all(
        "SELECT namespace, source_id, `group`, members, read_at, deleted "
        "FROM loom.knowledge_group_members FINAL WHERE org = {org:String} "
        "AND (namespace, source_id, `group`) IN {groups:Array(Tuple(String, String, String))} "
        "LIMIT {limit:UInt32}",
        params={"org": org, "groups": sorted(set(groups)), "limit": len(set(groups))},
    )


async def heads(org: str, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], ReadHead]:
    pairs = sorted({p for p in pairs if not pg.unstorable(p)})
    if not pairs or pg.unstorable(org):
        return {}
    fields = ", ".join(f"'{col}', h.row -> '{col}'" for col in _METADATA)
    rows = await pg.fetch(
        f"SELECT h.namespace, h.source_uri, jsonb_build_object({fields}) AS row, "
        "h.deleted, h.revision, h.chunks_revision, h.chunks_version "
        "FROM loom.knowledge_heads h "
        "JOIN unnest($2::text[], $3::text[]) AS s(namespace, source_uri) "
        "ON (h.namespace, h.source_uri) = (s.namespace, s.source_uri) WHERE h.org = $1",
        org,
        [ns for ns, _ in pairs],
        [uri for _, uri in pairs],
    )
    return {
        (str(r["namespace"]), str(r["source_uri"])): ReadHead(
            decode_row(r["row"]),
            Serving(
                bool(r["deleted"]),
                int(r["revision"]),
                None if r["chunks_revision"] is None else int(r["chunks_revision"]),
                None if r["chunks_version"] is None else int(r["chunks_version"]),
            ),
        )
        for r in rows
    }
