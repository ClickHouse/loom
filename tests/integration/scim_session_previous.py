"""The previous build's session serializers for rolling-upgrade tests."""

from __future__ import annotations

from typing import Any, cast

from server.auth import claims
from server.auth.oauth_store import UPSTREAM_PURPOSE, Device, Grant
from server.core.secrets_at_rest import seal, unseal

_CODE_FIELDS = (
    "client_id",
    "redirect_uri",
    "code_challenge",
    "subject",
    "org",
    "namespaces",
    "roles",
    "exp",
    "since",
    "family",
)


_DEVICE_FIELDS = (
    "id",
    "client_id",
    "subject",
    "org",
    "namespaces",
    "roles",
    "exp",
    "family",
    "user_code",
    "status",
    "address",
    "ticket",
)


def _record(g: Grant | Device, fields: tuple[str, ...], spent: bool) -> dict[str, Any]:
    """A grant as kept in Keeper: its IdP token sealed, and a nonce so no two
    versions are equal (claims.swap relies on it)."""
    record: dict[str, Any] = {f: g[f] for f in fields}  # type: ignore[literal-required]
    record["upstream_refresh"] = seal(UPSTREAM_PURPOSE, g["upstream_refresh"])
    return record | {"spent": spent, "nonce": claims.nonce()}


def _code(r: dict[str, Any]) -> Grant:
    g = {f: r[f] for f in _CODE_FIELDS}
    return cast(Grant, g | {"upstream_refresh": unseal(UPSTREAM_PURPOSE, r["upstream_refresh"])})


def _device(r: dict[str, Any]) -> Device:
    d = {f: r[f] for f in _DEVICE_FIELDS}
    return cast(Device, d | {"upstream_refresh": unseal(UPSTREAM_PURPOSE, r["upstream_refresh"])})


_REFRESH_TYPE = "session_refresh"


def _refresh_row(rid: str, g: Grant, version: int) -> dict[str, Any]:
    return {
        "record_type": _REFRESH_TYPE,
        "id": rid,
        "client_id": g["client_id"],
        "subject": g["subject"],
        "org": g["org"],
        "namespaces": list(g["namespaces"]),
        "roles": list(g["roles"]),
        "expires_at": int(g["exp"]),
        "family": g["family"],
        "generation": g.get("generation", 0),
        "upstream_refresh": seal(UPSTREAM_PURPOSE, g["upstream_refresh"]),
        "version": version,
    }
