"""A cell's own state, read from its Postgres, and the gate it puts on this process.

A server process under the Postgres authority reads the state rows (the three below
and whether `loom.tenancy` holds a row) whether `LOOM_CELL` is set or not, and is
gated until its first read lands. With `LOOM_CELL` unset the process serves as a
single deployment while the deployment is no cell (`open`) or a first cell still
`recording`, and is gated in any other state (`state`); it keeps its verdict through
failed reads. A process whose view is never read with `LOOM_CELL` unset (a CLI, the
workers child) and every process under the ClickHouse authority read as `open`; a CLI
command that declares an org judges the state on a session of its own instead
(`server.cli._ch.check_unset_cell`). With `LOOM_CELL` set, a process serves only while
three rows of its cell's Postgres are read and agree with its configuration
(docs/architecture/cells.md, "The cell's own row"):

- `loom.cell_first`: whether this cell is the deployment's first, and its name;
- `loom.tenancy_mode`: `recording` or `enforced`;
- `loom.cell_self`: the configuration fingerprints the directory accepted for the
  cell, `current` and, during a change, `next`.

Until all three are read, while one is absent, while the first row names another
cell, or while this process's fingerprint matches neither accepted value, the
process answers 503 to everything but its health probes (`gate_reason`), reports
itself unready and stands aside from the workers. A failed read keeps the last
rows for at most `jwt_revocation_max_stale_seconds`, as the signing keyring does;
past it the process is gated until a read lands.

A fingerprint is a set of named fields under a schema version. A process matches
a value when every field both know is equal, and a field the value lacks is equal
only at its default, so two builds with the same configuration match one row. A field
added keeps the schema version; a changed meaning raises it, and a build matches no
value of a version it does not know.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from server.core import readiness
from server.core.errors import caller_text
from server.settings import settings

log = logging.getLogger(__name__)

# A cell's name: compared exactly, unique in the directory, and the middle field
# of the cell's SCIM tokens, so it holds no `_`.
NAME = re.compile(r"[a-z0-9-]{1,32}")
# A credential's SHA-256 as the directory and the router's settings keep it.
SHA256 = re.compile(r"[0-9a-f]{64}")
# The largest placement quota: the directory keeps it in a bigint.
PLACEMENTS_MAX = 2**63 - 1  # not a setting: the bigint column bounds it
# The longest org name, in UTF-8 bytes, a cell records and the router places: below
# Postgres's btree entry limit (about 2.7 KB), so every org a cell records has a
# placement the directory can index.
ORG_MAX_BYTES = 2048  # not a setting: cells and the router judge one limit


def org_name_refusal(org: str) -> str:
    """Why a cell neither records nor places `org` ('' when its name fits)."""
    size = len(org.encode())
    if size <= ORG_MAX_BYTES:
        return ""
    return f"org name is {size} bytes, longer than the {ORG_MAX_BYTES} bytes an org name may have"


FINGERPRINT_VERSION = 1
# Every field and the value it has when a configuration does not set it. A value
# written by an older build that lacks a field matches only a process at this default.
DEFAULTS: dict[str, Any] = {
    "public_host": "",
    "oauth_issuer_host": "",
    "oidc_issuer": "",
    "oidc_client_id": "",
    "scim": False,
    "bearer_issuers": [],
    "configured_orgs": [],
    "end_user_orgs": [],
    "deployment_id": "",
    "bucket": "",
    "bucket_prefix": "",
}

MODES = ("recording", "enforced")

# Where a process with LOOM_CELL unset is told to look when its deployment's state gates it.
TROUBLESHOOTING = "docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated"


def state(*, first: tuple[bool, str] | None, mode: str, has_own: bool, has_tenancy: bool) -> str:
    """The deployment's cell state from its four state rows: `open` (none), `recording`
    (a first cell's full set, still recording), `enforced` (any other full set, a later
    cell whose mode row reads `recording` included) or `partial` (any other mix, which
    every gate treats as `enforced`). Prepare writes the full set in one transaction, so
    only a hand edit or a partial restore makes `partial`."""
    if first is None and not mode and not has_tenancy and not has_own:
        return "open"
    if first is None or mode not in MODES or not has_own:
        return "partial"
    if first[0] and mode == "recording":
        return "recording"
    return "enforced"


def normalise_host(value: str) -> str:
    """The one spelling of a host the directory, routing and this check compare:
    lowercase (an IPv6 zone keeps its case), IDNA as ASCII, no trailing dot, no default
    port, IPv6 in brackets.
    Takes a URL or a bare host[:port]."""
    raw = value.strip()
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        host = (parts.hostname or "").rstrip(".")
    except ValueError:  # an unbalanced or invalid IPv6 bracket: no host
        return ""
    if not host:
        return ""
    # `hostname` is lowercase already (an IPv6 zone keeps its case), and IDNA keeps it so.
    if ":" in host:  # an IPv6 literal: urlsplit drops its brackets
        host = f"[{host}]"
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:  # an empty or over-long label: no host
            return ""
    try:
        port = parts.port
    except ValueError:  # out of range or not a number: no host
        return ""
    default = {"https": 443, "http": 80}.get(parts.scheme or "https")
    return host if port in (None, default) else f"{host}:{port}"


# The orgs the operator's code entries name (`repos.operator_orgs`), which attach places
# since the code workers write them. Handed in at boot (`boot.require_storage_authority`,
# `boot.start_cell_state`) and by a CLI (`server.cli._ch.check_orgs`): `server.core` may
# not import the code plane. None counts none.
_code_orgs: Callable[[], set[str]] | None = None


def set_code_orgs(reader: Callable[[], set[str]]) -> None:
    global _code_orgs
    _code_orgs = reader


def _code_repo_orgs() -> set[str]:
    return _code_orgs() if _code_orgs is not None else set()


def fingerprint(deployment_id: str) -> dict[str, Any]:
    """This process's fingerprint: what the cell accepts, which the directory checks
    against every other cell, and the stores it serves from."""
    public = settings.public_base_url
    end_users = settings.end_user_issuers()
    bucket = settings.document_s3_bucket
    configured = {
        o
        for o in (
            settings.oidc_jit_org,
            settings.self_signup_org if settings.self_signup == "open" else "",
            settings.code_org,
            settings.tracker_org,
            *_code_repo_orgs(),
        )
        if o
    }
    return {
        "public_host": normalise_host(public),
        "oauth_issuer_host": normalise_host(settings.oauth_issuer or public),
        "oidc_issuer": settings.oidc_issuer.rstrip("/"),
        "oidc_client_id": settings.oidc_client_id,
        "scim": bool(settings.scim_token.strip()),
        "bearer_issuers": sorted(settings.trusted_issuers() | set(end_users)),
        "configured_orgs": sorted(configured),
        "end_user_orgs": sorted({o for spec in end_users.values() for o in spec.orgs}),
        "deployment_id": deployment_id,
        "bucket": bucket,
        # Without a bucket the prefix stores nothing and takes part in no check.
        "bucket_prefix": settings.document_s3_prefix if bucket else "",
    }


def as_value(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The stored form of a fingerprint: its fields under the schema version."""
    return {"v": FINGERPRINT_VERSION, "fields": dict(fields)}


