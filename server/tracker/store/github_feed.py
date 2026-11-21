"""The team's record tables; no copied content targets the deployment database."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.repo_base import _CHClient
from server.tracker.domain.columns import COLUMNS_READ
from server.tracker.domain.contract import concrete_type, uuid5_sql

IDENTITY = {
    "org": "String",
    "namespace": "String",
    "agent": "String",
    "user_id": "String",
    "session_id": "String",
    "actor": "String",
    "source_class": "String",
}
OWNER_COLUMNS = {**IDENTITY, "owner_uuid": "UUID", "schema_version": "UInt64"}
REPOSITORY_COLUMNS = {
    **IDENTITY,
    "repository": "String",
    "repository_id": "UInt64",
    "owner_uuid": "UUID",
    "token_enc": "String",
    "is_private": "UInt8",
    "available": "UInt8",
    "visible_until": "DateTime64(3, 'UTC')",
    "cursors": "String",
    "max_pages": "UInt64",
    "revision": "UUID",
}
COMMENT_COLUMNS = {
    **IDENTITY,
    "repository": "String",
    "comment_id": "UInt64",
    "item_number": "UInt64",
    "review": "UInt8",
    "is_private": "UInt8",
    "body": "String",
    "author": "String",
    "created_at": "DateTime64(3, 'UTC')",
    "updated_at": "DateTime64(3, 'UTC')",
}
TABLES = ("feed_owner", "feed_repositories", "feed_issues", "feed_prs", "feed_comments")
VIEWS = ("github_issues", "github_prs", "edges")


def owner_tag(org: str, owner_uuid: str) -> str:
    return "loom-github-feed:" + hashlib.sha256(f"{org}\0{owner_uuid}".encode()).hexdigest()


async def claim_owner(ch: _CHClient, org: str, owner_uuid: str) -> None:
    # CREATE's COMMENT grammar requires a literal. The generated tag contains
    # only a fixed prefix and hexadecimal digest, never caller-provided SQL.
    tag = owner_tag(org, owner_uuid)
    await ch.execute(statements()[0] + f" COMMENT '{tag}'")


async def owner_marker(ch: _CHClient) -> str:
    row = await ch.query_one(
        "SELECT comment FROM system.tables WHERE database = currentDatabase() "
        "AND name = 'feed_owner' LIMIT 1"
    )
    return str(row["comment"]) if row else ""


def create_table(name: str, columns: dict[str, str], key: str) -> str:
    if name not in TABLES:
        raise ValueError("unknown feed table")
    fields = ", ".join(f"`{column}` {kind}" for column, kind in columns.items())
    return (
        f"CREATE TABLE IF NOT EXISTS {name} ({fields}, "
        "version DateTime64(3, 'UTC') DEFAULT now64(3)) "
        f"ENGINE = ReplacingMergeTree(version) ORDER BY ({key})"
    )


def item_columns(table: str) -> dict[str, str]:
    return {
        **IDENTITY,
        "repository": "String",
        **{
            name: "Nullable(UInt64)"
            if name in {"has_test", "review_requests_test"}
            else concrete_type(kind)
            for name, kind in COLUMNS_READ[table].items()
        },
    }


def item_view(table: str) -> str:
    if table not in {"github_issues", "github_prs"}:
        raise ValueError("unknown feed view")
    raw = "feed_issues" if table == "github_issues" else "feed_prs"
    kind = "github_issue" if table == "github_issues" else "github_pr"
    private = "greatest(r.is_private, i.is_private, ifNull(c.is_private, 0))"
    ident = uuid5_sql(f"concat('{kind}', if(({private}) != 0, '_private', ''), ':', i.source_id)")
    columns = []
    for column in COLUMNS_READ[table]:
        expression = f"i.`{column}`"
        if column == "id":
            expression = ident
        elif column == "is_private":
            expression = private
        elif column == "body":
            expression = "concat(substringUTF8(i.body, 1, 32768), '\\n', ifNull(c.comments, ''))"
        elif column == "updated_at":
            expression = "greatest(i.version, ifNull(c.published_at, i.version))"
        columns.append(f"{expression} AS `{column}`")
    return f"""CREATE VIEW IF NOT EXISTS {table} DEFINER = CURRENT_USER SQL SECURITY DEFINER AS
