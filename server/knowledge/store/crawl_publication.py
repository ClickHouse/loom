"""Full source-scoped membership replacement, written only under crawl publication guards."""

from __future__ import annotations

from datetime import datetime

from server.core.ch import own_write_reads, set_query_tag
from server.core.repo_base import _CHClient, _default_client
from server.core.versions import next_version
from server.knowledge.domain.source_crawl import Expansion
from server.knowledge.domain.sources import KnowledgeSource

GROUPS = "loom.knowledge_group_members"


async def groups(
    source: KnowledgeSource,
    expansion: Expansion,
    started: datetime,
    *,
    maximum: int,
    ch: _CHClient | None = None,
) -> None:
    set_query_tag("knowledge.crawl.groups")
    if type(maximum) is not int or maximum <= 0 or not source.mirror:
        raise ValueError("crawl membership bounds refused")
    client = ch or _default_client()
    params = {"org": source.org, "ns": source.mirror, "id": source.source_id}
    with own_write_reads():
        prior = await client.query_all(
            f"SELECT `group` FROM {GROUPS} FINAL WHERE org = {{org:String}} "
            "AND namespace = {ns:String} AND source_id = {id:String} "
            "LIMIT {limit:UInt32}",
            params={**params, "limit": maximum + 1},
        )
    found = dict(expansion.groups)
    names = set(found) | {str(row["group"]) for row in prior}
    if len(prior) > maximum or len(names) > maximum:
        raise ValueError("crawl membership bound exceeded")
    rows = []
    for group in sorted(names):
        key = {
            "org": source.org,
            "namespace": source.mirror,
            "source_id": source.source_id,
            "group": group,
        }
        rows.append(
            {
                **key,
                "name": source.name,
                "members": list(found.get(group, ())),
                "read_at": started,
                "deleted": group not in found,
                "version": await next_version(client, GROUPS, key),
            }
        )
    if rows:
        await client.insert(GROUPS, rows)
