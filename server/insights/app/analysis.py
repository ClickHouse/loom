"""`insights.query` and `insights.catalog`, and the analyst user's boot step.

A statement runs as the ClickHouse user `loom_analyst`, whose grants, settings
profile and signed row policies hold it to the allowlist and to the request's
org and namespace (`server.insights.domain.analysis`). This module loads the
user's password and the scope key from `loom.analysis_secrets` (drawing them
once), writes the user under the analysis lease, probes it in the background
of every process, and runs statements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import date, datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from typing import Any
from uuid import UUID

from server.core import leases
from server.core.ch import statement_timeout
from server.core.ch_errors import error_code, error_line
from server.core.errors import AnalysisUnavailableError, RateLimitedError, ValidationError
from server.core.secrets_at_rest import seal, unseal
from server.insights.api.models_analysis import (
    AnalysisColumn,
    CatalogColumn,
    CatalogTable,
    InsightsCatalogInput,
    InsightsCatalogOutput,
    InsightsQueryInput,
    InsightsQueryOutput,
)
from server.insights.domain import analysis as boundary
from server.insights.store import analysis as store
from server.observability.event_args import text_digest
from server.settings import settings

log = logging.getLogger(__name__)

# How long a signed scope is good for: one statement's run and its retries.
_SCOPE_TTL_S = 60

# The ClickHouse codes a statement's own shape causes, answered as a 422 that
# quotes ClickHouse: a refused table, column or function, bad SQL, a missing
# name, a type error, a cap.
_ACCESS_DENIED = 497
_AUTHENTICATION_FAILED = 516
_TOO_MANY_SIMULTANEOUS_QUERIES = 202
_MEMORY_LIMIT_EXCEEDED = 241
_STATEMENT_CODES = frozenset(
    {
        _ACCESS_DENIED,
        1,  # UNSUPPORTED_METHOD
        6,  # CANNOT_PARSE_TEXT
        10,  # NOT_FOUND_COLUMN_IN_BLOCK
        16,  # NO_SUCH_COLUMN_IN_TABLE
        36,  # BAD_ARGUMENTS
        42,  # NUMBER_OF_ARGUMENTS_DOESNT_MATCH
        43,  # ILLEGAL_TYPE_OF_ARGUMENT
        44,  # ILLEGAL_COLUMN
        46,  # UNKNOWN_FUNCTION
        47,  # UNKNOWN_IDENTIFIER
        48,  # NOT_IMPLEMENTED
        53,  # TYPE_MISMATCH
        60,  # UNKNOWN_TABLE
        62,  # SYNTAX_ERROR
        69,  # ARGUMENT_OUT_OF_BOUND
        70,  # CANNOT_CONVERT_TYPE
        81,  # UNKNOWN_DATABASE
        115,  # UNKNOWN_SETTING
        158,  # TOO_MANY_ROWS
        159,  # TIMEOUT_EXCEEDED
        164,  # READONLY
        179,  # MULTIPLE_EXPRESSIONS_FOR_ALIAS
        184,  # ILLEGAL_AGGREGATION
        215,  # NOT_AN_AGGREGATE
        241,  # MEMORY_LIMIT_EXCEEDED
        291,  # DATABASE_ACCESS_DENIED: a file() path outside user_files
        307,  # TOO_MANY_BYTES
        352,  # AMBIGUOUS_COLUMN_NAME
        386,  # NO_COMMON_TYPE
        396,  # TOO_MANY_ROWS_OR_BYTES
        452,  # SETTING_CONSTRAINT_VIOLATION
        477,  # INVALID_USAGE_OF_INPUT: input() outside an INSERT
    }
)

_WRITE_LEASE = "analysis.entities"
_LEASE_TTL_S = 60
_SECRETS_PURPOSE = "analysis.secrets"
_loaded: tuple[bytes, str] | None = None
_REPROBE_S = 60.0

# Whether this process's last probe passed: None = not probed yet.
_ready: bool | None = None
_problem = ""

# Statements each org is running on this replica, so one org's slow
# statements cannot take the analyst user's whole share of ClickHouse.
_running: dict[str, int] = {}


def analysis_state() -> tuple[bool | None, str]:
    """(ready, why not) for health and `loom doctor`."""
    return _ready, _problem


def _password() -> str:
    return _secrets()[1]


def _key() -> bytes:
    return _secrets()[0]


def _secrets() -> tuple[bytes, str]:
    if _loaded is None:
        raise AnalysisUnavailableError("the analyst secrets are not loaded on this replica yet")
    return _loaded


async def load_secrets(*, may_draw: bool) -> None:
    """Read the scope key and the analyst password. They are random and never
    change by themselves: a signing-secret rotation only reseals them (`loom
    secrets reseal`). `may_draw` draws them when there are none: `loom
    ch-setup`, or a boot whose server user can see that no analyst user exists
    yet. Run under the analysis lease, so one caller draws them."""
    global _loaded
    sealed = await store.sealed_secrets()
    if sealed is None:
        if not may_draw:
            raise RuntimeError(
                "loom.analysis_secrets has no row while the analyst user may exist; "
                "`loom ch-setup` draws the secrets"
            )
        drawn = json.dumps({"key": secrets.token_hex(32), "password": secrets.token_hex(24)})
        await store.store_secrets(seal(_SECRETS_PURPOSE, drawn))
        sealed = await store.sealed_secrets()
    opened = _opened(sealed)
    if opened is None:
        raise RuntimeError(
            "loom.analysis_secrets does not open with LOOM_JWT_SECRET (set "
            "LOOM_JWT_SECRET_PREVIOUS to the secret it was sealed under and run "
            "`loom secrets reseal`)"
        )
    _loaded = opened


def _opened(sealed: str | None) -> tuple[bytes, str] | None:
    plain = unseal(_SECRETS_PURPOSE, sealed or "")
    if not plain:
        return None
    found = json.loads(plain)
    return bytes.fromhex(found["key"]), str(found["password"])


def caps() -> boundary.Caps:
    return boundary.Caps(
        max_execution_s=float(settings.ch_analysis_max_execution_s),
        max_rows_to_read=int(settings.ch_analysis_max_rows_to_read),
        max_memory_bytes=int(settings.ch_analysis_max_memory_bytes),
        max_result_rows=int(settings.ch_analysis_max_result_rows),
        max_result_bytes=int(settings.ch_analysis_max_result_bytes),
        max_concurrent=int(settings.ch_analysis_max_concurrent),
        max_threads=int(settings.ch_analysis_max_threads),
    )


def entity_statements(
    granted: frozenset[boundary.Grant] = frozenset(), roles: frozenset[str] = frozenset()
) -> list[str]:
    """The SQL that writes the analyst user for this deployment, password and key included."""
    return boundary.entity_sql(store.target_database(), _password(), _key(), caps(), granted, roles)


# What `loom ch-setup --print-analyst-sql` tells an admin to check first.
FOREIGN_POLICIES_SQL = (
    "SELECT short_name, table FROM system.row_policies WHERE database = currentDatabase() "
    "AND table IN ("
    + ", ".join(f"'{t}'" for t in boundary.allowlisted_tables())
    + f") AND NOT is_restrictive AND short_name NOT IN ('{boundary.POLICY_NAME}', "
    f"'{boundary.OTHERS_POLICY_NAME}');"
)


async def own_grant_revokes() -> list[str]:
    """A REVOKE for each grant the analyst's own SHOW GRANTS lists, for the
    printed SQL, whose writer cannot read system.grants. The allowlist is
    granted after them. None before the user exists."""
    try:
        lines = await store.own_grant_lines(password=_password(), extra=_time_cap())
    except Exception:  # noqa: BLE001 — no user yet, or it refuses this password: nothing to revoke
        return []
    out = []
    for line in lines:
        bare = line.replace(" WITH GRANT OPTION", "").replace(" WITH ADMIN OPTION", "")
        if bare.startswith("GRANT ") and " TO " in bare:
            what, _, to = bare[len("GRANT ") :].rpartition(" TO ")
            out.append(f"REVOKE {what} FROM {to}")
    return out


def printed_statements(revokes: list[str]) -> list[str]:
    """The entity statements with `revokes` before the first grant."""
    statements = entity_statements()
    first = next(i for i, s in enumerate(statements) if s.startswith("GRANT "))
    return [*statements[:first], *revokes, *statements[first:]]


def _scope_extra(org: str, namespaces: list[str], key: bytes | None = None) -> dict[str, Any]:
    """The settings one statement carries: the scope signed with `key` (the
    stored one by default), and the time cap as the statement's own
    `max_execution_time` so the class deadline Loom adds to every read never
    asks for more than the profile allows."""
    exp = int(time.time()) + _SCOPE_TTL_S
    signed = boundary.scope_settings(key or _key(), org, namespaces, exp)
    return {**signed, **_time_cap()}


def _time_cap() -> dict[str, Any]:
    limit = float(settings.ch_analysis_max_execution_s)
    return {"max_execution_time": int(limit) if limit.is_integer() else limit}


async def provision() -> None:
    """Load the secrets, write the analyst user when the stored entities
    differ from this build's and are not a newer build's, then probe it. Each
    process runs it once in the background of its start (`start_probe_loop`),
    one at a time under the lease."""
    try:
        async with lease():
            await load_secrets(may_draw=await store.analyst_user_absent())
            foreign = await _foreign_problem() if await store.can_manage_access() else None
            if foreign == "":
                statements = await _current_statements()
                stored = await store.stored_entities()
                if boundary.needs_rewrite(stored, boundary.entity_digest(statements)):
                    await store.write_entities(statements)
                    log.info("analyst user: written (version %s)", boundary.ENTITY_VERSION)
                elif (
                    stored is not None
                    and stored.version <= boundary.ENTITY_VERSION
                    and (await store.stored_caps_digest() != boundary.caps_digest(caps()))
                ):
                    # The caps are deployment settings, written apart from the version.
                    await store.write_entities(boundary.caps_sql(caps()))
        if foreign:
            _set_problem(foreign)
            return
        await reprobe()
    except Exception as e:  # noqa: BLE001 — a failed analyst setup is a reported problem, never fatal to boot
        _set_problem(f"writing or probing {boundary.ANALYST_USER} failed: {error_line(e)}")


async def _current_statements() -> list[str]:
    return entity_statements(await store.granted(), await store.granted_roles())


async def _foreign_problem() -> str:
    """Why nothing may be written while a permissive policy Loom did not write
    sits on an allowlisted table, or '' when none does."""
    if others := await store.other_deployments():
        return (
            f"the analyst user {boundary.ANALYST_USER} is also written by the Loom database "
            f"{others[0]} on this ClickHouse; analysis needs one Loom per ClickHouse service"
        )
    foreign = await store.foreign_policies()
    if not foreign:
        return ""
    return (
        f"row policies Loom did not write are on the analysis tables ({', '.join(foreign[:5])}); "
        "Loom's policy for every other user would widen them, so an admin must drop them or "
        "make them restrictive"
    )


async def _write_under_lease() -> str:
    """Write the entities again to repair what the probe found, unless a newer
    build wrote them meanwhile; a replica whose first boot drew no secrets
    draws them here. Returns why it wrote nothing, when a policy Loom did not
    write is there."""
    async with lease():
        if _loaded is None:
            await load_secrets(may_draw=await store.analyst_user_absent())
        if foreign := await _foreign_problem():
            return foreign
        stored = await store.stored_entities()
        if stored is None or stored.version <= boundary.ENTITY_VERSION:
            await store.write_entities(await _current_statements())
    return ""


async def rewrite() -> None:
    """Write the analyst user whatever is stored (`loom ch-setup`). It draws
    the secrets only while no analyst user exists, so a lost row never puts
    a new key beside replicas that still hold the old one."""
    async with lease():
        if await store.sealed_secrets() is None and not await store.analyst_user_absent():
            raise RuntimeError(
                "loom.analysis_secrets has no row while the analyst user exists, so the key "
                f"the replicas hold is lost: drop the user (`DROP USER {boundary.ANALYST_USER}`) "
                "and run `loom ch-setup` again to draw a new key"
            )
        await load_secrets(may_draw=True)
        stored = await store.stored_entities()
        if stored is not None and stored.version > boundary.ENTITY_VERSION:
            raise RuntimeError(
                f"the analyst user was written by a newer build (version {stored.version}, this "
                f"one writes {boundary.ENTITY_VERSION}); run `loom ch-setup` from that build"
            )
        if foreign := await _foreign_problem():
            raise RuntimeError(foreign)
        await store.write_entities(await _current_statements())


def lease(ch: Any = None) -> Any:
    # Each statement a holder sends times out well inside the lease, so a
    # holder that lost it has nothing left to land.
    return leases.cluster_lease(_WRITE_LEASE, ttl_s=_LEASE_TTL_S, wait_s=120, ch=ch)


async def reprobe() -> None:
    """Probe the analyst user again. It also re-reads the secrets row: a
    replica moves to a new key only once the canary reads under it. A failure
    a write repairs is written again under the lease, by a replica that
    writes the entities itself."""
    global _loaded
    try:
        stored = _opened(await store.sealed_secrets())
        if stored is not None and stored != _loaded and await _canary_reads(stored):
            _loaded = stored
        problem, repairable = await _probe()
        manages = await store.can_manage_access()
        if problem and repairable and manages:
            problem = await _write_under_lease() or (await _probe())[0]
        elif not problem and manages:
            # A policy added since the last write widens other users, not the
            # analyst, so only this check sees it.
            problem = await _foreign_problem()
    except Exception as e:  # noqa: BLE001 — a failed probe is a reported problem
        problem = f"probing {boundary.ANALYST_USER} failed: {error_line(e)}"
    _set_problem(problem)


def start_probe_loop() -> asyncio.Task[None]:
    """Provision once, then probe every minute, in this process's background,
    so no request waits on the lease or on a repair."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("analysis.probe")  # every process runs it, not a worker
        await provision()
        while True:
            await asyncio.sleep(_REPROBE_S)
            await reprobe()

    return asyncio.get_running_loop().create_task(_run(), name="loom-analysis-probe")


