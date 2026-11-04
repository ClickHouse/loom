"""The document reads behind `namespace.tune`'s chunking draft: a sample of the
namespace's live documents with what each is cut from, and the agents that wrote them.

Every statement filters on (org, namespace), the prefix of `loom.documents`' sort key,
which is a ReplacingMergeTree read with FINAL. Documents are the namespace's under any
read scope, as every `knowledge.*` read serves them. A sampled text document's text is
read to be cut in process and never returned.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.store.inspect import FORMAT_SQL

_LIVE = (
    "FROM loom.documents FINAL WHERE org = {org:String} AND namespace = {ns:String} "
    "AND superseded_at IS NULL"
)


async def sample_documents(
    org: str,
    namespace: str,
    *,
    limit: int,
    max_chars: int,
    total_chars: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Up to `limit` live documents picked by a hash of their source_uri, so the sample is
    spread over the namespace and the same while it does not change. Each row has its
    title, description, the length of its text (`chars`), its format and stored artifact
    (`artifact_sha256`, set when knowledge.ingest stored one, which is cut instead of the
    text) and, for a document with no artifact whose text is no longer than `max_chars`
    and, with the shorter texts before it in sample order, no longer than `total_chars`,
    its text (`text`, else ''). The pick reads only the sort key; the text is read for the
    picked documents alone."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT source_uri, title, description, ingestion_id, artifact_sha256, doc_format,
               chars,
               if(cut AND sum(if(cut, chars, 0)) OVER (
                      ORDER BY cityHash64(source_uri), source_uri
                      ROWS UNBOUNDED PRECEDING) <= {{total_chars:UInt64}},
                  content, '') AS text
        FROM (
            SELECT source_uri, title, description, ingestion_id, artifact_sha256, content,
                   {FORMAT_SQL} AS doc_format, lengthUTF8(content) AS chars,
                   artifact_sha256 = '' AND chars <= {{max_chars:UInt64}} AS cut
            {_LIVE}
              AND source_uri IN (
                  SELECT source_uri {_LIVE}
                  ORDER BY cityHash64(source_uri), source_uri
                  LIMIT {{lim:UInt32}})
        )
        ORDER BY cityHash64(source_uri), source_uri
        """,
        params={
            "org": org,
            "ns": namespace,
            "lim": int(limit),
            "max_chars": int(max_chars),
            "total_chars": int(total_chars),
        },
    )


async def document_writers(
    org: str, namespace: str, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(agent, rows, total): the agents that wrote the namespace's live documents, most
    documents first; `total` counts every one, past the limit."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT agent, count() AS rows, count() OVER () AS total {_LIVE}
        GROUP BY agent ORDER BY rows DESC, agent LIMIT {{lim:UInt32}}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )
