"""The ClickHouse writer check `loom cell attach` runs on the first cell: no user, user
directory, object or replica outside the operator's allow lists can write the configured
databases (docs/architecture/cells.md, "Attaching a cell").

A writer the check does not know about could add an org's rows after attach's last scan,
leaving the org with data and no placement. The check reads, on every host of
`settings.ch_cluster` (through `clusterAllReplicas` on several):

- attach's own grants first, since `system.tables` and `system.columns` silently hide what
  the reading user cannot see;
- every user's grants, roles followed and partial revokes ignored (a revoke only narrows a
  grant, so ignoring one over-approximates);
- the user directories, the objects that write the configured databases without a grant on
  them, and the replicas outside the cluster.

It is a point-in-time check: a grant or an object added later passes it, so doctor runs it
on every run. Every refusal is returned as data (`cell_scan.Refusal`)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from server.cli.cell_scan import Refusal, Topology, databases, per_host, quote

# Each privilege attach needs and the grants that imply it, closest first.
_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "SELECT": ("ALL",),
    "SHOW USERS": ("SHOW ACCESS", "ACCESS MANAGEMENT", "ALL"),
    "SHOW TABLES": ("SHOW", "ALL"),
    "SHOW COLUMNS": ("SHOW", "ALL"),
    "REMOTE": ("SOURCES", "ALL"),
    "CLUSTER": ("ALL",),
    "SYSTEM SYNC REPLICA": ("SYSTEM", "ALL"),
    "SYSTEM FLUSH ASYNC INSERT QUEUE": ("SYSTEM FLUSH", "SYSTEM", "ALL"),
}

# Grants that write the configured databases or hand that right on: a user holding one on
# them, or globally, must be one the operator names.
_WRITER_ACCESS = frozenset(
    {
        "ALL",
        "INSERT",
        "ALTER",
        "CREATE",
        "CREATE TABLE",
        "CREATE VIEW",
        "ACCESS MANAGEMENT",
        "CREATE USER",
        "DROP USER",
        "CREATE ROLE",
        "DROP ROLE",
        "ROLE ADMIN",
        "CREATE ROW POLICY",
        "DROP ROW POLICY",
        "CREATE QUOTA",
        "DROP QUOTA",
        "CREATE SETTINGS PROFILE",
        "DROP SETTINGS PROFILE",
    }
)

# The user directories ClickHouse itself keeps; any other (LDAP, Kerberos) defines users the
# check cannot read.
_OWN_DIRECTORIES = frozenset({"users_xml", "local_directory", "replicated", "memory"})

# Objects that write a database named in their definition, without a grant on it.
_FORWARDING = frozenset({"Buffer", "Distributed", "Merge"})
# Engines that write by themselves, from outside ClickHouse.
_STREAMS = frozenset(
    {
        "Kafka",
        "RabbitMQ",
        "NATS",
        "FileLog",
        "S3Queue",
        "AzureQueue",
        "RedisStreams",
        "MaterializedPostgreSQL",
        "MaterializedMySQL",
    }
)


@dataclass(frozen=True)
class Allowed:
    """What the operator names as known: users (`--ch-allow-user`), user directories
    (`--ch-allow-user-directory`), objects as `db.name` (`--ch-allow-object`) and replicas
    as `db.table:replica` (`--ch-allow-replica`)."""

    users: frozenset[str] = frozenset()
    user_directories: frozenset[str] = frozenset()
    objects: frozenset[str] = frozenset()
    replicas: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Grant:
    """One privilege a user holds: `database` None for a global grant, `table` None for the
    whole database; a `database` ending in `*` is a prefix grant."""

    access: str
    database: str | None = None
    table: str | None = None
    grant_option: bool = False

    def covers(self, database: str | None) -> bool:
        """Whether this grant reaches all of `database` (None: every database)."""
        if self.table is not None:
            return False
        return self.database is None or (database is not None and _db_match(self, database))

    def touches(self, database: str) -> bool:
        """Whether this grant reaches any table of `database`."""
        return self.database is None or _db_match(self, database)


def _db_match(grant: Grant, database: str) -> bool:
    assert grant.database is not None
    if grant.database.endswith("*"):
        return database.startswith(grant.database[:-1])
    return grant.database == database


def holds(grants: Iterable[Grant], access: str, database: str | None = None) -> bool:
    """Whether `grants` give `access` on all of `database` (None: globally)."""
    wanted = {access, *_IMPLIED_BY.get(access, ())}
    return any(g.access in wanted and g.covers(database) for g in grants)


# -- reading grants ----------------------------------------------------------------------

_GRANT = re.compile(
    r"^GRANT (?P<privs>.+?) ON (?P<on>(?:`(?:[^`\\]|\\.)*`|[^\s`])+) TO \S.*?"
    r"(?P<opt> WITH GRANT OPTION)?$"
)
_IDENT = re.compile(r"`((?:[^`\\]|\\.)*)`|([^.`]+)")


def _split_privileges(text: str) -> list[str]:
    """`SELECT(a, b), INSERT` → [`SELECT`, `INSERT`]: commas inside a column list stay."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return [p.split("(")[0].strip() for p in out if p.strip()]


def _target(on: str) -> tuple[str | None, str | None] | str:
    """`*.*` and `*` → (None, None), `db.*` → (db, None), `db.t` → (db, t); a source a
    `READ` grant names (`REMOTE`) as itself."""
    if on == "*":
        return (None, None)
    parts = [a or b for a, b in _IDENT.findall(on)]
    if len(parts) != 2:
        return on
    db, table = parts
    return (None if db == "*" else db, None if table == "*" else table)


def parse_show_grants(lines: Iterable[str]) -> list[Grant]:
    """The grants of `SHOW GRANTS FOR <user> FINAL`, whose lines already hold its roles'.
    Role grants (`GRANT r TO u`) and revokes are left out."""
    out = []
    for line in lines:
        m = _GRANT.match(line.strip())
        if m is None:
            continue
        target = _target(m["on"])
        option = bool(m["opt"])
        for access in _split_privileges(m["privs"]):
            if isinstance(target, str):
                # `READ ON REMOTE` is printed `REMOTE ON *.*`; a newer server may print it
                # as written.
                if access in ("READ", "WRITE"):
                    out.append(Grant(target, grant_option=option))
                continue
            out.append(Grant(access, target[0], target[1], option))
    return out


def resolve_grants(
    user: str, grant_rows: Iterable[Mapping[str, Any]], role_rows: Iterable[Mapping[str, Any]]
) -> list[Grant]:
    """`user`'s grants from one host's `system.grants` and `system.role_grants` rows, every
    role it holds followed (default or not, since it can set any)."""
    granted: dict[str, set[str]] = {}
    for r in role_rows:
        holder = r.get("user_name") or r.get("role_name")
        granted.setdefault(str(holder), set()).add(str(r["granted_role_name"]))
    roles: set[str] = set()
    todo = list(granted.get(user, ()))
    while todo:
        role = todo.pop()
        if role not in roles:
            roles.add(role)
            todo.extend(granted.get(role, ()))
    out = []
    for r in grant_rows:
        if r.get("is_partial_revoke"):
            continue
        if r.get("user_name") == user or (r.get("role_name") in roles and not r.get("user_name")):
            out.append(
                Grant(
                    str(r["access_type"]),
                    None if r.get("database") is None else str(r["database"]),
                    None if r.get("table") is None else str(r["table"]),
                    bool(r.get("grant_option")),
                )
            )
    return out


# -- judging -----------------------------------------------------------------------------


def own_problems(grants: list[Grant], topology: Topology, dbs: list[str]) -> list[str]:
    """What attach's own user lacks to read what the check and the scan read."""
    needs: list[tuple[str, str | None]] = [
        ("SELECT", "system"),
        ("SHOW USERS", None),
        ("SHOW TABLES", None),
        ("SHOW COLUMNS", None),
        ("SYSTEM FLUSH ASYNC INSERT QUEUE", None),
    ]
    for db in dbs:
        needs.append(("SELECT", db))
        if topology.hosts:
            needs.append(("SYSTEM SYNC REPLICA", db))
    if topology.multi:
        needs += [("REMOTE", None), ("CLUSTER", None)]
    return [_label(access, db) for access, db in needs if not holds(grants, access, db)]


def _label(access: str, db: str | None) -> str:
    if access == "REMOTE":
        return "READ ON REMOTE"
    return f"{access} ON {db}.*" if db else f"{access} ON *.*"


def writer_reason(grants: Iterable[Grant], dbs: list[str]) -> str:
    """Why a user with `grants` can write the configured databases ('' when it cannot): a
    writing grant on them or globally, else any grant there it can hand on."""
    reaching = [g for g in grants if any(g.touches(db) for db in dbs)]
    for g in reaching:
        if g.access in _WRITER_ACCESS or g.access.startswith("ALTER "):
            return f"holds {g.access} on {_scope(g)}"
    for g in reaching:
        if g.grant_option:
            return f"holds {g.access} WITH GRANT OPTION on {_scope(g)}"
    return ""


def _scope(g: Grant) -> str:
    if g.database is None:
        return "*.*"
    return f"{g.database}.{g.table or '*'}"


_TO = re.compile(r"\bTO\s+(?:`((?:[^`\\]|\\.)*)`|(\w+))\s*\.", re.I)
_NAME = r"(?:`(?:[^`\\]|\\.)*`|\w+)"
_SOURCE = re.compile(
    rf"\b(?:FROM|(?<!ARRAY\s)JOIN)\s+(?:`((?:[^`\\]|\\.)*)`|(\w+))(?:\s*(\()|\s*\.\s*{_NAME})?",
    re.I,
)
_ARGS = re.compile(r"'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`|(\w+)")


def _engine_targets(engine_full: str, dbs: list[str]) -> bool:
    """Whether a Buffer, Distributed or Merge definition names a configured database (a
    Merge's `REGEXP` matching one counts)."""
    head = engine_full.split(")")[0]
    regexp = re.search(r"REGEXP\s*\(\s*'((?:[^'\\]|\\.)*)'", head, re.I)
    if regexp and any(re.search(regexp.group(1), db) for db in dbs):
        return True
    return any((a or b or c) in dbs for a, b, c in _ARGS.findall(head.partition("(")[2]))


def _view_reads_outside(query: str, dbs: list[str]) -> str:
    """The first source outside the configured databases, or table function, a view's query
    reads; '' when there is none."""
    select = re.split(r"\bAS\s+(?=SELECT|WITH|\()", query, maxsplit=1, flags=re.I)
    for m in _SOURCE.finditer(select[-1]):
        name = m.group(1) or m.group(2)
        if m.group(3):
            return f"the table function {name}"
        qualified = "." in m.group(0)
        if qualified and name not in dbs:
            return f"database {name}"
    return ""


def object_problems(rows: Iterable[Mapping[str, Any]], dbs: list[str]) -> dict[str, str]:
    """Each object (`db.name`) that writes the configured databases without a grant on them,
    and why: a Buffer, Distributed or Merge table or materialized view outside them whose
    target is inside, an outside table a view inside reads, a materialized view inside
    reading outside them or through a table function, a stream engine inside."""
    out: dict[str, str] = {}
    for r in rows:
        db, name, engine = str(r["database"]), str(r["name"]), str(r["engine"])
        key = f"{db}.{name}"
        query = str(r.get("create_table_query") or "")
        inside = db in dbs
        if not inside and engine in _FORWARDING and _engine_targets(str(r["engine_full"]), dbs):
            out[key] = f"a {engine} table writing a configured database"
        elif not inside and engine == "MaterializedView":
            to = _TO.search(query.split(" AS ", 1)[0])
            if to and (to.group(1) or to.group(2)) in dbs:
                out[key] = "a materialized view writing a configured database"
        elif inside and engine == "MaterializedView":
            if source := _view_reads_outside(query, dbs):
                out[key] = f"a materialized view reading {source}"
        elif inside and engine in _STREAMS:
            out[key] = f"a {engine} table, which writes by itself"
        if not inside:
            for dep_db, dep in zip(
                r.get("dependencies_database") or [],
                r.get("dependencies_table") or [],
                strict=False,
            ):
                if dep_db in dbs:
                    out.setdefault(f"{dep_db}.{dep}", f"a view reading {key}, outside")
    return out


def extra_replicas(rows: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Per replicated table (`db.table`), the replicas Keeper lists that no host of the
    cluster is (`replica_is_active` keys against every host's `replica_name`, by
    `zookeeper_path`)."""
    ours: dict[str, set[str]] = {}
    listed: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for r in rows:
        path = str(r["zookeeper_path"])
        ours.setdefault(path, set()).add(str(r["replica_name"]))
        listed.setdefault(path, set()).update(str(k) for k in (r["replica_is_active"] or {}))
        names.setdefault(path, f"{r['database']}.{r['table']}")
    out = {}
    for path, keys in listed.items():
        if extra := keys - ours[path]:
            out[names[path]] = extra
    return out


# -- the check ---------------------------------------------------------------------------


async def _own_user(ch: Any) -> str:
    row = await ch.query_one("SELECT currentUser() AS u")
    return str((row or {}).get("u") or "")


async def _show_grants(ch: Any, user: str | None) -> list[Grant]:
    sql = "SHOW GRANTS FINAL" if user is None else f"SHOW GRANTS FOR {quote(user)} FINAL"
    rows = await ch.query_all(sql)
    return parse_show_grants(str(next(iter(r.values()))) for r in rows if r)


async def _process_users(
    ch: Any, topology: Topology, names: Mapping[str, str], own: str
) -> list[Refusal]:
    """On several hosts: each host runs this read as attach's own user, exactly once."""
    rows = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, user FROM {source:processes} "
        "WHERE initial_query_id = initialQueryID()",
    )
    out = []
    for host, found in sorted(rows.items()):
        users = [str(r["user"]) for r in found]
        if users != [own]:
            out.append(
                Refusal(
                    f"host {host}",
                    f"runs attach's reads as {', '.join(users) or 'no user'}, not {own}: set an "
                    "interserver secret, or the cluster's user to attach's",
                )
            )
    return out


async def _host_grants(
    ch: Any, topology: Topology, names: Mapping[str, str]
) -> dict[str, dict[str, list[Grant]]]:
    """Per host, each user it defines and that user's grants."""
    users = await per_host(ch, topology, names, "SELECT hostName() AS h, name FROM {source:users}")
    if not topology.multi:
        return {"": {str(u["name"]): await _show_grants(ch, str(u["name"])) for u in users[""]}}
    grants = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, user_name, role_name, access_type, database, table, "
        "grant_option, is_partial_revoke FROM {source:grants}",
    )
    roles = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, user_name, role_name, granted_role_name FROM {source:role_grants}",
    )
    return {
        host: {
            str(u["name"]): resolve_grants(str(u["name"]), grants[host], roles[host]) for u in found
        }
        for host, found in users.items()
    }


