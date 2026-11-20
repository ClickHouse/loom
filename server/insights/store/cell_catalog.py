"""Complete catalog visibility, replica identity and grant checks for cell reads."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from server.core import ch_names
from server.core.cell_ch import (
    CLUSTER_SQL,
    TAG,
    WHOLE_SQL,
    Refusal,
    Refused,
    Topology,
    databases,
    per_host,
)
from server.core.ch import query_tag
from server.core.ch_errors import error_code, error_line
from server.insights.domain.analysis import ANALYST_USER, OTHERS_POLICY_NAME, POLICY_NAME

_ACCESS_DENIED = 497  # ClickHouse's ACCESS_DENIED

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


def _scope(g: Grant) -> str:
    if g.database is None:
        return "*.*"
    return f"{g.database}.{g.table or '*'}"


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


async def _own_user(ch: Any) -> str:
    row = await ch.query_one(f"SELECT currentUser() AS u {WHOLE_SQL}")
    return str((row or {}).get("u") or "")


async def _show_grants(ch: Any) -> list[Grant]:
    """Attach's own grants, as `SHOW GRANTS FINAL` prints them: the one read that needs no
    privilege, so a user lacking `SELECT ON system.*` is told what it lacks."""
    # Pinned: a profile's `break` could end the list before its REVOKE lines.
    rows = await ch.query_all(f"SHOW GRANTS FINAL {WHOLE_SQL}")
    return parse_show_grants(str(next(iter(r.values()))) for r in rows if r)


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


def _as_loom_writes(r: Mapping[str, Any]) -> bool:
    """Whether one of Loom's two policies names exactly whom Loom writes it for: a role in
    either list could cover or exclude attach's user without naming it."""
    if r["short_name"] == POLICY_NAME:
        return not r.get("apply_to_all") and list(r.get("apply_to_list") or []) == [ANALYST_USER]
    return bool(r.get("apply_to_all")) and list(r.get("apply_to_except") or []) == [ANALYST_USER]


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
            elif not _as_loom_writes(p):
                out.append((f"{subject}: {name}", "names other users or roles than Loom writes"))
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
