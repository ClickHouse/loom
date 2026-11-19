"""Read-only clients for the tracker source's ClickHouse service.

The tracker record lives on another ClickHouse service and refreshes
itself every five to ten minutes; loom reads it LIVE, inside the request,
through `server/tracker/store/source/`, and never copies it. One credential
per source (`LOOM_TRACKER_CREDENTIALS_JSON`) serves every namespace of that
source; a standard namespace never sees a private row because loom's own
statements carry `is_private = 0`, and the restricted class is refused there
before any read. Unset = every tracker read answers unavailable.

Each credential has one async clickhouse_connect client (`readonly = 2`,
`max_execution_time` = `LOOM_TRACKER_LIVE_TIMEOUT_S`, two connections) and its own
breaker (CLOSED / OPEN / HALF_OPEN, the Langfuse score breaker's shape), so a slow
or absent service costs a request one bounded read and then nothing for the
cooldown — the caller answers `tracker_unavailable`, not a 500.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from server.core.ch_errors import error_code
from server.core.errors import VectorCapacityError
from server.core.hosts import vet_host
from server.core.remote_ch import connect
from server.core.vectors.admission import native_settings, raise_native
from server.settings import settings
from server.tracker.domain import sources as _sources
from server.tracker.domain.source_circuit import SourceCircuit

log = logging.getLogger("loom.clients.qa_intel")

CREDENTIAL = _sources.DEFAULT_SOURCE
# The default source's credential; `credentials()` is the live list once
# LOOM_TRACKER_SOURCES_JSON adds sources.
CREDENTIALS = (CREDENTIAL,)


class QaIntelUnavailable(RuntimeError):
    """The breaker is open, the credential is unset, or the service is not configured."""


@dataclass
class _Slot:
    client: Any | None = None
    breaker: SourceCircuit = field(default_factory=SourceCircuit)
    # Held while the client is built: reads that arrive together on a cold
    # slot (a brief's fan-out) share one client instead of each building one
    # and all but the last leaking theirs.
    building: asyncio.Lock = field(default_factory=asyncio.Lock)


_slots: dict[str, _Slot] = {c: _Slot(breaker=SourceCircuit(name=c)) for c in CREDENTIALS}


def credentials() -> tuple[str, ...]:
    """Every configured source name — one credential and breaker each."""
    return tuple(sorted(_sources.endpoints()))


def configured() -> bool:
    return bool(_sources.endpoints())


def largest_budget_s() -> float:
    """The widest per-read budget a reader may ask for (the bisect diff scan,
    the text / graph scans, the point read), in seconds."""
    budgets_ms = (
        settings.tracker_live_bisect_budget_ms,
        settings.tracker_live_text_budget_ms,
        settings.tracker_live_budget_ms,
        # the per-test history read and the daily roll-up statements
        settings.tracker_live_checks_budget_ms,
        settings.tracker_test_daily_budget_ms,
    )
    return max(0.1, max(float(b) for b in budgets_ms) / 1000.0)


def socket_timeout_s(session_timeout_s: float) -> int:
    """The HTTP socket waits 5 s past every ceiling a read may carry, so a
    slow statement ends as the server's TIMEOUT_EXCEEDED (our budget, not
    counted on the breaker) and not as a socket error at the same instant
    (which reads as the source being down)."""
    return int(max(float(session_timeout_s), largest_budget_s())) + 5


def _slot(credential: str) -> _Slot:
    """The slot of a configured source, created on first use."""
    if credential not in _slots:
        if credential not in _sources.endpoints():
            raise QaIntelUnavailable(f"unknown tracker credential {credential!r}")
        _slots[credential] = _Slot(breaker=SourceCircuit(name=credential))
    return _slots[credential]


# --- breaker, module-level for the public credential (tests, probe) ---------


def circuit_allow(credential: str = CREDENTIAL) -> bool:
    return _slot(credential).breaker.allow()


def circuit_record(success: bool, credential: str = CREDENTIAL) -> None:
    _slot(credential).breaker.record(success)


def circuit_status(credential: str = CREDENTIAL) -> dict[str, Any]:
    return _slot(credential).breaker.status()


def reset_for_tests() -> None:
    _slots.clear()
    for c in CREDENTIALS:
        _slots[c] = _Slot(breaker=SourceCircuit(name=c))


# --- clients -----------------------------------------------------------------


def _user_password(credential: str) -> tuple[str, str]:
    return _sources.credentials_for(credential)


async def _pinned_address(credential: str, host: str) -> str:
    """For a self-service source, the address its client connects to: the host is
    resolved once, per client built, and every answer must pass the host
    gate, so a name that turns private after registration is refused instead of
    followed. "" for an operator-configured source (connected by name, as the
    operator wrote it) and when the deployment allows private hosts."""
    if _sources.source_origin(credential) != "registry":
        return ""
    why, address = await vet_host(
        host, allow_private=settings.tracker_self_service_allow_private_hosts
    )
    if why:
        raise QaIntelUnavailable(f"qa_intel[{credential}] host refused: {why}")
    return address


async def _get_client(credential: str) -> Any:
    slot = _slot(credential)
    if slot.client is not None:
        return slot.client
    async with slot.building:
        if slot.client is None:
            slot.client = await _build_client(credential)
    return slot.client


async def _build_client(credential: str) -> Any:
    if not _sources.credential_configured(credential):
        raise QaIntelUnavailable(f"tracker credential {credential!r} is not configured")
    user, password = _user_password(credential)
    ep = _sources.endpoint(credential)
    if ep is None:
        raise QaIntelUnavailable(f"unknown tracker credential {credential!r}")
    timeout_s = max(0.1, float(settings.tracker_live_timeout_s))
    return await connect(
        ep.host,
        address=await _pinned_address(credential, ep.host),
        port=int(ep.port),
        secure=bool(ep.secure),
        database=ep.database,
        user=user,
        password=password,
        client_name=f"loom-tracker-live-{credential}",
        send_receive_timeout=socket_timeout_s(timeout_s),
        query_settings={"max_execution_time": timeout_s},
    )


# A remote result cap (`max_result_rows` / `max_result_bytes` on the service
# behind a table function) is the shape of ONE statement, not the upstream
# failing: it never counts on the breaker, which would silence every tracker read
# for two minutes (the per-shape quota is `is_quota_error`).
_REMOTE_CAP_MARKERS = ("TOO_MANY_ROWS_OR_BYTES", "Limit for result exceeded")


def is_result_cap_error(exc: BaseException) -> bool:
    text = str(exc)
    return any(m in text for m in _REMOTE_CAP_MARKERS)


async def query_rows(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    credential: str = CREDENTIAL,
    max_execution_s: float | None = None,
    query_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rows as dicts through `credential`. Raises QaIntelUnavailable when the
    breaker is open or the credential is unconfigured; records the outcome
    on the breaker. `query_settings` are per-statement settings sent beside
    the ceiling (the client already runs every statement under `readonly = 2`,
    which `remoteSecure`-backed views such as `checks` need; readers that
    also pass it do so explicitly, and 2 still permits SELECT only)."""
    breaker = _slot(credential).breaker
    if not breaker.allow():
        raise QaIntelUnavailable(f"qa_intel[{credential}] breaker is OPEN")
    # admitted in HALF_OPEN = this read holds the one probe slot (no await
    # between allow() and here, so no other read can have taken it)
    is_probe = breaker.state == "HALF_OPEN"
    try:
        client = await _get_client(credential)
        # the server-side ceiling follows the read's budget: a bisect diff scan
        # is allowed its own budget where an interactive read keeps the
        # session's ceiling
        settings_for_query: dict[str, Any] = dict(query_settings or {})
        if max_execution_s:
            settings_for_query["max_execution_time"] = max(0.1, float(max_execution_s))
        native_settings(
            settings_for_query, timeout=max_execution_s or float(settings.tracker_live_timeout_s)
        )
        res = await client.query(sql, parameters=params or {}, settings=settings_for_query)
        rows = [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]
    except (QaIntelUnavailable, VectorCapacityError):
        raise
    except Exception as e:
        raise_native(e)
        # an upstream quota refusal (code 201) is the source rationing this
        # shape, not the service failing: the read is unavailable, the
        # breaker stays closed for every other shape (the
        # build-history views proxy a playground under a per-shape quota)
        if is_quota_error(e):
            # The remote's text reaches the caller as a reason: logged, not quoted.
            log.warning("qa_intel[%s] upstream quota: %s", credential, e)
            raise QaIntelUnavailable(f"qa_intel[{credential}] upstream quota exceeded") from e
        # a remote result cap is the shape of one statement; a
        # statement the server ended at the ceiling WE sent is our budget, not
        # its outage (under a brief's twelve-way fan-out three
        # such timeouts in a row opened the breaker three times and refused 235
        # reads, 11 ms point reads included): neither counts on the breaker
        if not is_result_cap_error(e) and not is_budget_timeout(e) and not is_shape_error(e):
            breaker.record(False)
        raise
    finally:
        # whatever ended the probe (an uncounted error, a cancellation), its
        # slot must not stay taken; a read admitted while CLOSED never held it
        if is_probe:
            breaker.release()
    breaker.record(True)
    return rows