async def _canary_reads(secrets_: tuple[bytes, str]) -> bool:
    """Whether a scope signed with this key reads the canary's one row."""
    key, password = secrets_
    extra = _scope_extra(boundary.CANARY_ORG, [boundary.CANARY_NAMESPACE], key)
    try:
        _, _, rows = await store.run_statement(
            f"SELECT count() FROM {store.target_database()}.{boundary.CANARY_TABLE}",
            password=password,
            extra=extra,
        )
    except Exception:  # noqa: BLE001 — a refused login or read means the key is not in force
        return False
    return bool(rows) and rows[0][0] == 1


def _set_problem(problem: str) -> None:
    global _ready, _problem
    _problem = _scrubbed(problem)
    _ready = not _problem
    if _problem:
        log.warning("insights.query is unavailable: %s", _problem)


_KEY_LITERAL = re.compile(r"unhex\('[0-9A-Fa-f]+'\)")


def _scrubbed(text: str) -> str:
    """The text with any key pad a ClickHouse message quoted cut out."""
    return _KEY_LITERAL.sub("unhex(…)", text)


# Statements the analyst must be refused: ClickHouse lets every user read
# system.one and the rows of system.tables and system.columns it holds grants
# on, so the probe asks for tables it must not.
_PROBE_REFUSED = (
    "SELECT count() FROM system.query_log",
    "SELECT count() FROM system.users",
    # LIMIT 1: a LIMIT 0 statement can finish before ClickHouse checks columns.
    "SELECT toString(value) FROM {db}.memory LIMIT 1",
)
_UNKNOWN_SETTING = 115


