"""What a tracker source must prove before it is registered.

Registration is self-service — any signed-in person of the org — so the
guardrails are rules, not a person's approval:

- **the host is public**: it resolves, and every address it resolves to is
  globally reachable (`ipaddress.is_global`, not multicast) once an IPv4
  address carried inside IPv6 (mapped, compatible, NAT64) is unwrapped,
  unless the deployment allows private hosts (a dev stack). The probe
  connects to the address it validated, so a second lookup cannot swap in
  another;
- **the credential is read-only**: every access type `SHOW GRANTS` and
  `SHOW GRANTS FINAL` name for the user, its roles' grants included, is one a
  read-only user holds (SELECT, SHOW, dictGet, a temporary table), and none is
  granted WITH GRANT OPTION or WITH ADMIN OPTION; a type the check does not
  know is refused. Every granted role is a default role, so
  `system.enabled_roles` shows the whole role chain and any admin option in
  it. The roles are reported back so the registrant can see them;
- **the schema meets the record contract**: the same `describe` + `diff`
  the boot gate runs, through the supplied credential, before anything is
  written — the report is the refusal.

The probe opens one short-lived ClickHouse client of its own (the credential
is not a configured source yet) and closes it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from server.core.hosts import vet_host
from server.settings import settings
from server.tracker.domain.contract import OPTIONAL, REQUIRED
from server.tracker.store.source import qa_intel

log = logging.getLogger("loom.tracker.onboarding")

# The access types a read-only user may hold; any other type is refused.
# Compared upper-case: `SHOW GRANTS` spells some in mixed case.
_READ_ACCESS = frozenset(
    {
        "SELECT",
        "SHOW",
        "SHOW DATABASES",
        "SHOW TABLES",
        "SHOW COLUMNS",
        "SHOW DICTIONARIES",
        "DICTGET",
        "CREATE TEMPORARY TABLE",  # a temp table for a JOIN
    }
)
_ENABLED_ROLES = "SELECT role_name, with_admin_option FROM system.enabled_roles"
# a quoted name (a column, role or user), which may hold `(`, `,` or ` ON `
_QUOTED = re.compile(r"`(?:[^`\\]|\\.)*`|\"(?:[^\"\\]|\\.)*\"")
# an access type with an optional column list: `SELECT`, `SELECT(a, b)`
_ACCESS_TYPE = re.compile(r"([A-Za-z ]+?)\s*(?:\([^()]*\))?")


def _split(text: str, masked: str) -> list[str]:
    """Split `text` on the commas outside parentheses, reading the positions
    from `masked` (the same text with every quoted name blanked out)."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(masked):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    return [t for t in out if t]


async def host_refusal(host: str) -> str:
    """ "" when the host is acceptable; else one sentence saying why not."""
    return (await _vet(host))[0]


async def _vet(host: str) -> tuple[str, str]:
    return await vet_host(host, allow_private=settings.tracker_self_service_allow_private_hosts)


@dataclass
class GrantReview:
    roles: list[str] = field(default_factory=list)  # roles granted, reported back
    refused: list[str] = field(default_factory=list)  # what makes it not read-only
    # granted roles missing from the enabled roles, whose grants go unseen
    not_enabled: list[str] = field(default_factory=list)

    @property
    def read_only(self) -> bool:
        return not self.refused and not self.not_enabled

    def refusal(self, user: str) -> str:
        """The sentences that say why `user` is not read-only and what to do."""
        why = []
        if self.refused:
            why.append(
                f"{user!r} holds more than read access ({', '.join(self.refused[:4])}); loom "
                "takes a read-only user: create one with SELECT only, on the tables you map (or "
                "the views) and system.columns."
            )
        if self.not_enabled:
            why.append(
                f"{user!r} holds roles that are not default roles "
                f"({', '.join(self.not_enabled[:4])}), so loom cannot check what they grant. "
                f"Run: SET DEFAULT ROLE ALL TO {_sql_name(user)}"
            )
        return " ".join(why)


def review_grants(lines: list[str]) -> GrantReview:
    """Parse `SHOW GRANTS` output: `GRANT <types> ON <scope> TO <user>` lines
    name access types; `GRANT <role>[, …] TO <user>` lines name roles;
    `REVOKE` lines only narrow a grant. Any other line is refused."""
    rev = GrantReview()
    for raw in lines:
        line = " ".join(str(raw).split())
        masked = _QUOTED.sub(lambda m: "_" * len(m.group()), line)
        upper = masked.upper()
        if not line or upper.startswith("REVOKE "):
            continue
        unparsed = "`" in masked or '"' in masked or upper.count(" ON ") > 1
        if not upper.startswith("GRANT ") or unparsed:
            rev.refused.append(line)
            continue
        on, to = upper.find(" ON "), upper.find(" TO ")
        end = on if on >= 0 else to
        names = _split(line[6:end], masked[6:end]) if end >= 0 else []
        if not names:
            rev.refused.append(line)
            continue
        if on < 0:
            rev.roles.extend(names)
            if " WITH ADMIN OPTION" in upper:
                rev.refused.extend(f"WITH ADMIN OPTION ({_unquote(r)})" for r in names)
            continue
        if " WITH GRANT OPTION" in upper:
            rev.refused.append("WITH GRANT OPTION")
        for t in names:
            name = _ACCESS_TYPE.fullmatch(_QUOTED.sub("_", t))
            if name is None or name.group(1).strip().upper() not in _READ_ACCESS:
                rev.refused.append(t)
    rev.refused = list(dict.fromkeys(rev.refused))  # FINAL repeats the direct grants
    return rev