def matches(mine: Mapping[str, Any], value: Mapping[str, Any] | None) -> bool:
    """Whether this process's fields match an accepted value: each field both know is
    equal, and a field the value lacks is at its default here. Fields only the value
    knows (a newer build's) are not this build's to judge."""
    if not isinstance(value, Mapping) or not value:
        return False
    # A later version changed what a field means: this build cannot judge it. A field
    # added keeps the version (the default rule covers it).
    if type(value.get("v")) is not int or value["v"] != FINGERPRINT_VERSION:
        return False
    theirs = value.get("fields")
    if not isinstance(theirs, Mapping):
        return False
    for name, mine_value in mine.items():
        expected = theirs[name] if name in theirs else DEFAULTS.get(name)
        if mine_value != expected:
            return False
    return True


def digest(fields: Mapping[str, Any]) -> str:
    """A short digest of a fingerprint for the gate's reason; never compared."""
    blob = json.dumps(dict(fields), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class _View:
    read_at: float | None = None
    first: tuple[bool, str] | None = None  # (is_first, cell) once read and present
    mode: str = ""
    current: dict[str, Any] | None = None
    next: dict[str, Any] | None = None
    has_tenancy: bool = False
    error: str = ""

    def state(self) -> str:
        """The cell state these rows make; a view never read is `open`."""
        return state(
            first=self.first,
            mode=self.mode,
            has_own=self.current is not None,
            has_tenancy=self.has_tenancy,
        )


_view = _View()
_deployment_id = ""
# Set by the server process that reads the cell state (`boot.start_cell_gate`): until its
# first read lands it is gated. Unset in a process that reads nothing (a CLI, the
# workers child with LOOM_CELL unset), whose unread view is `open`.
_awaiting_first_read = False


def expect_first_read() -> None:
    global _awaiting_first_read
    _awaiting_first_read = True


def set_deployment_id(deployment_id: str) -> None:
    """The deployment id this process verified at boot: its Postgres and ClickHouse
    both record it (`pg_profile.require_authority`)."""
    global _deployment_id
    _deployment_id = deployment_id


def deployment_id() -> str:
    return _deployment_id


def mine() -> dict[str, Any]:
    return fingerprint(_deployment_id)


def mode() -> str:
    """The tenancy mode last read ('' before the first read or with `LOOM_CELL` unset).
    Only the first cell ever records: on a later cell any mode but `enforced` reads as
    `enforced` (docs/architecture/cells.md, "The `first` row")."""
    if not settings.cell:
        return ""
    if _view.first is not None and not _view.first[0] and _view.mode:
        return "enforced"
    return _view.mode


@dataclasses.dataclass(frozen=True)
class _Verdict:
    """The last verdict and what it judged: every request asks, and the fingerprint
    (settings parsed into fields) changes only with the view and the store id."""

    view: _View
    deployment_id: str
    cell: str
    reason: str


_memo: _Verdict | None = None


def gate_reason() -> str:
    """Why this process must serve nothing now ('' when it may serve)."""
    global _memo
    if not settings.cell:
        # No stale bound: a single deployment keeps serving through a Postgres outage,
        # and a gated one stays gated (docs/architecture/cells.md, "Invariants").
        return _unset_reason(_view)
    view, memo = _view, _memo
    # Checked on every call, before the verdict kept for this read: time moves on. A
    # failed read trusts the last rows as long as a revocation snapshot is trusted.
    max_age = float(settings.jwt_revocation_max_stale_seconds)
    if view.read_at is not None and (age := time.monotonic() - view.read_at) > max_age:
        return (
            f"the cell's rows were last read {age:.0f} s ago, more than "
            "LOOM_JWT_REVOCATION_MAX_STALE_SECONDS" + (f" ({view.error})" if view.error else "")
        )
    if (
        memo is not None
        and memo.view is view
        and (memo.deployment_id, memo.cell) == (_deployment_id, settings.cell)
    ):
        return memo.reason
    reason = _judge(view)
    _memo = _Verdict(view, _deployment_id, settings.cell, reason)
    return reason


def read_error() -> str:
    """Why the last read failed, as a caller may see it ('' after a read that landed)."""
    return _view.error


def gated_by_state() -> bool:
    """Whether this deployment's server processes read the state rows and are gated on
    them: under the Postgres authority, `LOOM_CELL` set or not (a cell needs that
    authority, `settings_problem`). Under the ClickHouse authority nothing is read."""
    from server.core import pg

    return pg.enabled()


def _unset_reason(view: _View) -> str:
    """The gate on a process with `LOOM_CELL` unset: open while the deployment is no
    cell or a first cell still recording."""
    if _awaiting_first_read and view.read_at is None:
        # A failed read is never taken for an absent row.
        return (
            "the deployment's cell state is not read yet"
            + (f": {view.error}" if view.error else "")
            + f" (see {TROUBLESHOOTING})"
        )
    found = view.state()
    if found in ("open", "recording"):
        return ""
    if found == "enforced":
        return (
            "this deployment is a cell in enforced mode and LOOM_CELL is unset "
            f"(see {TROUBLESHOOTING})"
        )
    present = [
        name
        for name, here in (
            ("loom.cell_first", view.first is not None),
            ("loom.tenancy_mode", bool(view.mode)),
            ("loom.cell_self", view.current is not None),
            ("loom.tenancy", view.has_tenancy),
        )
        if here
    ]
    return (
        f"this deployment holds a partial cell state ({', '.join(present)} present; "
        f"see {TROUBLESHOOTING})"
    )


# The fingerprint fields a CLI's gate judges: the stores it would write. A CLI verifies
# no token and its fingerprint never reaches the router, so the rest of the servers'
# fingerprint is not its to match (docs/architecture/cells.md, "The CLI declares its
# orgs").
_STORE_FIELDS = ("deployment_id", "bucket", "bucket_prefix")


def cli_gate_reason() -> str:
    """Why a CLI with `LOOM_CELL` set must refuse an org-declaring command ('' when it
    may), on the rows it has just read: the first row naming `LOOM_CELL`, and its stores
    matching a value of the cell's own row."""
    if reason := _rows_reason(_view):
        return reason
    fields = mine()
    stores = {name: fields[name] for name in _STORE_FIELDS}
    if matches(stores, _view.current) or matches(stores, _view.next):
        return ""
    return (
        f"this CLI's stores ({', '.join(stores)}) match neither value of its cell's own "
        "row: point it at the cell's Postgres, ClickHouse and document bucket"
    )


def _rows_reason(view: _View) -> str:
    """Why the rows read do not make a cell this process can belong to ('' when they do)."""
    if view.read_at is None:
        return "the cell's rows are not read yet" + (f" ({view.error})" if view.error else "")
    if view.first is None:
        return "this cell's first row is absent: run loom cell prepare"
    if view.first[1] != settings.cell:
        return f"the first row names cell {view.first[1]!r}, not LOOM_CELL={settings.cell!r}"
    if view.mode not in MODES:
        return "this cell's tenancy mode row is absent: run loom cell prepare"
    if view.current is None:
        return "this cell's own row is absent: run loom cell prepare"
    return ""


def _judge(view: _View) -> str:
    if reason := _rows_reason(view):
        return reason
    fields = mine()
    if not (matches(fields, view.current) or matches(fields, view.next)):
        return (
            "this process's configuration matches neither value of its cell's own row "
            f"(fingerprint {digest(fields)}); see loom doctor"
        )
    return ""


def readiness_check() -> readiness.CheckResult:
    reason = gate_reason()
    return readiness.result(not reason, reason)


_READ = (
    "SELECT (SELECT row_to_json(f) FROM (SELECT is_first, cell FROM loom.cell_first) f) AS first, "
    "(SELECT mode FROM loom.tenancy_mode) AS mode, "
    "(SELECT row_to_json(s) FROM (SELECT current_fp, next_fp FROM loom.cell_self) s) AS own, "
    "EXISTS (SELECT 1 FROM loom.tenancy) AS tenancy"
)


# The state rows as plain columns, for `ensure`, `loom doctor` and an org-declaring CLI with
# `LOOM_CELL` unset (whose role holds column grants only): the state verdict needs no
# fingerprint, so a `loom.cell_self` value that does not decode fails neither a boot's
# trigger pass nor a doctor run.
_STATE = (
    "SELECT (SELECT is_first FROM loom.cell_first) AS is_first, "
    "(SELECT cell FROM loom.cell_first) AS cell, "
    "(SELECT mode FROM loom.tenancy_mode) AS mode, "
    "EXISTS (SELECT 1 FROM loom.cell_self) AS has_own, "
    "EXISTS (SELECT 1 FROM loom.tenancy) AS has_tenancy"
)


async def read_state(conn: Any) -> str:
    """The cell state on `conn` (`state`); raises when it cannot read."""
    return (await _read_named_state(conn))[0]


async def _read_named_state(conn: Any) -> tuple[str, str]:
    """The cell state on `conn` and the cell its first row names ('' without one)."""
    row = await conn.fetchrow(_STATE)
    first = None if row["is_first"] is None else (bool(row["is_first"]), str(row["cell"]))
    found = state(
        first=first,
        mode=str(row["mode"] or ""),
        has_own=bool(row["has_own"]),
        has_tenancy=bool(row["has_tenancy"]),
    )
    return found, first[1] if first else ""


_STATE_TABLES = ("loom.cell_first", "loom.tenancy_mode", "loom.cell_self", "loom.tenancy")


async def read_state_if_present(conn: Any) -> tuple[str, str]:
    """`_read_named_state`, with every state table absent read as `open`: a schema that
    predates them cannot be a cell. Judged by `to_regclass`, never by matching a
    privilege error, so a role missing a grant still raises; some tables absent is
    `partial`."""
    present = await conn.fetchval(
        "SELECT count(to_regclass(t)) FROM unnest($1::text[]) AS t", list(_STATE_TABLES)
    )
    if present == 0:
        return "open", ""
    if present < len(_STATE_TABLES):
        return "partial", ""
    return await _read_named_state(conn)


async def refresh() -> bool:
    """Read the state rows in one statement. A failed read, or rows that do not
    decode, keeps the last view and is retried on schedule."""
    global _view
    from server.core import pg

    try:
        _view = _decoded(await pg.fetchrow(_READ))
    except Exception as e:  # noqa: BLE001 — a failed read keeps the last rows; the gate holds
        # The reason reaches callers in a 503: only what a caller may see.
        _view = dataclasses.replace(_view, error=caller_text(e))
        log.warning("cell state: read failed, keeping the last rows: %s", e)
        return False
    return True


class MalformedRow(ValueError):
    """A cell row whose value is not the shape this build reads."""


def _decoded(row: Any) -> _View:
    first = json_object(row["first"]) if row else None
    own = json_object(row["own"]) if row else None
    return _View(
        read_at=time.monotonic(),
        first=(bool(first["is_first"]), str(first["cell"])) if first else None,
        mode=str(row["mode"] or "") if row else "",
        current=json_object(own.get("current_fp")) if own else None,
        next=json_object(own.get("next_fp")) if own else None,
        has_tenancy=bool(row["tenancy"]) if row else False,
    )


def json_object(value: Any) -> dict[str, Any] | None:
    """A JSON object, as the pool's codec decodes it; None for SQL NULL."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MalformedRow(f"expected a JSON object, read {type(value).__name__}")
    return value


def start_refresh_loop() -> asyncio.Task[None]:
    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("cell.refresh")
        while True:
            await asyncio.sleep(
                float(settings.tenancy_mode_cache_s) if _view.read_at is not None else 1.0
            )
            await refresh()

    return asyncio.get_running_loop().create_task(_run(), name="loom-cell-refresh")


def settings_problem() -> str:
    """Why this process must not start with its cell settings ('' when it may). Run in
    the lifespan."""
    if settings.cell_probe_port and settings.storage_authority != "postgres":
        return "LOOM_CELL_PROBE_PORT needs LOOM_STORAGE_AUTHORITY=postgres"
    if settings.cell_probe_port and not settings.auth_actually_required():
        # With auth off the main port answers only its own hosts (DNS rebinding), so the
        # probe route would not answer attach's host check; a cell needs auth anyway.
        return "LOOM_CELL_PROBE_PORT needs authentication on (a cell needs it)"
    name = settings.cell
    if not name:
        return ""
    if not NAME.fullmatch(name):
        return f"LOOM_CELL={name!r} is not 1 to 32 of a-z, 0-9 and '-'"
    if settings.storage_authority != "postgres":
        return "LOOM_CELL needs LOOM_STORAGE_AUTHORITY=postgres"
    if not settings.auth_actually_required() or settings.mcp_auth_mode == "open":
        return "LOOM_CELL needs authentication on every transport (LOOM_MCP_AUTH_MODE not open)"
    if not normalise_host(settings.public_base_url):
        return (
            "LOOM_CELL needs LOOM_PUBLIC_BASE_URL with a valid host: no URL a cell hands "
            "out comes from a Host"
        )
    if settings.oauth_issuer and not normalise_host(settings.oauth_issuer):
        return "LOOM_OAUTH_ISSUER has no valid host"
    return ""


def require_settings() -> None:
    if problem := settings_problem():
        raise RuntimeError(f"refusing to start: {problem}")