async def _probe() -> tuple[str, bool]:
    """('', _) when the analyst user is held as the allowlist says, else why
    not and whether a write of the entities repairs it. It runs as the
    analyst itself, so it needs no access management."""
    db = store.target_database()
    if _loaded is None:
        return (
            "this replica holds no analyst secrets: loom.analysis_secrets has no row it can open",
            True,
        )
    if not await _canary_reads(_loaded):
        return "the analyst's policies do not accept this replica's key", True
    reserved = _scope_extra(boundary.CANARY_ORG, [boundary.CANARY_NAMESPACE])
    for t in boundary.TABLES:
        # Every allowlisted table's sort key leads with (org, namespace), so
        # this count reads a granule or so, however large the table.
        # No row of a real table sits in the reserved scope: a row here means
        # a policy Loom did not write widens what the analyst reads.
        try:
            _, _, rows = await store.run_statement(
                f"SELECT count() FROM {db}.{t.name}", password=_password(), extra=reserved
            )
        except Exception as e:
            if error_code(e) == _ACCESS_DENIED:
                return f"{boundary.ANALYST_USER} lacks its grants on {t.name}", True
            raise
        if rows and rows[0][0] != 0:
            # Seen as the analyst, a missing policy (under the server default a
            # user with none reads every row) and a widening one look alike, so
            # a write is tried; what it leaves is a policy for an admin to drop.
            return (
                f"{t.name} admits rows in the reserved scope: {boundary.ANALYST_USER} has no "
                "scope policy on it, or a row policy Loom did not write widens it",
                True,
            )
    for sql in _PROBE_REFUSED:
        try:
            await store.run_statement(sql.format(db=db), password=_password(), extra=reserved)
        except Exception as e:  # noqa: BLE001 — a refusal is the expected answer
            if error_code(e) == _ACCESS_DENIED:
                continue
            return f"{boundary.ANALYST_USER} was not refused `{sql}`: {error_line(e)}", False
        return f"{boundary.ANALYST_USER} can run `{sql}`: its grants are too wide", True
    if await store.own_version(password=_password(), extra=_time_cap()) != boundary.ENTITY_VERSION:
        # Another build's entities: the allowlist checks below are its to make.
        return "", False
    for table in [*(t.name for t in boundary.TABLES), boundary.CANARY_TABLE]:
        if not await _signed_only(db, table):
            return f"{boundary.ANALYST_USER} reads {table} unsigned: it has no scope policy", True
    granted = await store.own_grants(password=_password(), extra=_time_cap())
    if extra_grants := sorted(granted - boundary.allowed_columns()):
        return (
            f"{boundary.ANALYST_USER} holds grants outside the allowlist: {extra_grants[:5]}",
            True,
        )
    return "", False