_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "0": "\0"}


def _sql_name(name: str) -> str:
    """`name` as SQL takes it: bare when it is a plain identifier, else quoted."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _unquote(name: str) -> str:
    if len(name) > 1 and name[0] == name[-1] and name[0] in '`"':
        return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[1]), name[1:-1])
    return name


def review_roles(rev: GrantReview, enabled: list[dict[str, Any]]) -> None:
    """Add what `system.enabled_roles` shows that `SHOW GRANTS` cannot: a
    granted role that is not enabled (not a default role, so its chain cannot
    be checked) and an admin option on any role in the chain, nested ones
    included."""
    names = {str(r.get("role_name")) for r in enabled}
    rev.not_enabled.extend(r for r in rev.roles if _unquote(r) not in names)
    rev.refused.extend(
        f"WITH ADMIN OPTION ({r.get('role_name')})" for r in enabled if r.get("with_admin_option")
    )
    rev.refused = list(dict.fromkeys(rev.refused))  # a direct role's option shows twice


class ProbeError(RuntimeError):
    """The source could not be reached with the credential given."""


class HostRefused(ProbeError):
    """The host resolved, at connect time, to an address loom does not reach."""


class ReadonlyModeError(ProbeError):
    """The user has `readonly = 1`, which refuses every
    per-query setting — and loom sets `readonly = 2` and a timeout on each
    read, so no read of this source could ever run."""


# The user a team creates for loom, as the page and the contract doc show it:
# readonly = 2 (SELECT only, settings allowed), SELECT on the views and on
# system.columns — nothing on the tables the views read (they are DEFINER views).
READONLY_USER_SQL = (
    "CREATE USER loom_ro IDENTIFIED BY '<password>' SETTINGS readonly = 2;\n"
    "GRANT SELECT ON {database}.* TO loom_ro;\n"
    "GRANT SELECT ON system.columns TO loom_ro;"
)
_READONLY_MODE = ("Code: 164", "READONLY", "in readonly mode")
_ACCESS_DENIED = ("Code: 497", "ACCESS_DENIED", "Not enough privileges")


def _is_readonly_mode(e: BaseException) -> bool:
    text = str(e)
    return "readonly" in text.lower() and any(m in text for m in _READONLY_MODE)


@dataclass
class ProbeResult:
    grants: GrantReview
    described: dict[str, dict[str, str]]
    # Table → why one row could not be read through it with
    # this credential ({} = every described contract table answered)
    unreadable: dict[str, str] = field(default_factory=dict)
    # With `inspect`, the database's tables and their columns
    tables: list[dict[str, Any]] = field(default_factory=list)


async def probe_credential(
    *,
    host: str,
    port: int,
    secure: bool,
    database: str,
    user: str,
    password: str,
    timeout_s: float = 10.0,
    mapping: Any | None = None,
    inspect: bool = False,
) -> ProbeResult:
    """Connect once with the credential, review its grants and describe the
    contract's tables — or, with a `mapping`, the team's
    tables it names, reading one row through each rendered projection; with
    `inspect`, every table of the database too. Raises `ProbeError` when the
    service does not answer, `HostRefused` when the host does not pass
    `host_refusal` at connect time.

    The host is resolved and vetted here again and the client connects to
    that address, with the name kept as the Host header, TLS SNI and the name
    the certificate is checked against: a DNS answer that changes between
    `host_refusal` and the connection cannot point the probe inward."""
    from server.tracker.app import contract

    why, address = await _vet(host)
    if why:
        raise HostRefused(why)
    try:
        client = await asyncio.wait_for(
            qa_intel.connect(
                host,
                address=address,
                port=int(port),
                secure=bool(secure),
                database=database,
                user=user,
                password=password,
                client_name="loom-tracker-onboarding",
                send_receive_timeout=int(timeout_s) + 5,
            ),
            timeout=timeout_s,
        )
    except Exception as e:
        raise ProbeError(f"{host}:{port}/{database} did not accept {user!r}: {e}") from e

    async def query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        res = await client.query(sql, parameters=params or {})
        return [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]

    try:
        # The direct grants name the roles, FINAL adds what those roles grant
        # and the enabled roles carry the admin options of the whole chain
        answers: dict[str, list[dict[str, Any]]] = {}
        try:
            for sql in ("SHOW GRANTS", "SHOW GRANTS FINAL", _ENABLED_ROLES):
                answers[sql] = await asyncio.wait_for(query(sql), timeout=timeout_s)
        except Exception as e:
            if _is_readonly_mode(e):
                raise ReadonlyModeError(
                    f"{user!r} has readonly = 1, which refuses the per-query settings loom sends "
                    "(readonly = 2 and a timeout on every read), so nothing could be read. "
                    f"Run: ALTER USER {_sql_name(user)} SETTINGS readonly = 2 — it still allows "
                    "SELECT only."
                ) from e
            raise ProbeError(f"{sql} refused for {user!r}: {str(e) or type(e).__name__}") from e
        grant_rows = answers["SHOW GRANTS"] + answers["SHOW GRANTS FINAL"]
        grants = review_grants([str(next(iter(r.values()))) for r in grant_rows if r])
        review_roles(grants, answers[_ENABLED_ROLES])
        tables: list[dict[str, Any]] = []
        if inspect:
            tables = await _inspect_tables(query, database, timeout_s)
        if mapping is not None:
            described = await contract.describe_mapped(
                f"probe:{user}", database, mapping, budget_s=timeout_s, query=query
            )
            unreadable = await _read_one_row_each_mapped(
                query, database, mapping, described, timeout_s
            )
        else:
            described = await contract.describe(
                f"probe:{user}", database, budget_s=timeout_s, query=query
            )
            unreadable = await _read_one_row_each(query, database, described, timeout_s)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — a failed close must not mask the probe's result
            pass
    return ProbeResult(grants=grants, described=described, unreadable=unreadable, tables=tables)


_INSPECT_MAX_TABLES = 200


async def _inspect_tables(query: Any, database: str, timeout_s: float) -> list[dict[str, Any]]:
    """The tables this credential can read in `database` —
    name, engine, rows, and columns with their types — for the page to map."""
    rows = await asyncio.wait_for(
        query(
            "SELECT t.name AS table, t.engine AS engine, t.total_rows AS total_rows,"
            " groupArray((c.name, c.type)) AS columns"
            " FROM system.tables AS t"
            " INNER JOIN system.columns AS c ON c.database = t.database AND c.table = t.name"
            " WHERE t.database = {db:String} AND NOT t.is_temporary"
            " GROUP BY t.name, t.engine, t.total_rows ORDER BY t.name LIMIT {cap:UInt32}",
            {"db": database, "cap": _INSPECT_MAX_TABLES},
        ),
        timeout=timeout_s,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        cols = [{"name": str(n), "type": str(t)} for n, t in (r.get("columns") or [])]
        out.append(
            {
                "table": str(r.get("table") or ""),
                "engine": str(r.get("engine") or ""),
                "rows": int(r.get("total_rows") or 0),
                "columns": cols,
            }
        )
    return out


async def _read_one_row_each_mapped(
    query: Any, database: str, mapping: Any, live: dict[str, dict[str, str]], timeout_s: float
) -> dict[str, str]:
    """One row read through each mapped projection — the
    grant on the team's table and every expression's type, proven together."""
    from server.tracker.domain import mapping as m

    out: dict[str, str] = {}
    for logical, tmap in sorted(mapping.tables.items()):
        try:
            sql = m.table_sql(logical, tmap, database, live, final=True)
        except m.MappingError as e:
            out[logical] = str(e)
            continue
        try:
            await asyncio.wait_for(query(f"SELECT 1 FROM {sql} AS t LIMIT 1"), timeout=timeout_s)
        except Exception as e:  # noqa: BLE001 — a read failure is reported per table
            text = str(e)
            if any(k in text for k in _ACCESS_DENIED):
                out[logical] = f"access denied to {tmap.source}"
            else:
                out[logical] = text.splitlines()[0][:240] if text else type(e).__name__
    return out


async def _read_one_row_each(
    query: Any, database: str, described: dict[str, dict[str, str]], timeout_s: float
) -> dict[str, str]:
    """The schema check reads `system.columns` only, so a
    view the credential can see but not read through (a plain view runs with
    the READER's privileges; loom's user has none on the tables it selects
    from) passed the check and failed every read. One `LIMIT 1` per
    contract table the source has, through the same credential."""
    wanted = [t for t in (*REQUIRED, *OPTIONAL) if t in described]
    out: dict[str, str] = {}
    for table in wanted:
        try:
            await asyncio.wait_for(
                query(f"SELECT 1 FROM `{database}`.`{table}` LIMIT 1"), timeout=timeout_s
            )
        except Exception as e:  # noqa: BLE001 — each failure is named, none raises
            text = str(e)
            if any(m in text for m in _ACCESS_DENIED):
                out[table] = "access denied to what the view reads"
            else:
                out[table] = text.splitlines()[0][:200] if text else type(e).__name__
    return out


__all__ = [
    "READONLY_USER_SQL",
    "ReadonlyModeError",
    "GrantReview",
    "HostRefused",
    "ProbeError",
    "ProbeResult",
    "host_refusal",
    "probe_credential",
    "review_grants",
    "review_roles",
]
