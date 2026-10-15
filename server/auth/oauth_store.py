"""Where OAuth state lives: registered clients, codes, refresh tokens, device
grants and in-flight IdP sign-ins, in `loom.oauth_state` (ClickHouse).

Codes, refresh tokens and device codes are bearer secrets, so rows are keyed by
`token_id(token)` (sha256 hex), never the token: a row read out of the table
(a backup, a SELECT grant) is not a working credential. Device user codes are
keyed with the server secret by device.py; the identity provider's refresh
token has to be sent back to it, so it is sealed (secrets_at_rest). Expiry is
a Unix epoch `UInt32`, which round-trips without the naive-DateTime timezone
shift.

Every state change is a new ReplacingMergeTree version read back with FINAL.
A change that depends on the current state (spending a credential, a step of
a device grant) runs inside `transition(key)`, which claims the grant in
Keeper (claims.py) so two requests cannot both pass the same check.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal, Protocol, TypedDict

from server.auth import claims
from server.core.secrets_at_rest import seal, unseal

_T = "loom.oauth_state"
UPSTREAM_PURPOSE = "oidc refresh token"
PENDING_TTL = 600  # an in-flight IdP sign-in: time to finish it

DeviceStatus = Literal["pending", "approved", "denied"]


class Grant(TypedDict):
    """A code or refresh token: who it signs in, for which client, in which
    session `family`. A refresh token has no redirect_uri or code_challenge."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    subject: str
    org: str
    namespaces: list[str]
    roles: list[str]
    exp: int
    family: str
    upstream_refresh: str


class Device(TypedDict):
    """A device grant (device.py). `subject`… are empty until someone signs in
    on it; `ticket` is the sha256 of its one-time approval ticket."""

    id: str
    client_id: str
    subject: str
    org: str
    namespaces: list[str]
    roles: list[str]
    exp: int
    family: str
    upstream_refresh: str
    user_code: str
    status: DeviceStatus
    address: str
    ticket: str


class CodeReplayed(Exception):
    """A single-use code, refresh token or device code was presented after
    it was spent."""

    def __init__(self, grant: Grant | Device) -> None:
        super().__init__("spent credential presented again")
        self.grant = grant


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OAuthStore(Protocol):
    """What oauth.py and device.py need from the store (tests use an
    in-memory one). take_* spend a credential: None when it is unknown,
    expired or another client's (left unspent), CodeReplayed when spent."""

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None: ...
    async def get_client(self, client_id: str) -> dict[str, Any] | None: ...
    async def save_code(self, code: str, g: Grant) -> None: ...
    async def take_code(
        self, code: str, client_id: str, accept: Callable[[Grant], bool]
    ) -> Grant | None: ...
    async def save_refresh(self, token: str, g: Grant) -> None: ...
    async def take_refresh(self, token: str, client_id: str) -> Grant | None: ...
    async def save_pending(self, state: str, data: dict[str, Any]) -> None: ...
    async def take_pending(self, state: str) -> dict[str, Any] | None: ...
    async def save_device(self, d: Device) -> None: ...
    async def device(self, device_id: str) -> Device | None: ...
    async def device_by_user_code(self, user_code_id: str) -> Device | None: ...
    async def take_device(self, device_code: str) -> Device | None: ...
    async def owe_revocation(self, family: str, reason: str) -> None: ...
    async def revocation_owed(self, family: str) -> bool: ...
    def transition(self, key: str) -> AbstractAsyncContextManager[None]: ...


def _grant(row: dict[str, Any]) -> Grant:
    return {
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "code_challenge": row["code_challenge"],
        "subject": row["subject"],
        "org": row["org"],
        "namespaces": list(row["namespaces"]),
        "roles": list(row["roles"]),
        "exp": int(row["expires_at"]),
        "family": row["family"],
        "upstream_refresh": unseal(UPSTREAM_PURPOSE, row["upstream_refresh"]),
    }