def is_quota_error(e: BaseException) -> bool:
    text = str(e)
    return "QUOTA_EXCEEDED" in text or "Code: 201." in text


# Errors that describe the statement, not the source's health.
# A tracker that lacks one table answers code 60 to every read of it; counted
# on the shared breaker, three in a row would refuse every tracker read for
# 120 s, for every caller. Like a quota refusal or our own ceiling, such an
# error is raised to its caller and never counted: the source is up, this
# shape cannot run on it.
_SHAPE_ERROR_CODES: frozenset[int] = frozenset(
    {
        36,  # BAD_ARGUMENTS
        43,  # ILLEGAL_TYPE_OF_ARGUMENT
        46,  # UNKNOWN_FUNCTION
        47,  # UNKNOWN_IDENTIFIER
        53,  # TYPE_MISMATCH
        60,  # UNKNOWN_TABLE
        62,  # SYNTAX_ERROR
        81,  # UNKNOWN_DATABASE
        115,  # UNKNOWN_SETTING
    }
)
_SHAPE_ERROR_NAMES: tuple[str, ...] = (
    "UNKNOWN_TABLE",
    "UNKNOWN_IDENTIFIER",
    "UNKNOWN_FUNCTION",
    "UNKNOWN_DATABASE",
    "UNKNOWN_SETTING",
    "SYNTAX_ERROR",
    "TYPE_MISMATCH",
    "ILLEGAL_TYPE_OF_ARGUMENT",
)


