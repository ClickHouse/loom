"""The ClickHouse writer check `loom cell attach` runs on the first cell: no user, user
directory, object or replica outside the operator's allow lists can write the configured
databases (docs/architecture/cells.md, "Attaching a cell").

A writer the check does not know about could add an org's rows after attach's last scan,
leaving the org with data and no placement. The check reads, on every host of
`settings.ch_cluster` (through `clusterAllReplicas` on several):

- attach's own grants first, since `system.tables` and `system.columns` silently hide what
  the reading user cannot see: a partial revoke of any of them refuses too;
- every user's grants, roles followed and partial revokes ignored (a revoke only narrows a
  grant, so ignoring one over-approximates); a grant that creates tables, acts as another
  user, or connects out (`_ANYWHERE_ACCESS`) counts on any database, since it writes the
  configured databases without a grant on them;
- the user directories, the objects that write the configured databases without a grant on
  them or bring in what lies outside them (a view, a forwarding table or an external engine
  inside them reading outside; a networked table outside them; a KeeperMap table outside
  them sharing an inside one's keys), and the replicas outside the cluster.

The check is a list of known write routes judged at one point in time, not a proof: a grant
or an object added later, another server on the same Keeper (its KeeperMap tables on an
inside table's root, the users of a replica named with `--ch-allow-replica` or one added
later), and a route outside ClickHouse's grant model pass it, so doctor runs it again on
every run. Every refusal is returned as data (`cell_ch.Refusal`), but for a host that
answers outside the cluster's names, which raises `cell_ch.Refused`."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from server.cli.cell_ch import (
    CLUSTER_SQL,
    TAG,
    WHOLE_SQL,
    WRITERS_TAG,
    Refusal,
    Refused,
    Topology,
    databases,
    per_host,
)
from server.core import ch_names
from server.core.ch import query_tag
from server.core.ch_errors import error_code, error_line
from server.insights.domain.analysis import OTHERS_POLICY_NAME, POLICY_NAME

_ACCESS_DENIED = 497  # ClickHouse's ACCESS_DENIED

# Each privilege attach needs and the grants that imply it, closest first.
_IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "SELECT": ("ALL",),
    "SHOW USERS": ("SHOW ACCESS", "ACCESS MANAGEMENT", "ALL"),
    "SHOW ROLES": ("SHOW ACCESS", "ACCESS MANAGEMENT", "ALL"),
    "SHOW ROW POLICIES": ("SHOW ACCESS", "ACCESS MANAGEMENT", "ALL"),
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
        "INSERT",
        "ALTER",
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

# Grants that write the configured databases wherever they are held, on any database:
# - creating a table, a temporary one included: a Distributed table on another database, or
#   a temporary Distributed table, forwards its inserts to a remote shard over the cluster's
#   connection, as the cluster's configured user, and the server that takes the insert does
#   not check INSERT on the destination (a Buffer table and a materialized view do: the
#   insert, or the view's creation, refuses without it);
# - a source that connects to an address the query names (REMOTE, URL, MYSQL, POSTGRES and
#   the other networked ones, or SOURCES, all of them; `READ` and `WRITE ON <source>` on
#   newer servers): it can reach this server again, over its own protocols, as another user,
#   as `INSERT INTO FUNCTION remote(…)` does;
# - IMPERSONATE (`EXECUTE AS`): a statement run as another user;
# - SET DEFINER and ALLOW SQL SECURITY NONE: a view or a materialized view created to run as
#   another user, or with no check at all (ALTER VIEW MODIFY SQL SECURITY needs SET DEFINER
#   to name another user).
#
# Not counted, each running as the caller with no address of its own: FILE, SQLITE and DISK
# (files on the server, never a statement), CREATE FUNCTION (a SQL function is an
# expression; an executable one is defined in the server's files), NAMED COLLECTION (its
# credentials reach a server only through a source or a table engine counted above), and
# SYSTEM RELOAD CONFIG and USERS (they read files the grant model does not reach).
_NETWORKED_SOURCES = frozenset(
    {"REMOTE", "URL", "MONGO", "REDIS", "MYSQL", "POSTGRES", "ODBC", "JDBC", "HDFS", "S3",
     "HIVE", "AZURE", "KAFKA", "NATS", "RABBITMQ", "YTSAURUS", "ARROW FLIGHT", "BIGQUERY",
     "ELASTICSEARCH", "SOURCES"}
)  # fmt: skip
_ANYWHERE_ACCESS = frozenset(
    {
        "ALL",
        "CREATE",
        "CREATE TABLE",
        "CREATE VIEW",
        "CREATE TEMPORARY TABLE",
        "CREATE ARBITRARY TEMPORARY TABLE",
        "READ",
        "WRITE",
        "IMPERSONATE",
        "SET DEFINER",
        "ALLOW SQL SECURITY NONE",
        *_NETWORKED_SOURCES,
    }
)

# Grants that make a row policy, which hides rows from a reader it does not cover.
_POLICY_ACCESS = frozenset({"CREATE ROW POLICY", "ALTER ROW POLICY"})

# The user directories ClickHouse itself keeps; any other (LDAP) defines users the check
# cannot read.
_OWN_DIRECTORIES = frozenset({"users_xml", "local_directory", "replicated", "memory"})

# Objects that write a database named in their definition, without a grant on it.
_FORWARDING = frozenset({"Alias", "Buffer", "Distributed", "Merge"})
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
    whole database; a `database` ending in `*` is a prefix grant. `revoked` marks a partial
    revoke, which narrows a grant: no grant counts it, and attach's own user refuses one."""

    access: str
    database: str | None = None
    table: str | None = None
    grant_option: bool = False
    revoked: bool = False

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
    return any(g.access in wanted and g.covers(database) and not g.revoked for g in grants)