def _on(host: str) -> str:
    return f" on host {host}" if host else ""


async def check_writers(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed
) -> list[Refusal]:
    """Every ClickHouse writer of the configured databases the operator has not named.
    `names` is `cell_scan.host_names()` on a cluster of several hosts, else empty."""
    from server.core.ch import query_tag

    dbs = databases()
    with query_tag("cell.writers"):
        own = await _own_user(ch)
        if missing := own_problems(await _show_grants(ch, None), topology, dbs):
            return [Refusal(f"user {own}", f"attach's own user lacks {', '.join(missing)}")]
        if topology.multi and (wrong := await _process_users(ch, topology, names, own)):
            return wrong
        refusals: list[Refusal] = []
        per_user = await _host_grants(ch, topology, names)
        for host, users in sorted(per_user.items()):
            # Each host keeps its own users: attach's must hold the same on every one.
            lacks = own_problems(users.get(own, []), topology, dbs) if topology.multi else []
            if lacks:
                refusals.append(Refusal(f"user {own}", f"lacks {', '.join(lacks)}{_on(host)}"))
            for user, grants in sorted(users.items()):
                if user not in allowed.users and (why := writer_reason(grants, dbs)):
                    refusals.append(
                        Refusal(f"user {user}", f"{why}{_on(host)}: name it with --ch-allow-user")
                    )
        refusals += await _directories(ch, topology, names, allowed)
        refusals += await _objects(ch, topology, names, allowed, dbs)
        refusals += await _replicas(ch, topology, names, allowed, dbs)
    return refusals


