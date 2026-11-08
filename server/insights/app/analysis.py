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
import contextlib
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from typing import Any
from uuid import UUID

from server.core import leases
from server.core.ch import query_tag_var, statement_timeout
from server.core.ch_errors import (
    error_code,
    error_line,
    is_connection_failure,
    is_passing_state,
    is_transient_network_error,
)
from server.core.ch_request_cost import request_query_id
from server.core.errors import (
    AnalysisUnavailableError,
    BackendTimeoutError,
    OverloadedError,
    RateLimitedError,
    ValidationError,
    error_kind,
)
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


# The ClickHouse codes a statement's own shape causes, answered as a 422 that
# quotes ClickHouse: a refused table, column or function, bad SQL, a missing
# name, a type error, a cap.
_ACCESS_DENIED = 497
_AUTHENTICATION_FAILED = 516
_TOO_MANY_SIMULTANEOUS_QUERIES = 202
_MEMORY_LIMIT_EXCEEDED = 241  # not a setting: ClickHouse's error code
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


class SetupRefused(RuntimeError):
    """A refusal Loom wrote, safe to show as is; any other error shows as its kind."""


_WRITE_LEASE = "analysis.entities"
_LEASE_TTL_S = 60  # not a setting: the statements under it are bounded by its share
_SECRETS_PURPOSE = "analysis.secrets"
_loaded: tuple[bytes, str] | None = None