def _grant_row(rtype: str, rid: str, g: Grant, consumed: int) -> dict[str, Any]:
    return {
        "record_type": rtype,
        "id": rid,
        "client_id": g["client_id"],
        "redirect_uri": g["redirect_uri"],
        "code_challenge": g["code_challenge"],
        "subject": g["subject"],
        "org": g["org"],
        "namespaces": list(g["namespaces"]),
        "roles": list(g["roles"]),
        "expires_at": int(g["exp"]),
        "family": g["family"],
        "upstream_refresh": seal(UPSTREAM_PURPOSE, g["upstream_refresh"]),
        "consumed": consumed,
        "version": time.time_ns(),
    }


def _device(row: dict[str, Any]) -> Device:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "subject": row["subject"],
        "org": row["org"],
        "namespaces": list(row["namespaces"]),
        "roles": list(row["roles"]),
        "exp": int(row["expires_at"]),
        "family": row["family"],
        "upstream_refresh": unseal(UPSTREAM_PURPOSE, row["upstream_refresh"]),
        "user_code": row["user_code"],
        "status": row["status"],
        "address": row["address"],
        "ticket": row["ticket"],
    }


def _device_row(d: Device, consumed: int) -> dict[str, Any]:
    return {
        "record_type": "device",
        "id": d["id"],
        "client_id": d["client_id"],
        "subject": d["subject"],
        "org": d["org"],
        "namespaces": list(d["namespaces"]),
        "roles": list(d["roles"]),
        "expires_at": int(d["exp"]),
        "family": d["family"],
        "upstream_refresh": seal(UPSTREAM_PURPOSE, d["upstream_refresh"]),
        "user_code": d["user_code"],
        "status": d["status"],
        "address": d["address"],
        "ticket": d["ticket"],
        "consumed": consumed,
        "version": time.time_ns(),
    }


_GRANT_COLUMNS = (
    "client_id, redirect_uri, code_challenge, subject, org, namespaces, roles, "
    "expires_at, consumed, family, upstream_refresh"
)
_DEVICE_COLUMNS = (
    "id, client_id, subject, org, namespaces, roles, expires_at, family, upstream_refresh, "
    "user_code, status, address, ticket, consumed"
)