SELECT {", ".join(columns)}
FROM {raw} AS i FINAL
INNER JOIN feed_repositories AS r FINAL ON i.org = r.org AND i.repository = r.repository
INNER JOIN feed_owner AS o FINAL ON i.org = o.org AND r.owner_uuid = o.owner_uuid
LEFT JOIN (
    SELECT org, repository, item_number, max(version) AS published_at,
           max(is_private) AS is_private,
           arrayStringConcat(arrayMap(x -> x.4, groupArraySorted(64)(
               tuple(-toUnixTimestamp64Milli(updated_at), -toInt16(review),
                     -toInt128(comment_id), substringUTF8(body, 1, 2048))
           )), '\\n') AS comments
    FROM feed_comments FINAL GROUP BY org, repository, item_number
) AS c ON i.org = c.org AND i.repository = c.repository AND i.number = c.item_number
WHERE r.available = 1 AND r.visible_until > now64(3)"""


def statements() -> list[str]:
    return [
        create_table("feed_owner", OWNER_COLUMNS, "org"),
        create_table("feed_repositories", REPOSITORY_COLUMNS, "org, repository"),
        create_table("feed_issues", item_columns("github_issues"), "org, repository, number"),
        create_table("feed_prs", item_columns("github_prs"), "org, repository, number"),
        create_table("feed_comments", COMMENT_COLUMNS, "org, repository, review, comment_id"),
        item_view("github_issues"),
        item_view("github_prs"),
        """CREATE VIEW IF NOT EXISTS edges DEFINER = CURRENT_USER SQL SECURITY DEFINER AS
SELECT toUUID('00000000-0000-0000-0000-000000000000') AS from_id,
       '' AS from_type, from_id AS to_id, '' AS to_type, '' AS rel_type,
       toDateTime(0) AS created_at WHERE 0""",
    ]


async def clock(ch: _CHClient) -> datetime:
    row = await ch.query_one("SELECT toUInt64(toUnixTimestamp64Milli(now64(3))) AS ms")
    if not row:
        raise ValueError("record clock is unavailable")
    return datetime.fromtimestamp(int(row["ms"]) / 1000, UTC)


async def database_uuid(ch: _CHClient) -> str:
    row = await ch.query_one(
        "SELECT toString(uuid) AS id FROM system.databases WHERE name = currentDatabase() LIMIT 1"
    )
    if not row or not row["id"] or row["id"] == "00000000-0000-0000-0000-000000000000":
        raise ValueError("feed requires a database with a stable UUID")
    return str(row["id"])


async def owner(ch: _CHClient) -> dict[str, Any] | None:
    return await ch.query_one(
        "SELECT * FROM feed_owner FINAL LIMIT 1 SETTINGS select_sequential_consistency = 1"
    )


async def objects(ch: _CHClient) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT name FROM system.tables WHERE database = currentDatabase() LIMIT 100"
    )


async def table_engines(ch: _CHClient) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT name, engine FROM system.tables WHERE database = currentDatabase() "
        "AND name IN {tables:Array(String)} LIMIT 5 SETTINGS select_sequential_consistency = 1",
        {"tables": list(TABLES)},
    )


async def put_repository(ch: _CHClient, row: dict[str, Any]) -> None:
    """Replace control state under the record writer's lease, even after clock rollback."""
    clock_row = await ch.query_one(
        """SELECT toUnixTimestamp64Milli(now64(3)) AS now_ms,
       toUnixTimestamp64Milli(max(version)) AS prior_ms
FROM feed_repositories WHERE org = {org:String} AND repository = {repo:String}
LIMIT 1 SETTINGS select_sequential_consistency = 1""",
        {"org": row["org"], "repo": row["repository"]},
    )
    if not clock_row:
        raise ValueError("repository publication clock is unavailable")
    published_ms = max(int(clock_row["now_ms"]), int(clock_row["prior_ms"]) + 1)
    version = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=published_ms)
    await ch.insert(
        "feed_repositories", [{key: row[key] for key in REPOSITORY_COLUMNS} | {"version": version}]
    )


async def repository(ch: _CHClient, org: str, repo: str) -> dict[str, Any] | None:
    return await ch.query_one(
        "SELECT * FROM feed_repositories FINAL "
        "WHERE org = {org:String} AND lower(repository) = lower({repo:String}) LIMIT 1 "
        "SETTINGS select_sequential_consistency = 1",
        {"org": org, "repo": repo},
    )


async def put_items(
    ch: _CHClient, table: str, rows: list[dict[str, Any]], identity: dict[str, str], repo: str
) -> None:
    """Publish a batch under the record writer's lease."""
    if table not in {"feed_issues", "feed_prs", "feed_comments"}:
        raise ValueError("unknown feed record table")
    if not rows:
        return
    clock_row = await ch.query_one(
        """SELECT toUnixTimestamp64Milli(now64(3)) AS now_ms, max(published_ms) AS prior_ms
FROM (
    SELECT toUnixTimestamp64Milli(max(version)) AS published_ms
    FROM feed_issues WHERE org = {org:String}
    UNION ALL
    SELECT toUnixTimestamp64Milli(max(version)) AS published_ms
    FROM feed_prs WHERE org = {org:String}
    UNION ALL
    SELECT toUnixTimestamp64Milli(max(version)) AS published_ms
    FROM feed_comments WHERE org = {org:String}
) LIMIT 1 SETTINGS select_sequential_consistency = 1""",
        {"org": identity["org"]},
    )
    if not clock_row:
        raise ValueError("record publication clock is unavailable")
    published_ms = max(int(clock_row["now_ms"]), int(clock_row["prior_ms"]) + 1)
    version = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=published_ms)
    await ch.insert(
        table, [row | identity | {"repository": repo, "version": version} for row in rows]
    )
