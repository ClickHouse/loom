"""Isolated tenants for the integration suite, and their cleanup.

`unique_org` mints an org id no other run can hold. The suite purges every
row of every org it minted when the session ends, straight from ClickHouse
across every MergeTree table with an `org` column, then counts again to prove
nothing is left. Only ids of the minted shape are ever purged, so a shared
dev stack keeps every other tenant's rows.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from typing import Any

import pytest

_ORG_SHAPE = re.compile(r"^it-[a-z0-9][a-z0-9-]{0,40}-[0-9a-f]{8}$")
_minted: set[str] = set()

# Server-side buffers flush every half second or so (namespace write marks
# every 5 s); a row flushed after the purge is retried once past this window.
_LATE_FLUSH_S = 6.0
_MUTATION_WAIT_S = 120.0


def unique_org(label: str) -> str:
    """A fresh org id `it-<label>-<8 hex>`, registered for the session purge."""
    org = f"it-{label}-{secrets.token_hex(4)}"
    if not _ORG_SHAPE.match(org):
        raise ValueError(f"org label {label!r} must be lowercase letters, digits and dashes")
    _minted.add(org)
    return org


def minted_orgs() -> frozenset[str]:
    return frozenset(_minted)


NO_CH_TARGET = (
    "Set LOOM_CH_HOST (with LOOM_CH_PORT, LOOM_CH_USER, LOOM_CH_PASSWORD and "
    "LOOM_CH_DATABASE) to the disposable ClickHouse the server under test writes to; "
    "the integration suite never falls back to localhost, where a shared store may live."
)


def ch_target_configured() -> bool:
    return bool(os.environ.get("LOOM_CH_HOST"))


def ch_target() -> dict[str, Any]:
    """Connection arguments for the explicitly configured store; skips without one."""
    if not ch_target_configured():
        pytest.skip(NO_CH_TARGET)
    port = int(os.environ.get("LOOM_CH_PORT", "8123"))
    return {
        "host": os.environ["LOOM_CH_HOST"],
        "port": port,
        "username": os.environ.get("LOOM_CH_USER", "loom"),
        "password": os.environ.get("LOOM_CH_PASSWORD", ""),
        "database": os.environ.get("LOOM_CH_DATABASE", "loom"),
        "secure": port == 8443 or os.environ.get("LOOM_CH_SECURE", "").lower() == "true",
    }


def ch_client_from_env(**kwargs: Any) -> Any:
    """A ClickHouse client on the store the server under test writes to;
    `kwargs` add to or replace its arguments (another user, say). The only
    place the suite builds one; the unit gate test_integration_suite_targets
    holds every other module to that."""
    import clickhouse_connect

    return clickhouse_connect.get_client(**{**ch_target(), **kwargs})


_REFRESH_TARGET = r"\bREFRESH\b[^(]*?\bTO\s+`?\w+`?\.`?(\w+)"
_REFRESH_APPEND = r"\bREFRESH\b[^(]*\bAPPEND\b"


def memory_rows(client: Any, org: str) -> list[tuple[Any, ...]]:
    """Each of the org's loom.memory rows at its latest version (FINAL), as the fields a
    write changes: a "wrote nothing" check compares two of these. A merge leaves it as
    it was, and so does the entity extractor, which rewrites only `version` and
    `entities`; a write a caller meant stamps `updated_at`."""
    return [
        tuple(r)
        for r in client.query(
            "SELECT agent, user_id, session_id, memory_key, memory_id, content_sha256, status,"
            " pinned, tags, tombstoned_at, actor, source_class, reinforcement_count, updated_at"
            " FROM loom.memory FINAL WHERE org = {org:String}"
            " ORDER BY agent, user_id, session_id, memory_key",
            parameters={"org": org},
        ).result_rows
    ]


def _org_tables(client: Any, database: str) -> list[str]:
    rows = client.query(
        """
        SELECT DISTINCT t.name
        FROM system.tables AS t
        INNER JOIN system.columns AS c ON c.database = t.database AND c.table = t.name
        WHERE t.database = %(db)s AND c.name = 'org' AND t.engine LIKE '%%MergeTree%%'
          AND t.name NOT IN (
            -- a table a refreshable view replaces whole: a mutation on it never
            -- finishes, and its rows are rebuilt from the tables purged here
            SELECT extract(v.create_table_query, %(target)s)
            FROM system.tables AS v
            WHERE v.database = %(db)s AND v.engine = 'MaterializedView'
              AND match(v.create_table_query, %(target)s)
              AND NOT match(v.create_table_query, %(append)s)
          )
        ORDER BY t.name
        """,
        parameters={"db": database, "target": _REFRESH_TARGET, "append": _REFRESH_APPEND},
    ).result_rows
    return [str(r[0]) for r in rows]


def residue(client: Any, database: str, orgs: frozenset[str]) -> dict[str, int]:
    """Rows still held for `orgs`, by table."""
    left: dict[str, int] = {}
    for table in _org_tables(client, database):
        n = client.query(
            f"SELECT count() FROM `{database}`.`{table}` WHERE org IN %(orgs)s",
            parameters={"orgs": sorted(orgs)},
        ).result_rows[0][0]
        if n:
            left[table] = int(n)
    return left


def purge(client: Any, database: str, orgs: frozenset[str]) -> dict[str, int]:
    """Delete every row of `orgs`; return the residue after a few late-flush retries."""
    foreign = sorted(o for o in orgs if not _ORG_SHAPE.match(o))
    if foreign:
        raise ValueError(f"refusing to purge orgs the suite did not mint: {foreign}")
    if not orgs:
        return {}
    _delete(client, database, _org_tables(client, database), orgs)
    left = residue(client, database, orgs)
    # A response's follow-up work can still be writing when the run ends, so
    # one retry isn't always past the last flush.
    for _ in range(3):
        if not left:
            break
        time.sleep(_LATE_FLUSH_S)
        _delete(client, database, sorted(left), orgs)
        left = residue(client, database, orgs)
    return left


def _delete(client: Any, database: str, tables: list[str], orgs: frozenset[str]) -> None:
    """One asynchronous DELETE mutation per table, then wait for all of them:
    issued one by one with mutations_sync they cost about three times as long."""
    started = client.query("SELECT now()").result_rows[0][0]
    for table in tables:
        client.command(
            f"ALTER TABLE `{database}`.`{table}` DELETE WHERE org IN %(orgs)s",
            parameters={"orgs": sorted(orgs)},
        )
    deadline = time.monotonic() + _MUTATION_WAIT_S
    while True:
        pending = client.query(
            """
            SELECT table, latest_fail_reason FROM system.mutations
            WHERE database = %(db)s AND NOT is_done AND create_time >= %(started)s
              AND command LIKE '%%DELETE WHERE org IN%%'
            """,
            parameters={"db": database, "started": started},
        ).result_rows
        if not pending:
            return
        failed = {str(t): str(why) for t, why in pending if why}
        if failed or time.monotonic() > deadline:
            raise AssertionError(f"purge mutations did not finish: {failed or pending}")
        time.sleep(0.1)