def is_shape_error(e: BaseException) -> bool:
    """The upstream refused this statement for what it names (a missing
    table, column, function or setting; bad syntax or types)."""
    text = str(e)
    if any(n in text for n in _SHAPE_ERROR_NAMES):
        return True
    return error_code(text) in _SHAPE_ERROR_CODES


def is_budget_timeout(e: BaseException) -> bool:
    """The upstream ended the statement at `max_execution_time` (code 159).
    The server writes `Code: 159.`; clickhouse_connect prefixes an HTTP error
    with `code: 159,` from the header, which a mid-stream error lacks."""
    text = str(e)
    return "TIMEOUT_EXCEEDED" in text or re.search(r"\b[Cc]ode: 159\b", text) is not None


_TABLE_NAME = re.compile(r"^\s*[\w`\".]+\s*$")
# The address and the table of a `remote` / `remoteSecure` call: the table is
# one argument (`db.table`) or two (`db`, `table`), each quoted or bare.
_REMOTE_HEAD = re.compile(
    r"^\s*(remote(?:Secure)?)\s*\(\s*('[^'\\]*')\s*,\s*('?[\w.]+'?)\s*(?:,\s*('?\w+'?)\s*)?([,)])",
    re.IGNORECASE,
)
_CALL_NAME = re.compile(r"^\s*(\w+)\s*\(")


