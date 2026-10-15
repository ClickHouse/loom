"""Where OAuth state lives.

What a sign-in spends or moves once is kept in Keeper (claims.py), changed only
by compare-and-set: a code, an identity provider round trip in flight
(`pending`), a device grant, and which refresh token is each session's current
one (`family:<id>`). Of two requests spending the same credential exactly one
wins. The rest is in `loom.oauth_state` (ClickHouse): registered clients,
refresh tokens' grants (written before the token is handed out, read with
sequential consistency) and owed revocations.

Codes, refresh tokens and device codes are bearer secrets, so they are keyed by
`token_id(token)` (sha256 hex), never the token: a key or row read out of
storage is not a working credential. Device user codes are keyed with the
server secret by device.py; the identity provider's refresh token has to be
sent back to it, so it is sealed (secrets_at_rest). Expiry is a Unix epoch.

A record read from Keeper carries `held`, the values this request read or
wrote by key, so its next write swaps from them: a put-back after a failure,
or the next step of a device grant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, Literal, NotRequired, Protocol, TypedDict, cast

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
    held: NotRequired[dict[str, str]]


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
    held: NotRequired[dict[str, str]]


class CodeReplayed(Exception):
    """A single-use code, refresh token or device code was presented after
    it was spent."""

    def __init__(self, grant: Grant | Device) -> None:
        super().__init__("spent credential presented again")
        self.grant = grant


def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OAuthStore(Protocol):
    """What oauth.py and device.py need from the store. take_* spend a
    credential: None when it is unknown, expired or another client's (left
    unspent), CodeReplayed when it was spent. A save that finds the record
    changed since this request read it raises claims.Changed."""

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
    async def use_once(self, key: str, expires_at: int) -> bool: ...
    async def save_device(self, d: Device) -> None: ...
    async def device(self, device_id: str) -> Device | None: ...
    async def device_by_user_code(self, user_code_id: str) -> Device | None: ...
    async def take_device(self, device_code: str) -> Device | None: ...
    async def owe_revocation(self, family: str, reason: str) -> None: ...
    async def session_ended(self, family: str) -> bool: ...


_GRANT_FIELDS = (
    "client_id",
    "redirect_uri",
    "code_challenge",
    "subject",
    "org",
    "namespaces",
    "roles",
    "exp",
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


def _grant(r: dict[str, Any]) -> Grant:
    g = {f: r[f] for f in _GRANT_FIELDS}
    return cast(Grant, g | {"upstream_refresh": unseal(UPSTREAM_PURPOSE, r["upstream_refresh"])})


def _device(r: dict[str, Any]) -> Device:
    d = {f: r[f] for f in _DEVICE_FIELDS}
    return cast(Device, d | {"upstream_refresh": unseal(UPSTREAM_PURPOSE, r["upstream_refresh"])})


def _refresh_row(rid: str, g: Grant) -> dict[str, Any]:
    return {
        "record_type": "refresh",
        "id": rid,
        "client_id": g["client_id"],
        "subject": g["subject"],
        "org": g["org"],
        "namespaces": list(g["namespaces"]),
        "roles": list(g["roles"]),
        "expires_at": int(g["exp"]),
        "family": g["family"],
        "upstream_refresh": seal(UPSTREAM_PURPOSE, g["upstream_refresh"]),
        "version": time.time_ns(),
    }


def _held(g: Grant | Device, key: str) -> str | None:
    return g.get("held", {}).get(key)


class ClickHouseStore:
    """OAuth state in Keeper and loom.oauth_state: durable across restarts
    and replicas."""

    def _ch(self) -> Any:
        from server.core.ch import get_client

        return get_client()

    async def _get(self, key: str) -> str | None:
        return await claims.get(self._ch(), key)

    async def _swap(self, key: str, old: str | None, new: str, expires_at: int) -> str | None:
        return await claims.swap(self._ch(), key, old, new, expires_at)

    async def _load(self, key: str) -> tuple[dict[str, Any], str] | None:
        value = await self._get(key)
        return (json.loads(value), value) if value is not None else None

    async def _put(self, key: str, old: str | None, record: dict[str, Any]) -> str:
        """Write `record` (kept until its `exp`) over `old` (None: a new key);
        the value written. Raises claims.Changed when someone else changed it
        first."""
        new = json.dumps(record)
        if await self._swap(key, old, new, record["exp"]) != new:
            raise claims.Changed(key)
        return new

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
        """A new code, or a spent one put back."""
        key = f"code:{token_id(code)}"
        await self._put(key, _held(g, key), _record(g, _GRANT_FIELDS, spent=False))

    async def take_code(
        self, code: str, client_id: str, accept: Callable[[Grant], bool]
    ) -> Grant | None:
        """`accept` (the redirect URI and PKCE verifier match) is checked before
        the spend, like the client: a code presented wrongly is left unspent."""
        key = f"code:{token_id(code)}"
        loaded = await self._load(key)
        if loaded is None or loaded[0]["exp"] < int(time.time()):
            return None
        record, value = loaded
        grant = _grant(record)
        if grant["client_id"] != client_id or not accept(grant):
            return None
        if record["spent"]:
            raise CodeReplayed(grant)
        try:
            spent = await self._put(key, value, {**record, "spent": True, "nonce": claims.nonce()})
        except claims.Changed:
            await self._raise_if_spent(key, grant)
            raise
        return {**grant, "held": {key: spent}}

    async def save_refresh(self, token: str, g: Grant) -> None:
        """Make `token` its session's current refresh token: a new session's
        first, the next in a rotation, or a spent one put back. Its grant is
        written before the token is handed out."""
        rid = token_id(token)
        await self._save_refresh_grant(rid, g)
        key = f"family:{g['family']}"
        if await self._swap(key, _held(g, key), rid, g["exp"]) != rid:
            raise claims.Changed(key)

    async def _save_refresh_grant(self, rid: str, g: Grant) -> None:
        await self._ch().insert(_T, [_refresh_row(rid, g)])

    async def _refresh_grant(self, rid: str) -> Grant | None:
        from server.core.ch import own_write_reads

        with own_write_reads():  # handed out a moment ago, maybe through another replica
            row = await self._ch().query_one(
                f"""SELECT client_id, subject, org, namespaces, roles, expires_at, family,
                           upstream_refresh
                    FROM {_T} FINAL
                    WHERE record_type = 'refresh' AND id = {{id:String}} LIMIT 1""",
                params={"id": rid},
            )
        if not row:
            return None
        return {
            "client_id": row["client_id"],
            "redirect_uri": "",
            "code_challenge": "",
            "subject": row["subject"],
            "org": row["org"],
            "namespaces": list(row["namespaces"]),
            "roles": list(row["roles"]),
            "exp": int(row["expires_at"]),
            "family": row["family"],
            "upstream_refresh": unseal(UPSTREAM_PURPOSE, row["upstream_refresh"]),
        }

    async def take_refresh(self, token: str, client_id: str) -> Grant | None:
        rid = token_id(token)
        grant = await self._refresh_grant(rid)
        if grant is None or grant["exp"] < int(time.time()) or grant["client_id"] != client_id:
            return None
        if not grant["family"]:
            return None  # issued before sessions had families: its owner signs in again
        key = f"family:{grant['family']}"
        spending = f"spending:{claims.nonce()}"
        for attempt in range(2):
            now = await self._swap(key, rid, spending, grant["exp"])
            if now == spending:
                return {**grant, "held": {key: spending}}
            if now is None:
                return None  # the session's key is gone: it has expired
            if attempt == 0:
                # Another token, or a Keeper replica a moment behind: look again.
                await asyncio.sleep(0.2)
        raise CodeReplayed(grant)  # no longer the session's current token: spent

    async def save_pending(self, state: str, data: dict[str, Any]) -> None:
        exp = int(time.time()) + PENDING_TTL
        record = {"data": data, "exp": exp, "spent": False, "nonce": claims.nonce()}
        await self._put(f"pending:{state}", None, record)

    async def take_pending(self, state: str) -> dict[str, Any] | None:
        key = f"pending:{state}"
        loaded = await self._load(key)
        if loaded is None:
            return None
        record, value = loaded
        if record["spent"] or record["exp"] < int(time.time()):
            return None
        try:
            await self._put(key, value, {**record, "spent": True, "nonce": claims.nonce()})
        except claims.Changed:
            return None  # the same callback twice: the other one finishes it
        data: dict[str, Any] = record["data"]
        return data

    async def use_once(self, key: str, expires_at: int) -> bool:
        """Mark `key` used until `expires_at`; False if it already was."""
        mark = f"used:{claims.nonce()}"
        return await self._swap(key, None, mark, expires_at) == mark

    async def owe_revocation(self, family: str, reason: str) -> None:
        """Record that the session must be revoked: the revocation itself did
        not land. revocation.refresh() counts the family as revoked from it."""
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

    async def session_ended(self, family: str) -> bool:
        """Whether the session is revoked or owed its revocation, read from
        storage rather than this process's revocation snapshot."""
        from server.auth.revocation import revoked_outright

        return await revoked_outright(family)

    async def save_device(self, d: Device) -> None:
        """A new device grant, or its next step (from the version this
        request read), or a redeemed one put back."""
        key = f"device:{d['id']}"
        held = _held(d, key)
        if held is None:  # new: its user code leads to it
            await self._put(f"user_code:{d['user_code']}", None, {"id": d["id"], "exp": d["exp"]})
        await self._put(key, held, _record(d, _DEVICE_FIELDS, spent=False))

    async def _device(self, device_id: str) -> tuple[Device, dict[str, Any]] | None:
        key = f"device:{device_id}"
        loaded = await self._load(key)
        if loaded is None:
            return None
        record, value = loaded
        return {**_device(record), "held": {key: value}}, record

    async def device(self, device_id: str) -> Device | None:
        found = await self._device(device_id)
        return found[0] if found and not found[1]["spent"] else None

    async def device_by_user_code(self, user_code_id: str) -> Device | None:
        """The live, undecided device grant whose user code keys to this."""
        pointer = await self._load(f"user_code:{user_code_id}")
        d = await self.device(pointer[0]["id"]) if pointer else None
        if d is None or d["status"] != "pending" or d["exp"] < int(time.time()):
            return None
        return d

    async def take_device(self, device_code: str) -> Device | None:
        """The device grant as the polling client sees it. An approved or
        denied grant is spent by this read; a pending one is not."""
        found = await self._device(token_id(device_code))
        if found is None:
            return None
        d, record = found
        if d["exp"] < int(time.time()):
            return d  # expired: the poller hears so, spent or not
        if record["spent"]:
            raise CodeReplayed(d)
        if d["status"] == "pending":
            return d
        key = f"device:{d['id']}"
        try:
            spent = await self._put(
                key, d["held"][key], {**record, "spent": True, "nonce": claims.nonce()}
            )
        except claims.Changed:
            await self._raise_if_spent(key, d)
            raise
        return {**d, "held": {key: spent}}

    async def _raise_if_spent(self, key: str, grant: Grant | Device) -> None:
        """After losing a spend: a replay if another request spent it, else
        (a change undone, a step taken meanwhile) the request just retries."""
        loaded = await self._load(key)
        if loaded is not None and loaded[0]["spent"]:
            raise CodeReplayed(grant)