async def _signed_only(db: str, table: str) -> bool:
    """Whether a read of the table fails without the scope settings, as one
    under Loom's scope policy does. Any other failure is raised."""
    try:
        await store.run_statement(
            f"SELECT 1 FROM {db}.{table} LIMIT 1", password=_password(), extra=_time_cap()
        )
    except Exception as e:
        if error_code(e) == _UNKNOWN_SETTING:
            return True
        raise
    return False


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID | Decimal | IPv4Address | IPv6Address):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _refusal(e: BaseException) -> ValidationError | None:
    """A 422 quoting ClickHouse for an error the statement caused, else None.
    A message that names the scope settings may quote a row policy, whose
    text holds the scope key, so it is never quoted."""
    code = error_code(e)
    if code not in _STATEMENT_CODES:
        return None
    if code == _MEMORY_LIMIT_EXCEEDED and ("(total)" in str(e) or "(for user)" in str(e)):
        return None  # the server's memory, not the statement's cap
    if "SQL_loom_" in str(e) or _KEY_LITERAL.search(str(e)):
        return ValidationError(
            f"ClickHouse refused the statement (code {code}) while checking the analysis "
            "scope; a statement must not set the SQL_loom_* settings"
        )
    if code in {158, 159, 241, 307, 396}:
        return ValidationError(
            f"the statement needs more rows, time or memory than one analysis may use "
            f"({error_line(e)}); add a time or key filter, aggregate, or lower the limit"
        )
    return ValidationError(f"ClickHouse refused the statement: {error_line(e, 400)}")