def redact_table_function(clause: str) -> str:
    """`clause` as operators and callers may read it, built only from parts
    that cannot hold a credential: a table name as is; a `remote` /
    `remoteSecure` call as its address and table, with every argument after
    the table (the user, the password, a sharding key) shown as `...` when
    there is one; any other clause as its function name alone, or `(...)`
    when it is no call."""
    if not clause or _TABLE_NAME.match(clause):
        return clause
    m = _REMOTE_HEAD.match(clause)
    if m:
        fn, address, first, second, end = m.groups()
        if "." in first:
            # a second word after `db.table` is the user, never shown
            more = ", ..." if second or end == "," else ""
            return f"{fn}({address}, {first}{more})"
        if second:
            more = ", ..." if end == "," else ""
            return f"{fn}({address}, {first}, {second}{more})"
    call = _CALL_NAME.match(clause)
    return f"{call.group(1)}(...)" if call else "(...)"


def _printable_endpoint(ep: Any) -> dict[str, Any]:
    """An endpoint's fields for operator output: its `checks_from` may be a
    `remoteSecure(...)` call holding the checks service's password."""
    out = dict(ep.__dict__)
    out["checks_from"] = redact_table_function(str(out.get("checks_from") or ""))
    return out


async def probe() -> dict[str, Any]:
    """Counts and the newest timestamp per upstream table, per configured
    credential — `loom tracker-probe`, first contact."""
    names = credentials()
    out: dict[str, Any] = {
        "configured": configured(),
        "credentials": {c: _sources.credential_configured(c) for c in names},
        "circuit": {c: circuit_status(c) for c in names},
        "sources": {c: _printable_endpoint(_sources.endpoints()[c]) for c in names},
    }
    if not configured():
        return out
    tables = ("github_issues", "github_prs", "edges", "component_risk_snapshot", "release_items")
    for credential in names:
        if not _sources.credential_configured(credential):
            continue
        db = _sources.endpoints()[credential].database
        per: dict[str, Any] = {}
        for table in tables:
            col = (
                "created_at"
                if table == "edges"
                else ("computed_at" if table.endswith("snapshot") else "updated_at")
            )
            try:
                rows = await query_rows(
                    f"SELECT count() AS n, max({col}) AS newest FROM {db}.{table}",
                    credential=credential,
                )
            except Exception as e:  # noqa: BLE001 — an optional table may be absent
                per[table] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
                continue
            per[table] = rows[0] if rows else {}
        out[credential] = per
    return out


async def forget(credential: str) -> None:
    """Drop a source's client so the next read reconnects with the current
    credential (rotation); the breaker is kept."""
    slot = _slots.get(credential)
    if slot is None or slot.client is None:
        return
    try:
        await slot.client.close()
    except Exception:  # noqa: BLE001 — a failed close still drops the client
        pass
    slot.client = None


async def close() -> None:
    for slot in _slots.values():
        if slot.client is not None:
            try:
                await slot.client.close()
            except Exception:  # noqa: BLE001 — shutdown closes every other client
                pass
            slot.client = None


__all__ = [
    "CREDENTIAL",
    "CREDENTIALS",
    "QaIntelUnavailable",
    "circuit_allow",
    "circuit_record",
    "circuit_status",
    "close",
    "configured",
    "credentials",
    "forget",
    "is_budget_timeout",
    "is_quota_error",
    "is_result_cap_error",
    "largest_budget_s",
    "probe",
    "query_rows",
    "redact_table_function",
    "reset_for_tests",
    "socket_timeout_s",
    "connect",
]
