"""The read behind `knowledge.inspect`'s decisions: the live decision rows of a namespace
that cite a section of one of its documents.

A decision mined from a document (`loom decisions extract`, or any `decision.record`
that names the section) carries `source_resource_id = knowledge:<namespace>/<uri>#<section>`.
The statement filters on (org, namespace), the prefix of loom.memory's sort key, reads
FINAL, and takes the caller's read `scope`.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and
from server.memory.domain.decision_schema import (
    DECISION_TAG,
    OUTCOME_KNOWN_TAG,
    PROPOSED_TAG,
    SCHEMA_NAME,
    SOURCE_CHANGED_TAG,
)
from server.memory.domain.temporal_sql import served_status_sql
from server.memory.store.sql import attested_sql


def document_prefix(namespace: str) -> str:
    """The start of every resource id that cites a document of `namespace`."""
    return f"knowledge:{namespace}/"


async def decisions_by_document(
    org: str,
    namespace: str,
    source_uris: list[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(source_uri, proposed, total, confirmed, source_changed, outcome_known) of the
    decision rows citing a document of the namespace, one row per document and stage:
    `proposed` is 1 for rows tagged `decision:proposed`, 0 for decided ones. Each row goes
    to the longest of `source_uris` whose `<uri>#` its citation starts with, so
    `doc#v2#Intro` is `doc#v2`'s when both are passed; a row citing none of them is counted
    under source_uri ''. Pass every document that extends a wanted one with `#`, or its
    rows are counted under the shorter."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "prefix": document_prefix(namespace),
        # Longest first, ties by name: arrayFirst takes the longest match.
        "uris": sorted(source_uris, key=lambda u: (-len(u), u)),
        "dtype": SCHEMA_NAME,
        "dtag": DECISION_TAG,
        "changed": SOURCE_CHANGED_TAG,
        "known": OUTCOME_KNOWN_TAG,
        "proposed_tag": PROPOSED_TAG,
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT arrayFirst(
                   u -> startsWith(source_resource_id, concat({{prefix:String}}, u, '#')),
                   {{uris:Array(String)}}
               ) AS source_uri,
               toUInt8(has(tags, {{proposed_tag:String}})) AS proposed,
               count() AS total,
               countIf(pinned = 1 OR {attested_sql()}) AS confirmed,
               countIf(has(tags, {{changed:String}})) AS source_changed,
               countIf(has(tags, {{known:String}})) AS outcome_known
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL AND {served_status_sql()}
          AND (memory_type = {{dtype:String}} OR has(tags, {{dtag:String}}))
          AND startsWith(source_resource_id, {{prefix:String}}){scope_and(scope, params)}
        GROUP BY source_uri, proposed
        ORDER BY source_uri, proposed
        """,
        params=params,
    )