async def _directories(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed
) -> list[Refusal]:
    rows = await per_host(
        ch, topology, names, "SELECT hostName() AS h, name, type FROM {source:user_directories}"
    )
    out = []
    for host, found in sorted(rows.items()):
        for r in found:
            name, kind = str(r["name"]), str(r["type"])
            if kind not in _OWN_DIRECTORIES and name not in allowed.user_directories:
                out.append(
                    Refusal(
                        f"user directory {name}",
                        f"its type, {kind}, defines users this check cannot read{_on(host)}: "
                        "name it with --ch-allow-user-directory",
                    )
                )
    return out


_OBJECTS_SQL = (
    "SELECT hostName() AS h, database, name, engine, engine_full, create_table_query, "
    "dependencies_database, dependencies_table FROM {source:tables} "
    "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema') AND ("
    "engine IN {kinds:Array(String)} OR notEmpty(dependencies_database))"
)


async def _objects(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed, dbs: list[str]
) -> list[Refusal]:
    kinds = sorted(_FORWARDING | _STREAMS | {"MaterializedView"})
    rows = await per_host(ch, topology, names, _OBJECTS_SQL, {"kinds": kinds})
    out = []
    for host, found in sorted(rows.items()):
        for key, why in sorted(object_problems(found, dbs).items()):
            if key not in allowed.objects:
                out.append(
                    Refusal(f"object {key}", f"{why}{_on(host)}: name it with --ch-allow-object")
                )
    return out


async def _replicas(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed, dbs: list[str]
) -> list[Refusal]:
    rows = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, database, table, zookeeper_path, replica_name, "
        "replica_is_active FROM {source:replicas} WHERE database IN {dbs:Array(String)}",
        {"dbs": dbs},
    )
    every = [r for found in rows.values() for r in found]
    out = []
    for table, extra in sorted(extra_replicas(every).items()):
        for replica in sorted(extra):
            if f"{table}:{replica}" not in allowed.replicas:
                out.append(
                    Refusal(
                        f"replica {table}:{replica}",
                        "replicates into the table from outside the cluster, whose users "
                        "this check never reads: name it with --ch-allow-replica",
                    )
                )
    return out
