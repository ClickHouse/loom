"""A cell's own state, read from its Postgres, and the gate it puts on this process.

With `LOOM_CELL` unset nothing here reads a row or gates anything. With it set, a
process serves only while three rows of its cell's Postgres are read and agree with
its configuration (docs/architecture/cells.md, "The cell's own row"):

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
# `boot.start_cell_state`): `server.core` may not import the code plane. None counts none.
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
    error: str = ""


_view = _View()
_deployment_id = ""


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
        return ""
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


def _judge(view: _View) -> str:
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
    "(SELECT row_to_json(s) FROM (SELECT current_fp, next_fp FROM loom.cell_self) s) AS own"
)


async def refresh() -> bool:
    """Read the three rows in one statement. A failed read, or rows that do not
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
    first = _object(row["first"]) if row else None
    own = _object(row["own"]) if row else None
    return _View(
        read_at=time.monotonic(),
        first=(bool(first["is_first"]), str(first["cell"])) if first else None,
        mode=str(row["mode"] or "") if row else "",
        current=_object(own.get("current_fp")) if own else None,
        next=_object(own.get("next_fp")) if own else None,
    )


def _object(value: Any) -> dict[str, Any] | None:
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
    """Why this process must not start as a cell ('' when it may). Run in the lifespan."""
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