def narrowing(grants: Iterable[Grant], access: str, database: str | None = None) -> list[Grant]:
    """The partial revokes in `grants` of `access`, or of a grant implying it, on any part
    of `database` (None: anywhere)."""
    wanted = {access, *_IMPLIED_BY.get(access, ())}
    return [
        g
        for g in grants
        if g.revoked
        and g.access in wanted
        and (database is None or g.database is None or _db_match(g, database))
    ]


# -- reading grants ----------------------------------------------------------------------

_GRANT = re.compile(
    r"^(?P<verb>GRANT|REVOKE) (?P<privs>.+?) ON (?P<on>(?:`(?:[^`\\]|\\.)*`|[^\s`])+) "
    r"(?:TO|FROM) \S.*?(?P<opt> WITH GRANT OPTION)?$"
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
    """The grants of `SHOW GRANTS FINAL` (attach's own), whose lines already hold its
    roles', and its partial revokes (`revoked`). Role grants (`GRANT r TO u`) and the
    revokes of a grant option alone are left out."""
    out = []
    for line in lines:
        m = _GRANT.match(line.strip())
        if m is None or m["privs"].startswith("GRANT OPTION FOR "):
            continue
        target = _target(m["on"])
        option, revoked = bool(m["opt"]), m["verb"] == "REVOKE"
        for access in _split_privileges(m["privs"]):
            if isinstance(target, str):
                # `READ ON REMOTE` is printed `REMOTE ON *.*`; a newer server may print it
                # as written.
                if access in ("READ", "WRITE"):
                    out.append(Grant(target, grant_option=option, revoked=revoked))
                continue
            out.append(Grant(access, target[0], target[1], option, revoked))
    return out


def resolve_grants(
    user: str,
    grant_rows: Iterable[Mapping[str, Any]],
    role_rows: Iterable[Mapping[str, Any]],
    *,
    default_only: bool = False,
) -> list[Grant]:
    """`user`'s grants from one host's `system.grants` and `system.role_grants` rows, every
    role it holds followed (default or not, since it can set any). `default_only` follows
    only the user's default roles, the ones a new session has (attach's own reads run in
    one), and every role those hold: `granted_role_is_default` marks the user's own grants,
    while a role's grant of another always passes it on."""
    # A user and a role may share a name: each holder is keyed by its kind.
    granted: dict[tuple[str, str], set[str]] = {}
    for r in role_rows:
        holder = (
            ("user", str(r["user_name"]))
            if r.get("user_name")
            else ("role", str(r.get("role_name")))
        )
        if default_only and holder[0] == "user" and r.get("granted_role_is_default") == 0:
            continue
        granted.setdefault(holder, set()).add(str(r["granted_role_name"]))
    roles: set[str] = set()
    todo = list(granted.get(("user", user), ()))
    while todo:
        role = todo.pop()
        if role not in roles:
            roles.add(role)
            todo.extend(granted.get(("role", role), ()))
    out = []
    for r in grant_rows:
        if r.get("user_name") == user or (r.get("role_name") in roles and not r.get("user_name")):
            out.append(_row_grant(r))
    return out


def _row_grant(r: Mapping[str, Any]) -> Grant:
    """A `system.grants` row as a Grant: `READ ON REMOTE` is access `READ` on object
    `REMOTE` there, read as `REMOTE` (as `SHOW GRANTS` prints it), and a wildcard database
    as a prefix."""
    access = str(r["access_type"])
    if access in ("READ", "WRITE") and r.get("access_object"):
        access = str(r["access_object"])
    database = None if r.get("database") is None else str(r["database"])
    if database is not None and r.get("is_wildcard") and not database.endswith("*"):
        database += "*"
    return Grant(
        access,
        database,
        None if r.get("table") is None else str(r["table"]),
        bool(r.get("grant_option")),
        bool(r.get("is_partial_revoke")),
    )


# -- judging -----------------------------------------------------------------------------


def own_problems(grants: list[Grant], topology: Topology, dbs: list[str]) -> list[str]:
    """What attach's own user lacks to read what the check and the scan read."""
    needs: list[tuple[str, str | None]] = [
        ("SELECT", "system"),
        ("SHOW USERS", None),
        ("SHOW ROLES", None),
        ("SHOW ROW POLICIES", None),
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
    out = []
    for access, scope in needs:
        if not holds(grants, access, scope):
            out.append(_label(access, scope))
        elif revokes := narrowing(grants, access, scope):
            # A revoke hides part of what the checks read, which reads short silently.
            out.append(
                f"{_label(access, scope)} (narrowed by REVOKE "
                + ", ".join(f"{g.access} ON {_scope(g)}" for g in revokes)
                + ")"
            )
    return out


def _label(access: str, db: str | None) -> str:
    if access == "REMOTE":
        return "READ ON REMOTE"
    return f"{access} ON {db}.*" if db else f"{access} ON *.*"


def writer_reason(grants: Iterable[Grant], dbs: list[str]) -> str:
    """Why a user with `grants` can write the configured databases ('' when it cannot): a
    grant that writes them from anywhere (`_ANYWHERE_ACCESS`), a writing grant on them or
    globally, else any grant there it can hand on."""
    grants = [g for g in grants if not g.revoked]  # a revoke only narrows: ignored
    for g in grants:
        if g.access in _ANYWHERE_ACCESS:
            return f"holds {g.access} on {_scope(g)}"
    reaching = [g for g in grants if any(g.touches(db) for db in dbs)]
    for g in reaching:
        if g.access in _WRITER_ACCESS or g.access.startswith("ALTER "):
            return f"holds {g.access} on {_scope(g)}"
    for g in reaching:
        if g.grant_option:
            return f"holds {g.access} WITH GRANT OPTION on {_scope(g)}"
    # A row policy created later hides rows from the next scan or check, as a write made
    # later adds them: on the configured databases or the system tables the checks read.
    for g in grants:
        if g.access in _POLICY_ACCESS and (g.touches("system") or any(g.touches(db) for db in dbs)):
            return f"holds {g.access} on {_scope(g)}, which can hide rows from the scan"
    return ""


def _scope(g: Grant) -> str:
    if g.database is None:
        return "*.*"
    return f"{g.database}.{g.table or '*'}"


_TO = re.compile(r"\bTO\s+(?:`((?:[^`\\]|\\.)*)`|(\w+))\s*\.", re.I)
_NAME = r"(?:`(?:[^`\\]|\\.)*`|[A-Za-z_]\w*)"
# `CREATE … VIEW <name>`: a view's `TO` target follows it, and its name may hold anything.
_VIEW_HEAD = re.compile(
    rf"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+|WINDOW\s+|LIVE\s+)?VIEW\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?:{_NAME}\s*\.\s*)?{_NAME}",
    re.I,
)
# A call: of a table function (`system.table_functions`) it is a read through it, wherever
# it stands in the query (a comma join, a subquery), so a scalar function of the same name
# counts too, failing closed.
_CALL = re.compile(r"(?<![\w.`])(\w+)\s*\(")
# Any qualified name: the schema's own views qualify only by a configured database or a
# table alias they define, so any other qualifier (another database, a column) counts as an
# outside read, failing closed.
_QUALIFIED = re.compile(rf"(?<![\w.`])({_NAME})\s*\.\s*{_NAME}")
# The names a query defines for its tables and subqueries: `FROM t AS a`, `JOIN db.t a`,
# `(SELECT …) AS s`, `WITH s AS (…)`.
_ALIAS = re.compile(
    rf"(?:\b(?:FROM|JOIN)\s+(?:{_NAME}\s*\.\s*)?{_NAME}|\))\s+(?:AS\s+)?({_NAME})"
    rf"|(?:\bWITH|,)\s*({_NAME})\s+AS\s*\(",
    re.I,
)
_NOT_ALIAS = frozenset(
    {"WHERE", "PREWHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "ON", "USING", "FINAL",
     "SAMPLE", "ARRAY", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "ANY", "ALL",
     "ASOF", "SEMI", "ANTI", "GLOBAL", "JOIN", "UNION", "SETTINGS", "FORMAT", "WINDOW",
     "QUALIFY", "AS", "SELECT", "FROM", "WITH", "INTERVAL"}
)  # fmt: skip
# A dictionary or Join table read by name, which a string literal holds.
_BY_NAME = re.compile(r"\b(?:dict\w*|joinGet\w*)\s*\(\s*('((?:[^'\\]|\\.)*)')?", re.I)
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'")
_ARGS = re.compile(r"'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`|(\w+\(\))|(\w+)")

# Objects judged by the query they run: a view's or a window view's.
_QUERIES = frozenset({"View", "MaterializedView", "WindowView", "LiveView"})
# Objects that write to their target without the writer holding a grant on it.
_WRITING_VIEWS = frozenset({"MaterializedView", "WindowView"})
# Engines inside the configured databases that keep their rows there and read nothing else;
# any other engine inside (a URL, S3, File, remote database or dictionary) reads outside
# them, and an insert through a view on it brings that in.
_LOCAL_ENGINES = frozenset(
    {"KeeperMap", "Memory", "Log", "TinyLog", "StripeLog", "Set", "Join", "Null",
     "EmbeddedRocksDB"}
)  # fmt: skip
# Which argument of a forwarding engine names the database it reads or writes.
_DATABASE_ARG = {"Alias": 0, "Buffer": 0, "Distributed": 1, "Merge": 0}
# Engines that open a connection of their own, to an address and with credentials their
# definition holds: one outside the configured databases can point back at this server as
# another user, so every one outside refuses unless the operator names it (whether it points
# back is not judged).
_NETWORKED = frozenset(
    {"URL", "MySQL", "PostgreSQL", "MaterializedPostgreSQL", "ODBC", "JDBC", "MongoDB",
     "Redis", "S3", "S3Queue", "GCS", "OSS", "COSN", "HDFS", "Hive", "AzureBlobStorage",
     "AzureQueue", "Remote", "RemoteSecure", "ArrowFlight", "BigQuery", "Elasticsearch",
     "YTsaurus", "Kafka", "NATS", "RabbitMQ", "Executable", "ExecutablePool", "QueryRunner",
     "Iceberg", "IcebergS3", "IcebergAzure", "IcebergHDFS", "DeltaLake", "DeltaLakeS3",
     "DeltaLakeAzure", "Hudi", "Paimon", "PaimonS3", "PaimonAzure", "PaimonHDFS"}
)  # fmt: skip


def _engine_args(engine_full: str) -> list[str]:
    inner = engine_full.partition("(")[2].rpartition(")")[0]
    return [a or b or c or d for a, b, c, d in _ARGS.findall(inner)]


def _engine_database(engine: str, engine_full: str) -> str:
    """The database a Buffer, Distributed or Merge definition names: '' for the table's own
    (`currentDatabase()`), `REGEXP` for a pattern."""
    if engine == "Merge" and re.search(r"\bREGEXP\s*\(", engine_full, re.I):
        return "REGEXP"
    args = _engine_args(engine_full)
    i = _DATABASE_ARG[engine]
    if i >= len(args) or args[i] == "currentDatabase()":
        return ""
    # An Alias may name its target as one `'db.table'` string.
    return args[i].split(".", 1)[0] if engine == "Alias" else args[i]


def _engine_targets(engine_full: str, dbs: list[str]) -> bool:
    """Whether a Buffer, Distributed or Merge definition names a configured database (a
    Merge's `REGEXP` matching one counts)."""
    head = engine_full.split(")")[0]
    regexp = re.search(r"REGEXP\s*\(\s*'((?:[^'\\]|\\.)*)'", head, re.I)
    if regexp and any(re.search(regexp.group(1), db) for db in dbs):
        return True
    return any(a in dbs or a.split(".", 1)[0] in dbs for a in _engine_args(engine_full))


def _view_reads_outside(query: str, dbs: list[str], functions: frozenset[str]) -> str:
    """What a view's query reads outside the configured databases: a table function
    (`functions`, lowercase), a dictionary or Join table not named in them, or any name
    qualified by something other than one of them or an alias the query defines; '' when
    there is none."""
    select = re.split(r"\bAS\s+(?=SELECT|WITH|\()", query, maxsplit=1, flags=re.I)[-1]
    for by_name in _BY_NAME.finditer(select):
        if by_name.group(1) is None:
            # A name built at run time can name anything.
            return "a dictionary or table named by an expression"
        db, dot, _ = by_name.group(2).partition(".")
        if not dot or db.strip("`") not in dbs:
            return f"the dictionary or table {by_name.group(2)}"
    code = _STRING.sub("''", select)
    for call in _CALL.finditer(code):
        if call.group(1).lower() in functions:
            return f"the table function {call.group(1)}"
    # A table reference's database is judged as it is: an alias of the same name never hides
    # it.
    for db in _referenced_databases(code):
        if db not in dbs:
            return f"database {db}"
    aliases = {
        name.strip("`")
        for m in _ALIAS.finditer(code)
        if (name := m.group(1) or m.group(2)) and name.upper() not in _NOT_ALIAS
    }
    for q in _QUALIFIED.finditer(code):
        name = q.group(1).strip("`")
        if name not in dbs and name not in aliases:
            return f"database {name}"
    return ""


_FROM = re.compile(r"\bFROM\b", re.I)
# A qualified name ClickHouse keeps as one quoted identifier: a parameterized view's call is
# stored as `` `db.view`(a = 1) ``.
_DOTTED = r"`((?:[^`\\.]|\\.)+)\.(?:[^`\\]|\\.)*`"
_JOINED = re.compile(rf"(?<!ARRAY\s)\bJOIN\s+(?:({_NAME})\s*\.\s*{_NAME}|{_DOTTED}\s*\()", re.I)
# What ends a FROM list: the next clause, or a join.
_FROM_END = re.compile(
    r"\b(?:WHERE|PREWHERE|GROUP|ORDER|LIMIT|HAVING|SETTINGS|UNION|FORMAT|WINDOW|QUALIFY|"
    r"INNER|LEFT|RIGHT|FULL|CROSS|JOIN|ARRAY|ANY|ALL|ASOF|SEMI|ANTI|GLOBAL|PASTE|ON|USING|"
    r"INTERSECT|EXCEPT)\b",
    re.I,
)
_LEADING_REF = re.compile(rf"\s*(?:({_NAME})\s*\.\s*{_NAME}|{_DOTTED}\s*\()")


def _referenced_databases(code: str) -> set[str]:
    """The database of every qualified table reference: each item of a FROM list (a comma
    join included) and each joined table."""
    out = {(m.group(1) or m.group(2)).strip("`") for m in _JOINED.finditer(code)}
    for m in _FROM.finditer(code):
        depth, start, items = 0, m.end(), []
        i = start
        while i < len(code):
            ch = code[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0 and ch == ",":
                items.append(code[start:i])
                start = i + 1
            elif depth == 0 and _FROM_END.match(code, i):
                break
            i += 1
        items.append(code[start:i])
        for item in items:
            if ref := _LEADING_REF.match(item):
                out.add((ref.group(1) or ref.group(2)).strip("`"))
    return out


def keeper_root(engine_full: str) -> str:
    """A KeeperMap table's root path with one slash between parts and none at the ends."""
    args = _engine_args(engine_full)
    return "/".join(p for p in (args[0] if args else "").split("/") if p)


def object_problems(
    rows: Iterable[Mapping[str, Any]],
    dbs: list[str],
    functions: frozenset[str] = frozenset(),
    cluster: str = "",
) -> dict[str, str]:
    """Each object (`db.name`) that writes the configured databases without a grant on them,
    or brings in what lies outside them, and why: outside them, a Buffer, Distributed or
    Merge table, or a materialized or window view, whose target is inside, and a table a
    view inside reads; inside them, a Distributed table over another cluster than `cluster`,
    a view reading outside them (`_view_reads_outside`, with `functions` the table
    functions), a forwarding table naming a database outside them, and any other engine but
    the MergeTree family and `_LOCAL_ENGINES`."""
    rows = list(rows)
    # The keys of a KeeperMap table live under its root path in Keeper, shared by every
    # table with that root.
    roots = {
        keeper_root(str(r.get("engine_full") or "")): f"{r['database']}.{r['name']}"
        for r in rows
        if r["engine"] == "KeeperMap" and r["database"] in dbs
    }
    out: dict[str, str] = {}
    for r in rows:
        db, name, engine = str(r["database"]), str(r["name"]), str(r["engine"])
        key = f"{db}.{name}"
        if db in dbs:
            if why := _inside_problem(r, engine, dbs, functions, cluster):
                out[key] = why
            continue
        query = str(r.get("create_table_query") or "")
        if engine == "KeeperMap":
            if shared := roots.get(keeper_root(str(r.get("engine_full") or ""))):
                out[key] = f"a KeeperMap table sharing {shared}'s keys"
        elif engine in _NETWORKED:
            out[key] = f"a table of engine {engine}, which connects out with credentials of its own"
        elif engine in _FORWARDING and _engine_targets(str(r.get("engine_full") or ""), dbs):
            out[key] = f"a table of engine {engine} writing a configured database"
        elif engine in _WRITING_VIEWS:
            head = _VIEW_HEAD.match(query)
            to = _TO.search(query, head.end()) if head else None
            if to and (to.group(1) or to.group(2)) in dbs:
                out[key] = f"a {_kind(engine)} writing a configured database"
        for dep_db, dep in zip(
            r.get("dependencies_database") or [], r.get("dependencies_table") or [], strict=False
        ):
            if dep_db in dbs:
                out.setdefault(f"{dep_db}.{dep}", f"a view reading {key}, outside")
    return out


def _kind(engine: str) -> str:
    return {"MaterializedView": "materialized view", "WindowView": "window view"}.get(
        engine, engine
    )


def _inside_problem(
    r: Mapping[str, Any], engine: str, dbs: list[str], functions: frozenset[str], cluster: str
) -> str:
    if engine in _QUERIES:
        source = _view_reads_outside(str(r.get("create_table_query") or ""), dbs, functions)
        return f"a {_kind(engine)} reading {source}" if source else ""
    if engine == "Distributed":
        # Its shards are the servers of the cluster it names: another reads outside them.
        named = (_engine_args(str(r.get("engine_full") or "")) or [""])[0]
        if not cluster or named != cluster:
            return (
                f"a table of engine Distributed reading cluster {named or '(none)'}, not "
                f"{cluster or 'LOOM_CH_CLUSTER (unset)'}"
            )
    if engine in _FORWARDING:
        target = _engine_database(engine, str(r.get("engine_full") or ""))
        if target and target not in dbs:
            return f"a table of engine {engine} reading database {target}"
        return ""
    if engine in _STREAMS:
        return f"a table of engine {engine}, which writes by itself"
    if engine.endswith("MergeTree") or engine in _LOCAL_ENGINES:
        return ""
    return f"a table of engine {engine}, which reads outside the configured databases"


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
    row = await ch.query_one(f"SELECT currentUser() AS u {WHOLE_SQL}")
    return str((row or {}).get("u") or "")


async def _show_grants(ch: Any) -> list[Grant]:
    """Attach's own grants, as `SHOW GRANTS FINAL` prints them: the one read that needs no
    privilege, so a user lacking `SELECT ON system.*` is told what it lacks."""
    # Pinned: a profile's `break` could end the list before its REVOKE lines.
    rows = await ch.query_all(f"SHOW GRANTS FINAL {WHOLE_SQL}")
    return parse_show_grants(str(next(iter(r.values()))) for r in rows if r)


# Per host, each user it defines and that user's grants.
HostGrants = dict[str, dict[str, list[Grant]]]


async def host_names(ch: Any, topology: Topology) -> dict[str, str]:
    """Each answering host's OS hostname (an opaque group key) and the one configured name
    it answers for in the cluster; Refused unless there is one group per listed host, each
    with exactly one name, the names together the listing. The topology's own refusals (two
    servers under one host name, several shards) come first, then what attach's own user
    lacks by its own `SHOW GRANTS FINAL` (this read needs READ ON REMOTE), before any read
    through `clusterAllReplicas`."""
    if refusals := topology.refusals():
        raise Refused(refusals)
    with query_tag(TAG):
        if lacking := await first_refusals(ch, topology):
            raise Refused(lacking)
        rows = await ch.query_all(
            "SELECT hostName() AS h, groupArrayIf(host_name, is_local) AS names "
            "FROM clusterAllReplicas({c:String}, system.clusters) WHERE cluster = {c:String} "
            f"GROUP BY h {CLUSTER_SQL}",
            {"c": topology.cluster},
        )
    where = f"cluster {topology.cluster}"
    refusals = []
    out: dict[str, str] = {}
    for r in rows:
        names = sorted(str(n) for n in r["names"])
        if not names:
            refusals.append(
                Refusal(
                    f"host {r['h']}",
                    f"this host's address in {where} does not resolve to a local interface",
                )
            )
        elif len(names) > 1:
            refusals.append(
                Refusal(
                    f"host {r['h']}",
                    f"this host answers for several entries in {where} ({', '.join(names)}): "
                    "two local entries, or two hosts with one OS hostname",
                )
            )
        else:
            out[str(r["h"])] = names[0]
    named = sorted(out.values())
    if not refusals and (len(named) != len(set(named)) or set(named) != topology.hosts):
        missing = sorted(topology.hosts - set(named))
        refusals.append(
            Refusal(
                where,
                "the answering hosts are not the listed ones"
                + (f" (no answer for {', '.join(missing)})" if missing else ""),
            )
        )
    if refusals:
        raise Refused(refusals)
    return out


async def first_refusals(ch: Any, topology: Topology) -> list[Refusal]:
    """What attach's own user lacks by its own `SHOW GRANTS FINAL`, the one read that needs
    no privilege. It prints the grants of every role the user holds, a role a session does
    not have included: this first look names what is missing; the rows decide."""
    if missing := own_problems(await _show_grants(ch), topology, databases()):
        own = await _own_user(ch)
        return [Refusal(f"user {own}", f"attach's own user lacks {', '.join(missing)}")]
    return []


async def own_refusals(
    ch: Any, topology: Topology, names: Mapping[str, str]
) -> tuple[list[Refusal], HostGrants]:
    """What attach's own user lacks to read what the checks read (without it `system.tables`
    and `system.columns` would silently list less than there is), and every user's grants
    per host once they could be read. First its own `SHOW GRANTS FINAL`; then, on several
    hosts, that every host runs the reads as this user; then that user's grants as each host
    keeps them; then the row policies, which would hide rows from its scan."""
    dbs = databases()
    if lacking := await first_refusals(ch, topology):
        return lacking, {}
    own = await _own_user(ch)
    try:
        if topology.multi and (wrong := await _process_users(ch, topology, names, own)):
            return wrong, {}
        per_user, own_grants = await _host_grants(ch, topology, names, own)
        out = []
        for host, grants in sorted(own_grants.items()):
            # Each host keeps its own users: attach's must hold the same on every one,
            # through its default roles alone, as its sessions have them.
            if lacks := own_problems(grants, topology, dbs):
                out.append(Refusal(f"user {own}", f"lacks {', '.join(lacks)}{_on(host)}"))
        if not out:
            out = await _row_policies(ch, topology, names, own, dbs)
    except Exception as e:  # only a refused read is judged here; the rest pass to the caller
        if error_code(e) != _ACCESS_DENIED:
            raise
        reason = (
            f"attach's own user cannot read what the checks read ({error_line(e)}); a grant "
            "held only through a role that is not a default role does not reach its sessions"
        )
        return [Refusal(f"user {own}", reason)], {}
    return out, per_user


async def _row_policies(
    ch: Any, topology: Topology, names: Mapping[str, str], own: str, dbs: list[str]
) -> list[Refusal]:
    rows = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, short_name, database, table, select_filter, is_restrictive, "
        "apply_to_all, apply_to_list, apply_to_except FROM {source:row_policies} "
        "WHERE database IN {dbs:Array(String)} OR database = 'system'",
        {"dbs": dbs},
    )
    out = []
    for host, found in sorted(rows.items()):
        for subject, why in row_policy_problems(found, own):
            out.append(Refusal(subject, f"{why}{_on(host)}"))
    return out


def _applies(r: Mapping[str, Any], user: str) -> bool:
    if r.get("apply_to_all"):
        return user not in (r.get("apply_to_except") or [])
    return user in (r.get("apply_to_list") or [])


def row_policy_problems(rows: Iterable[Mapping[str, Any]], own: str) -> list[tuple[str, str]]:
    """What hides rows from attach's own user, per row policy on a table of the configured
    databases or a system table. Any policy on a system table refuses: the checks read them.
    Once a table has a row policy, a user that no permissive policy covers reads none of its
    rows, and a restrictive one filters them: either shortens the scan silently. Loom's own
    pair passes (`analysis.POLICY_NAME` for the analyst, and `analysis.OTHERS_POLICY_NAME`
    letting every other user read every row) while it covers attach's user as written; every
    other policy refuses."""
    tables: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        tables.setdefault(f"{ch_names.logical(str(r['database']))}.{r['table']}", []).append(r)
    out = []
    for table, policies in sorted(tables.items()):
        subject = f"row policy on {table}"
        if table.startswith("system."):
            # The checks read the system tables: a policy there hides what they read. Loom
            # writes none.
            out += [
                (f"{subject}: {p['short_name']}", "a row policy hides rows from the checks")
                for p in policies
            ]
            continue
        others = [p for p in policies if p["short_name"] == OTHERS_POLICY_NAME]
        for p in policies:
            name = str(p["short_name"])
            if name not in (POLICY_NAME, OTHERS_POLICY_NAME):
                out.append((f"{subject}: {name}", "a row policy hides rows from the scan"))
            elif name == POLICY_NAME and _applies(p, own):
                out.append((f"{subject}: {name}", f"the analyst's policy applies to {own}"))
        if not any(
            not p.get("is_restrictive") and str(p.get("select_filter")) == "1" and _applies(p, own)
            for p in others
        ):
            out.append((subject, f"no permissive policy `USING 1` covers {own}: it reads no row"))
    return out


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
    ch: Any, topology: Topology, names: Mapping[str, str], own: str
) -> tuple[HostGrants, dict[str, list[Grant]]]:
    """Per host, each user it defines and that user's grants, from `system.grants` and
    `system.role_grants` on every topology (every granted role followed, default or not);
    and per host the grants of `own` through its default roles alone."""
    users = await per_host(ch, topology, names, "SELECT hostName() AS h, name FROM {source:users}")
    # Every column: `access_object` and `is_wildcard` are read where the server has them.
    grants = await per_host(ch, topology, names, "SELECT hostName() AS h, * FROM {source:grants}")
    roles = await per_host(
        ch, topology, names, "SELECT hostName() AS h, * FROM {source:role_grants}"
    )
    every = {
        host: {
            str(u["name"]): resolve_grants(str(u["name"]), grants[host], roles[host]) for u in found
        }
        for host, found in users.items()
    }
    own_grants = {
        host: resolve_grants(own, grants[host], roles[host], default_only=True) for host in users
    }
    return every, own_grants


def _on(host: str) -> str:
    return f" on host {host}" if host else ""


async def check_writers(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed
) -> list[Refusal]:
    """Every ClickHouse writer of the configured databases the operator has not named.
    `names` is `host_names()` on a cluster of several hosts, else empty. Raises
    `cell_ch.Refused` when a host answers outside the cluster's names; every other refusal
    is returned."""
    dbs = databases()
    with query_tag(WRITERS_TAG):
        lacking, per_user = await own_refusals(ch, topology, names)
        if lacking:
            return lacking
        refusals: list[Refusal] = []
        for host, users in sorted(per_user.items()):
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
    "engine IN {kinds:Array(String)} OR notEmpty(dependencies_database) "
    "OR (database IN {dbs:Array(String)} AND NOT endsWith(engine, 'MergeTree')))"
)


async def _objects(
    ch: Any, topology: Topology, names: Mapping[str, str], allowed: Allowed, dbs: list[str]
) -> list[Refusal]:
    kinds = sorted(_FORWARDING | _WRITING_VIEWS | _NETWORKED | {"KeeperMap"})
    rows = await per_host(ch, topology, names, _OBJECTS_SQL, {"kinds": kinds, "dbs": dbs})
    # Each host's own: a build may add one.
    listed = await per_host(
        ch, topology, names, "SELECT hostName() AS h, name FROM {source:table_functions}"
    )
    functions = frozenset(str(r["name"]).lower() for found in listed.values() for r in found)
    out = []
    for host, found in sorted(rows.items()):
        for key, why in sorted(object_problems(found, dbs, functions, topology.cluster).items()):
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