# Whether this process's last probe passed: None = no answer yet, or its last
# probe got no answer (the op answers OVERLOADED); False = a finding.
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
            raise SetupRefused(
                "loom.analysis_secrets has no row while the analyst user may exist; "
                "`loom ch-setup` draws the secrets"
            )
        drawn = json.dumps({"key": secrets.token_hex(32), "password": secrets.token_hex(24)})
        await store.store_secrets(seal(_SECRETS_PURPOSE, drawn))
        sealed = await store.sealed_secrets()
    opened = _opened(sealed)
    if opened is None:
        raise SetupRefused(
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


def printed_checks() -> list[str]:
    """What `loom ch-setup --print-analyst-sql` tells an admin to run first."""
    return store.printed_checks()


async def own_grant_revokes() -> list[str]:
    """A REVOKE for each grant the analyst's own SHOW GRANTS lists, for the
    printed SQL, whose writer cannot read system.grants. The allowlist is
    granted after them. None before the user exists."""
    try:
        lines = await store.own_grant_lines(password=_password(), extra={})
    except Exception:  # noqa: BLE001 — no user yet, or it refuses this password: nothing to revoke
        return []
    return boundary.revokes_for(lines)


def printed_statements(revokes: list[str]) -> list[str]:
    """The entity statements with `revokes` before the first grant."""
    statements = entity_statements()
    first = next(i for i, s in enumerate(statements) if s.startswith("GRANT "))
    return [*statements[:first], *revokes, *statements[first:]]


def _scope_extra(org: str, namespaces: list[str], key: bytes | None = None) -> dict[str, Any]:
    """The scope one statement carries, signed with `key` (the stored one by
    default). It sends no cap: the caps profile's apply, so a replica whose
    own settings differ from the stored caps (during a roll) still runs."""
    exp = int(time.time()) + scope_ttl_s()
    # The request's own query id where there is one, as every statement Loom
    # sends carries; a retry resends it and replaces its own orphaned attempt.
    query_id = request_query_id(query_tag_var.get()) or str(uuid.uuid4())
    signed = boundary.scope_settings(key or _key(), org, namespaces, exp, query_id)
    return {**signed, "replace_running_query": 1}


async def provision() -> bool:
    """Load the secrets, write the analyst user when the stored entities
    differ from this build's and are not a newer build's, then probe it. Each
    process runs it in the background of its start (`start_probe_loop`) until
    one run's write step finishes, one at a time under the lease. True once
    that step finished (or there is nothing this user may write); False when a
    policy Loom did not write stopped it or it raised, for the loop to run it
    again."""
    try:
        async with lease():
            await load_secrets(may_draw=await store.analyst_user_absent())
            foreign = await _foreign_problem() if await store.can_manage_access() else None
            if foreign == ("", ""):
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
        if foreign and foreign[0]:
            _set_problem(*foreign)
            return False  # nothing written: run again, once the policy is dropped
        await reprobe()
        return True
    except Exception as e:  # noqa: BLE001 — a failed setup is a reported problem, never fatal to boot
        _probe_failed(f"writing or probing {boundary.ANALYST_USER} failed", e)
        return False  # the write may not have finished: run the setup again


async def _current_statements() -> list[str]:
    return entity_statements(await store.granted(), await store.granted_roles())


async def _foreign_problem() -> tuple[str, str]:
    """Why nothing may be written, and the names behind it, or ('', '') when
    nothing stops a write: a second Loom database writing the analyst user, or
    a permissive policy Loom did not write on an allowlisted table. The names
    go to the log and the CLI, never to `/v1/health`."""
    if others := await store.other_deployments():
        return (
            "another Loom database on this ClickHouse writes the analyst user; analysis "
            "needs one Loom per ClickHouse service",
            f"databases: {', '.join(others)}",
        )
    if foreign := await store.foreign_policies():
        return (
            "row policies Loom did not write are on the analysis tables; Loom's policy for "
            "every other user would widen them, so an admin must drop them or make them "
            "restrictive",
            f"policies: {', '.join(foreign)}",
        )
    return "", ""


async def _write_under_lease() -> tuple[str, str]:
    """Write the entities again to repair what the probe found, unless a newer
    build wrote them meanwhile; a replica whose first boot drew no secrets
    draws them here. Returns why it wrote nothing, and the names behind it,
    when something stops a write."""
    async with lease():
        if _loaded is None:
            await load_secrets(may_draw=await store.analyst_user_absent())
        foreign = await _foreign_problem()
        if foreign[0]:
            return foreign
        stored = await store.stored_entities()
        if stored is None or stored.version <= boundary.ENTITY_VERSION:
            await store.write_entities(await _current_statements())
    return "", ""


NOT_MIGRATED = (
    "loom.analysis_secrets does not exist yet: run `loom ch-setup` again once Loom's first "
    "boot has applied its migrations"
)
_LOST_ROW = (
    "loom.analysis_secrets has no row while the analyst user exists, so the key the replicas "
    f"hold is lost: drop the user (`DROP USER {boundary.ANALYST_USER}`) and run `loom ch-setup` "
    "again to draw a new key"
)


async def load_for_print() -> None:
    """Load the secrets for `loom ch-setup --print-analyst-sql`, drawing them
    when there are none. Where Loom's user can see that the analyst user
    exists, a lost row is refused as `rewrite` refuses it; where it cannot, a
    missing row is drawn again, which moves the key."""
    if not await store.secrets_table_exists():
        raise SetupRefused(NOT_MIGRATED)
    async with lease():
        if (
            await store.sealed_secrets() is None
            and await store.can_manage_access()
            and not await store.analyst_user_absent()
        ):
            raise SetupRefused(_LOST_ROW)
        await load_secrets(may_draw=True)


async def rewrite() -> None:
    """Write the analyst user whatever is stored (`loom ch-setup`). It draws
    the secrets only while no analyst user exists, so a lost row never puts
    a new key beside replicas that still hold the old one."""
    if not await store.secrets_table_exists():
        raise SetupRefused(NOT_MIGRATED)
    async with lease():
        if not await store.can_manage_access():
            raise SetupRefused(
                "LOOM_CH_USER cannot manage access entities, so `loom ch-setup` cannot write "
                "the analyst user: have an admin run the SQL `loom ch-setup "
                "--print-analyst-sql` prints"
            )
        if await store.sealed_secrets() is None and not await store.analyst_user_absent():
            raise SetupRefused(_LOST_ROW)
        await load_secrets(may_draw=True)
        stored = await store.stored_entities()
        if stored is not None and stored.version > boundary.ENTITY_VERSION:
            raise SetupRefused(
                f"the analyst user was written by a newer build (version {stored.version}, this "
                f"one writes {boundary.ENTITY_VERSION}); run `loom ch-setup` from that build"
            )
        problem, names = await _foreign_problem()
        if problem:
            raise SetupRefused(f"{problem} ({names})")
        await store.write_entities(await _current_statements())


def scope_ttl_s() -> int:
    """How long a signed scope is good for. The client's retry ladder sends one
    statement at most max(2, 1 + ch_replica_bound_retries) times (a transient
    retry and the replica-bound retries never follow one another), each waiting
    up to its time cap and five seconds; the policy judges the expiry when the
    last attempt starts, so every earlier attempt's wait and a margin for the
    reconnects between them are enough. A retry past it would read no rows."""
    attempts = max(2, 1 + int(settings.ch_replica_bound_retries))
    return int((float(settings.ch_analysis_max_execution_s) + 5) * (attempts - 1)) + 30


@contextlib.asynccontextmanager
async def lease(ch: Any = None) -> AsyncIterator[None]:
    """Hold the analysis lease, with every statement sent under it bounded well
    inside the lease, so a holder that lost it has nothing left to land."""
    async with leases.cluster_lease(_WRITE_LEASE, ttl_s=_LEASE_TTL_S, wait_s=120, ch=ch):
        with statement_timeout(leases.statement_bound(_LEASE_TTL_S)):
            yield


async def reprobe() -> None:
    """Probe the analyst user again. It also re-reads the secrets row: a
    replica moves to a new key only once the canary reads under it. A failure
    a write repairs is written again under the lease, by a replica that
    writes the entities itself. Every ClickHouse answer that is not a clean
    probe is a finding, a repair that fails keeps the finding it was for, and
    a probe that got no answer at all leaves the replica answering OVERLOADED:
    no path serves on a check that did not pass."""
    global _loaded
    try:
        stored = _opened(await store.sealed_secrets())
        if stored is not None and stored != _loaded and await _canary_reads(stored):
            _loaded = stored
        problem, repairable, detail = await _probe()
        manages = await store.can_manage_access()
    except Exception as e:  # noqa: BLE001 — a failed probe is a reported problem
        _probe_failed(f"probing {boundary.ANALYST_USER} failed", e)
        return
    try:
        if problem and repairable and manages:
            blocked, blocked_detail = await _write_under_lease()
            if blocked:
                problem, detail = blocked, blocked_detail
            else:
                problem, _, detail = await _probe()
        elif not problem and manages:
            # A policy added since the last write widens other users, not the
            # analyst, so only this check sees it.
            problem, detail = await _foreign_problem()
    except Exception as e:  # noqa: BLE001 — a failed repair or check keeps the finding it was for
        if not problem:
            _probe_failed(f"checking for row policies {_LOOM_DID_NOT_WRITE} failed", e)
            return
        if isinstance(e, SetupRefused):
            problem = f"{problem}: {e}"  # Loom's own text, which names the fix
        else:
            detail = f"{detail}; the repair or its re-check did not finish: {error_line(e)}"
    _set_problem(problem, detail.lstrip("; "))


_LOOM_DID_NOT_WRITE = "Loom did not write"
# ClickHouse's own socket timeout and network error: no answer reached it.
_NETWORK_CODES = frozenset({209, 210})


def _no_answer(e: BaseException) -> bool:
    """An error that carries no answer about the analyst user: no answer from
    ClickHouse at all (a dropped connection, a client-side wait that ran out,
    a proxy's own status), a server state that clears by itself (too many
    queries, a server short of memory, Keeper lost, a read-only replica), or
    the pool or a lease held elsewhere. Every other ClickHouse answer, a
    statement's own cap included, is a finding."""
    if isinstance(
        e, TimeoutError | leases.LeaseBusy | leases.LeaseUnavailable | BackendTimeoutError
    ):
        return True
    if is_connection_failure(e) or is_passing_state(e) or is_transient_network_error(e):
        return True
    # The same answers as text, from a client that keeps no exception type.
    code = error_code(e)
    if code in _NETWORK_CODES or code == _TOO_MANY_SIMULTANEOUS_QUERIES:
        return True
    return code == _MEMORY_LIMIT_EXCEEDED and ("(total)" in str(e) or "(for user)" in str(e))


_unreached_kind = ""
_unreached_since = 0.0


def _probe_failed(what: str, e: BaseException) -> None:
    """Record a probe or setup that raised. A ClickHouse answer, or a refusal
    Loom wrote, is a finding. No answer at all keeps a finding, and leaves a
    replica that was serving or starting unknown, so it answers OVERLOADED,
    which a client resends, until a probe runs; past three probe intervals the
    problem is named for health and `loom doctor`, still as OVERLOADED."""
    global _ready, _problem, _unreached_kind, _unreached_since
    if isinstance(e, SetupRefused):
        _set_problem(f"{what}: {e}")
        return
    if not _no_answer(e):
        _set_problem(f"{what}: {error_kind(e)}", error_line(e))
        return
    kind = error_kind(e)
    if kind != _unreached_kind:
        _unreached_kind = kind
        log.warning("the analysis probe got no answer (%s): %s", kind, _scrubbed(error_line(e)))
    if _ready is False:
        return  # a finding stands until a probe that runs clears it
    now = time.monotonic()
    if _ready is True or not _unreached_since:
        _unreached_since = now
    # Still unknown, still refused with OVERLOADED; past three intervals the
    # problem is named, so health and `loom doctor` show it.
    stale = now - _unreached_since >= 3 * float(settings.ch_analysis_probe_interval_s)
    _ready = None
    _problem = f"the analysis probe has had no answer for three intervals: {kind}" if stale else ""


def start_probe_loop() -> asyncio.Task[None]:
    """Provision until one run's write step finishes, then probe, once every
    `ch_analysis_probe_interval_s`, in this process's background, so no
    request waits on the lease or on a repair."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("analysis.probe")  # every process runs it, not a worker
        provisioned = False
        while True:
            # A setup that got no answer is run again: only it writes a new
            # version's entities and changed caps.
            if provisioned:
                await reprobe()
            else:
                provisioned = await provision()
            await asyncio.sleep(float(settings.ch_analysis_probe_interval_s))

    return asyncio.get_running_loop().create_task(_run(), name="loom-analysis-probe")


async def _canary_reads(secrets_: tuple[bytes, str]) -> bool:
    """Whether a scope signed with this key reads the canary's one row."""
    key, password = secrets_
    extra = _scope_extra(boundary.CANARY_ORG, [boundary.CANARY_NAMESPACE], key)
    try:
        return await store.count_rows(boundary.CANARY_TABLE, password=password, extra=extra) == 1
    except Exception as e:
        if _no_answer(e):
            raise  # no answer about the key
        return False  # a refused login, a refused or capped read: the key is not in force


def _set_problem(problem: str, detail: str = "") -> None:
    """Record the probe's answer. `problem` is Loom's own text, which
    `/v1/health` shows without a token; `detail` (a driver's text, the names
    of policies or databases) goes only to the log."""
    global _ready, _problem, _unreached_kind, _unreached_since
    _unreached_kind, _unreached_since = "", 0.0  # the probe ran: no answer logs again
    scrubbed = _scrubbed(problem)
    changed = scrubbed != _problem
    _problem = scrubbed
    _ready = not _problem
    if _problem and changed:
        # Once per change: health and `loom doctor` carry the standing state.
        log.warning(
            "insights.query is unavailable: %s%s",
            _problem,
            f" ({_scrubbed(detail)})" if detail else "",
        )


_KEY_LITERAL = re.compile(r"unhex\('[0-9A-Fa-f]+'\)")


def _scrubbed(text: str) -> str:
    """The text with any key pad a ClickHouse message quoted cut out."""
    return _KEY_LITERAL.sub("unhex(…)", text)


async def _probe() -> tuple[str, bool, str]:
    """('', _, '') when the analyst user is held as the allowlist says, else
    why not, whether a write of the entities repairs it, and the names behind
    it for the log only (`/v1/health` shows the first). It runs as the analyst
    itself, so it needs no access management."""
    if _loaded is None:
        return (
            "this replica holds no analyst secrets: loom.analysis_secrets has no row it can open",
            True,
            "",
        )
    if not await _canary_reads(_loaded):
        return "the analyst's policies do not accept this replica's key", True, ""

    def reserved() -> dict[str, Any]:
        # One query id per statement: a statement still running past its
        # client deadline must not hold the id the next one is sent with.
        return _scope_extra(boundary.CANARY_ORG, [boundary.CANARY_NAMESPACE])

    for t in boundary.TABLES:
        # Every allowlisted table's sort key leads with (org, namespace), so
        # this count reads a granule or so, however large the table.
        # No row of a real table sits in the reserved scope: a row here means
        # a policy Loom did not write widens what the analyst reads.
        try:
            admitted = await store.count_rows(t.name, password=_password(), extra=reserved())
        except Exception as e:
            if error_code(e) == _ACCESS_DENIED:
                return f"{boundary.ANALYST_USER} lacks its grants on {t.name}", True, ""
            if _no_answer(e):
                raise
            # A cap the count hit: no policy narrows the reserved scope to nothing.
            return (
                f"{t.name}'s count in the reserved scope failed ({error_kind(e)}): "
                f"{boundary.ANALYST_USER} may have no scope policy on it",
                True,
                error_line(e),
            )
        if admitted:
            # Seen as the analyst, a missing policy (under the server default a
            # user with none reads every row) and a widening one look alike, so
            # a write is tried; what it leaves is a policy for an admin to drop.
            return (
                f"{t.name} admits rows in the reserved scope: {boundary.ANALYST_USER} has no "
                "scope policy on it, or a row policy Loom did not write widens it",
                True,
                "",
            )
    for shown in store.REFUSED_READS:
        try:
            await store.read_refused(shown, password=_password(), extra=reserved())
        except Exception as e:  # a refusal (497) is the expected answer
            if error_code(e) == _ACCESS_DENIED:
                continue
            if _no_answer(e):
                raise  # no answer about the grant
            return f"{boundary.ANALYST_USER} was not refused {shown} ({error_kind(e)})", False, ""
        return f"{boundary.ANALYST_USER} may read {shown}: its grants are too wide", True, ""
    if await store.own_version(password=_password(), extra={}) > boundary.ENTITY_VERSION:
        # A newer build's entities: the allowlist checks below are its to make.
        return "", False, ""
    for table in [*(t.name for t in boundary.TABLES), boundary.CANARY_TABLE]:
        if not await _signed_only(table):
            problem = f"{boundary.ANALYST_USER} reads {table} unsigned: it has no scope policy"
            return problem, True, ""
    granted = await store.own_grants(password=_password(), extra={})
    if extra_grants := sorted(granted - boundary.allowed_columns()):
        # The grants can name other databases and tables: they go to the log only.
        return (
            f"{boundary.ANALYST_USER} holds grants outside the allowlist",
            True,
            f"grants: {extra_grants[:5]}",
        )
    return "", False, ""


async def _signed_only(table: str) -> bool:
    """Whether a read of the table fails without the scope settings, as one
    under Loom's scope policy does. Any other failure is raised."""
    try:
        await store.read_unsigned(table, password=_password())
    except Exception as e:
        if error_code(e) == store.UNKNOWN_SETTING:
            return True
        if _no_answer(e):
            raise
        return False  # read, or refused another way: not under the scope policy
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
    if _ready is None:
        raise OverloadedError(
            "insights.query has no probe answer on this replica yet, or its last probe got "
            "no answer from ClickHouse",
            retry_after_s=5,
        )
    if _ready is not True:
        raise AnalysisUnavailableError(
            "the analyst ClickHouse user is not ready on this replica; `loom doctor` names "
            "the cause and the fix"
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
        # The client waits a little past this replica's time cap, so ClickHouse's
        # own timeout answers first. During a roll that raises the stored cap,
        # a statement can outlive its call by the difference.
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
            raise OverloadedError(
                "ClickHouse is short of memory for analysis statements; retry later",
                retry_after_s=5,
            ) from None
        if refusal is None:
            raise
        raise refusal from None
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