async def handle_insights_query(input: InsightsQueryInput) -> InsightsQueryOutput:
    """Run one read-only statement as the analyst user, held to the request's
    org and namespace by ClickHouse's row policies."""
    if (input.org, input.namespace) == (boundary.CANARY_ORG, boundary.CANARY_NAMESPACE):
        raise ValidationError("the empty org and namespace are reserved for the analysis probe")
    if "," in input.namespace:
        raise ValidationError("a namespace name cannot hold a comma")
    if _ready is not True:
        raise AnalysisUnavailableError(
            "the analyst ClickHouse user is not ready on this replica"
            + (f": {_problem}" if _problem else "")
            + "; `loom doctor` names the fix"
        )
    if _running.get(input.org, 0) >= int(settings.ch_analysis_max_concurrent_per_org):
        raise RateLimitedError(
            "this org already runs as many analysis statements as it may at once; retry"
        )
    _running[input.org] = _running.get(input.org, 0) + 1
    try:
        return await _run(input)
    finally:
        _running[input.org] -= 1
        if not _running[input.org]:
            del _running[input.org]


async def _run(input: InsightsQueryInput) -> InsightsQueryOutput:
    limit = min(input.limit, int(settings.ch_analysis_max_result_rows))
    sql = boundary.wrap_statement(input.sql, limit)
    extra = _scope_extra(input.org, [input.namespace])
    try:
        # The client waits a little past the time cap, so ClickHouse's own
        # timeout answers first and the statement does not outlive its call.
        with statement_timeout(float(settings.ch_analysis_max_execution_s) + 5):
            names, types, rows = await store.run_statement(sql, password=_password(), extra=extra)
    except Exception as e:
        code = error_code(e)
        if code == _AUTHENTICATION_FAILED:
            raise AnalysisUnavailableError(
                "the analyst user refuses this replica's password; `loom doctor` names the fix"
            ) from None
        if code == _TOO_MANY_SIMULTANEOUS_QUERIES:
            raise RateLimitedError(
                "the analyst user already runs as many statements as it may at once; retry"
            ) from None
        refusal = _refusal(e)
        if code == _MEMORY_LIMIT_EXCEEDED and refusal is None:
            raise AnalysisUnavailableError(
                "ClickHouse is short of memory for analysis statements; retry later"
            ) from None
        if refusal is None:
            raise
        raise refusal from None
    # A policy that refuses this replica's signature reads no rows, and an
    # aggregate over no rows still answers: the scope column says which it was.
    truncated = len(rows) > limit
    rows = rows[:limit]
    return InsightsQueryOutput(
        columns=[AnalysisColumn(name=n, type=t) for n, t in zip(names, types, strict=True)],
        rows=[[_json_value(v) for v in row] for row in rows],
        row_count=len(rows),
        truncated=truncated,
        sql_digest=text_digest(input.sql),
    )


async def handle_insights_catalog(input: InsightsCatalogInput) -> InsightsCatalogOutput:
    """The allowlisted tables and the caps a statement runs under."""
    del input  # the allowlist is the same for every namespace
    return InsightsCatalogOutput(
        tables=[
            CatalogTable(
                name=t.name,
                comment=t.comment,
                notes=t.notes,
                columns=[
                    CatalogColumn(name=c.name, type=c.type, comment=c.comment) for c in t.columns
                ],
            )
            for t in boundary.TABLES
        ],
        max_result_rows=int(settings.ch_analysis_max_result_rows),
        max_execution_s=float(settings.ch_analysis_max_execution_s),
        max_rows_to_read=int(settings.ch_analysis_max_rows_to_read),
    )


__all__ = [
    "analysis_state",
    "entity_statements",
    "handle_insights_catalog",
    "handle_insights_query",
    "provision",
    "start_probe_loop",
]
