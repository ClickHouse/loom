"""Self-service tracker sources: `loom.qa_sources` as the second
input of the tracker source map, beside the operator's `LOOM_TRACKER_SOURCES_JSON`.

A row is the same entry the operator would have written — endpoint,
database, read-only credential, the namespaces reading it — added by a
signed-in person of the org through `tracker.source.add`. This module refreshes
the in-process snapshot (`refresh_db_sources`, TTL
`tracker_sources_refresh_seconds`, forced after a write), the way the code plane's
`repo_registry` does for `loom.code_repos`; `server/tracker/domain/sources.py`
holds it (`registered`) and merges it UNDER the operator's JSON (a JSON entry wins by name).

The password is stored encrypted at rest (Fernet, key derived from the
deployment's JWT secret — the code plane's shape) and never leaves this
process except toward the source itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime
from typing import Any

from server.core import readiness
from server.core.datetimes import naive_utc_now_seconds as _now
from server.core.secrets_at_rest import seal, unseal
from server.settings import settings
from server.tracker.domain import sources
from server.tracker.domain.sources import DbSource

logger = logging.getLogger("loom.tracker.registry_sources")

TABLE = "loom.qa_sources"


# --- the password at rest ------------------------------------------------------


_PASSWORD_PURPOSE = "qa_sources.password"


def encrypt_password(plain: str) -> str:
    return seal(_PASSWORD_PURPOSE, plain)


def decrypt_password(enc: str) -> str:
    return unseal(_PASSWORD_PURPOSE, enc)


# --- snapshot ------------------------------------------------------------------

_LOADED_AT = 0.0


def _namespaces_of(raw: Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:  # noqa: BLE001 — an unparseable stored map links no namespace
        return {}
    out: dict[str, dict[str, Any]] = {}
    for ns, spec in (data or {}).items():
        if not isinstance(spec, dict) or not str(ns).strip():
            continue
        out[str(ns)] = {
            "repos": [str(r) for r in (spec.get("repos") or []) if str(r).strip()],
            "restricted": bool(spec.get("restricted", False)),
            "join_policy": str(spec.get("join_policy") or ""),
        }
    return out


_COLUMNS = (
    "org, name, host, port, secure, database, user, password_enc, namespaces_json, created_by, "
    "mapping_json, created_at"
)


async def _read_rows(client: Any) -> list[dict[str, Any]]:
    """The active rows."""
    rows: list[dict[str, Any]] = await client.query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL WHERE active ORDER BY org, name"
    )
    return rows


def row_to_source(r: dict[str, Any]) -> DbSource:
    return DbSource(
        org=str(r.get("org") or ""),
        name=str(r.get("name") or ""),
        host=str(r.get("host") or ""),
        port=int(r.get("port") or 8443),
        secure=bool(r.get("secure", True)),
        database=str(r.get("database") or ""),
        user=str(r.get("user") or ""),
        password=decrypt_password(str(r.get("password_enc") or "")),
        namespaces=_namespaces_of(r.get("namespaces_json")),
        created_by=str(r.get("created_by") or ""),
        mapping_json=str(r.get("mapping_json") or ""),
        created_at=r.get("created_at") if isinstance(r.get("created_at"), datetime) else None,
    )


async def refresh_db_sources(*, force: bool = False, ch: Any | None = None) -> list[DbSource]:
    """Re-read `loom.qa_sources` when the snapshot is older than
    `tracker_sources_refresh_seconds` (or `force`). Never raises: a directory that
    cannot be read keeps the last snapshot."""
    global _LOADED_AT
    if not bool(settings.tracker_self_service_enabled):
        return []
    now = time.monotonic()
    ttl = float(settings.tracker_sources_refresh_seconds)
    if not force and _LOADED_AT and now - _LOADED_AT < ttl:
        return sources.registered()
    try:
        from server.core.ch import get_client, sequential_reads

        client = ch or get_client()
        # A forced refresh follows a write (add, rotate, retire)
        # or a miss right after one on another process; on Cloud a plain read
        # can land on a replica that has not seen that write yet
        with sequential_reads() if force else contextlib.nullcontext():
            rows = await _read_rows(client)
        sources.set_registered([row_to_source(r) for r in rows if r.get("name") and r.get("host")])
        _LOADED_AT = now
    except Exception as e:  # noqa: BLE001 — keep the last snapshot
        logger.warning(
            "tracker_sources: refresh failed, keeping %d entries: %s",
            len(sources.registered()),
            e,
        )
    return sources.registered()


def loaded() -> bool:
    """One read of `loom.qa_sources` has landed in this process (or self-service
    is off, so there is nothing to load). Readiness waits for it."""
    return bool(_LOADED_AT) or not bool(settings.tracker_self_service_enabled)


def readiness_check() -> readiness.CheckResult:
    return readiness.result(loaded(), "tracker registry not loaded")


def find(org: str, name: str) -> DbSource | None:
    return next((s for s in sources.registered() if s.org == org and s.name == name), None)


def reset_for_tests(rows: list[DbSource] | None = None) -> None:
    global _LOADED_AT
    sources.set_registered(list(rows or []))
    _LOADED_AT = time.monotonic() if rows else 0.0


# --- writes ----------------------------------------------------------------------


def _row(src: DbSource, *, active: bool, now: Any) -> dict[str, Any]:
    return {
        "org": src.org,
        "name": src.name,
        "host": src.host,
        "port": int(src.port),
        "secure": bool(src.secure),
        "database": src.database,
        "user": src.user,
        "password_enc": encrypt_password(src.password),
        "namespaces_json": json.dumps(src.namespaces, sort_keys=True),
        "created_by": src.created_by,
        "mapping_json": src.mapping_json,
        "active": active,
        "created_at": src.created_at or now,
        "updated_at": now,
        "version": time.time_ns(),
    }


async def insert_source(src: DbSource, *, active: bool = True, ch: Any | None = None) -> None:
    """Write a source (a new version row; the same (org, name) supersedes)."""
    from server.core.ch import get_client

    client = ch or get_client()
    now = _now()
    await client.insert(TABLE, [_row(src, active=active, now=now)])


async def deactivate_source(org: str, name: str, *, ch: Any | None = None) -> bool:
    """Retire a source: a new version of its row with active=false, every
    other column carried forward (ReplacingMergeTree). False when unknown."""
    src = find(org, name)
    if src is None:
        return False
    await insert_source(src, active=False, ch=ch)
    return True


async def active_count(org: str, *, ch: Any | None = None) -> int:
    from server.core.ch import get_client

    client = ch or get_client()
    row = await client.query_one(
        f"SELECT count() AS n FROM {TABLE} FINAL WHERE org = {{org:String}} AND active",
        params={"org": org},
    )
    return int((row or {}).get("n") or 0)


__all__ = [
    "TABLE",
    "active_count",
    "deactivate_source",
    "decrypt_password",
    "encrypt_password",
    "find",
    "insert_source",
    "refresh_db_sources",
    "reset_for_tests",
    "row_to_source",
]