class ClickHouseStore:
    """OAuth state in loom.oauth_state: durable across restarts and replicas."""

    def _ch(self) -> Any:
        from server.core.ch import get_client

        return get_client()

    @asynccontextmanager
    async def transition(self, key: str) -> AsyncIterator[None]:
        async with claims.holding(self._ch(), key):
            yield

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None:
        await self._ch().insert(
            _T,
            [
                {
                    "record_type": "client",
                    "id": client_id,
                    "redirect_uris": list(redirect_uris),
                    "client_name": name,
                    "version": time.time_ns(),
                }
            ],
        )

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        row = await self._ch().query_one(
            f"SELECT redirect_uris, client_name FROM {_T} FINAL "
            "WHERE record_type = 'client' AND id = {id:String} AND consumed = 0 LIMIT 1",
            params={"id": client_id},
        )
        if not row:
            return None
        return {"redirect_uris": list(row["redirect_uris"]), "client_name": row["client_name"]}

    async def save_code(self, code: str, g: Grant) -> None:
        await self._ch().insert(_T, [_grant_row("code", token_id(code), g, 0)])

    async def save_refresh(self, token: str, g: Grant) -> None:
        await self._ch().insert(_T, [_grant_row("refresh", token_id(token), g, 0)])

    async def take_code(
        self, code: str, client_id: str, accept: Callable[[Grant], bool]
    ) -> Grant | None:
        """`accept` (the redirect URI and PKCE verifier match) is checked before
        the spend, like the client: a code presented wrongly is left unspent."""
        return await self._take("code", token_id(code), client_id, accept)

    async def take_refresh(self, token: str, client_id: str) -> Grant | None:
        return await self._take("refresh", token_id(token), client_id)

    async def _take(
        self,
        rtype: str,
        rid: str,
        client_id: str,
        accept: Callable[[Grant], bool] = lambda g: True,
    ) -> Grant | None:
        async with self.transition(f"{rtype}:{rid}"):
            row = await self._ch().query_one(
                f"""SELECT {_GRANT_COLUMNS} FROM {_T} FINAL
                    WHERE record_type = {{t:String}} AND id = {{id:String}} LIMIT 1""",
                params={"t": rtype, "id": rid},
            )
            if not row or int(row["expires_at"] or 0) < int(time.time()):
                return None
            grant = _grant(row)
            if row["client_id"] != client_id or not accept(grant):
                return None  # checked before the spend: a wrong presenter can't burn it
            if row["consumed"]:
                raise CodeReplayed(grant)
            await self._ch().insert(_T, [_grant_row(rtype, rid, grant, 1)])
            return grant

    async def save_pending(self, state: str, data: dict[str, Any]) -> None:
        # The JSON rides the client_name column, unused for non-client rows.
        await self._ch().insert(
            _T,
            [
                {
                    "record_type": "oidc_pending",
                    "id": state,
                    "client_name": json.dumps(data),
                    "expires_at": int(time.time()) + PENDING_TTL,
                    "version": time.time_ns(),
                }
            ],
        )

    async def take_pending(self, state: str) -> dict[str, Any] | None:
        async with self.transition(f"pending:{state}"):
            row = await self._ch().query_one(
                f"SELECT client_name, expires_at FROM {_T} FINAL WHERE "
                "record_type = 'oidc_pending' AND id = {id:String} AND consumed = 0 LIMIT 1",
                params={"id": state},
            )
            if not row or int(row["expires_at"] or 0) < int(time.time()):
                return None
            await self._ch().insert(
                _T,
                [
                    {
                        "record_type": "oidc_pending",
                        "id": state,
                        "client_name": row["client_name"],
                        "expires_at": int(row["expires_at"]),
                        "consumed": 1,
                        "version": time.time_ns(),
                    }
                ],
            )
        data: dict[str, Any] = json.loads(row["client_name"])
        return data

    async def owe_revocation(self, family: str, reason: str) -> None:
        """Record that the session must be revoked: the revocation itself did
        not land. A refresh in the family is refused while this stands."""
        await self._ch().insert(
            _T,
            [
                {
                    "record_type": "revocation_owed",
                    "id": family,
                    "client_name": reason,
                    "expires_at": int(time.time()) + 30 * 86400,
                    "version": time.time_ns(),
                }
            ],
        )

    async def revocation_owed(self, family: str) -> bool:
        row = await self._ch().query_one(
            f"SELECT 1 AS owed FROM {_T} FINAL "
            "WHERE record_type = 'revocation_owed' AND id = {id:String} LIMIT 1",
            params={"id": family},
        )
        return row is not None

    async def save_device(self, d: Device) -> None:
        await self._ch().insert(_T, [_device_row(d, 0)])

    async def device(self, device_id: str) -> Device | None:
        row = await self._ch().query_one(
            f"""SELECT {_DEVICE_COLUMNS} FROM {_T} FINAL
                WHERE record_type = 'device' AND id = {{id:String}} AND consumed = 0 LIMIT 1""",
            params={"id": device_id},
        )
        return _device(row) if row else None

    async def device_by_user_code(self, user_code_id: str) -> Device | None:
        """The live, undecided device grant whose user code keys to this."""
        row = await self._ch().query_one(
            f"""SELECT {_DEVICE_COLUMNS} FROM {_T} FINAL
                WHERE record_type = 'device' AND user_code = {{u:String}}
                  AND consumed = 0 AND status = 'pending'
                  AND expires_at >= toUInt32(now())
                LIMIT 1""",
            params={"u": user_code_id},
        )
        return _device(row) if row else None

    async def take_device(self, device_code: str) -> Device | None:
        """The device grant as the polling client sees it. An approved or
        denied grant is spent by this read; a pending one is not."""
        rid = token_id(device_code)
        undecided = await self.device(rid)
        if undecided is not None and undecided["status"] == "pending":
            return undecided  # a poll that writes nothing needs no claim
        async with self.transition(f"device:{rid}"):
            row = await self._ch().query_one(
                f"""SELECT {_DEVICE_COLUMNS} FROM {_T} FINAL
                    WHERE record_type = 'device' AND id = {{id:String}} LIMIT 1""",
                params={"id": rid},
            )
            if not row:
                return None
            d = _device(row)
            if d["exp"] < int(time.time()):
                return d  # expired: the poller hears so, spent or not
            if row["consumed"]:
                raise CodeReplayed(d)
            if d["status"] != "pending":
                await self._ch().insert(_T, [_device_row(d, 1)])
            return d
