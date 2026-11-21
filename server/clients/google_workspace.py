"""Delegated Google reads; provider identity is not Loom source or reader authority.

The caller supplies finite I/O bounds. Crawl registration, registrant standing,
complete expansion, exclusions and publication are outside this transport boundary.
No request retries or follows redirects; provider errors retain no body or URL.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, SupportsIndex, cast
from urllib.parse import quote

import httpx

from server.clients import gdrive
from server.clients._http import get_client
from server.clients._vendor import (
    CredentialRefused,
    TooLarge,
    VendorError,
    raise_for_status,
    read_capped,
)

_now = time.monotonic
_pace = asyncio.sleep
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"
# not a setting: Directory members.list has a fixed documented page maximum.
_PAGE_SIZE = 200
# not a setting: the selector wire protocol caps the total people at 2000.
MAX_PEOPLE = 2000
type MemberKind = Literal["USER", "GROUP", "CUSTOMER"]


def _positive(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("a positive integer bound is required")


def _remaining(deadline: float) -> float:
    if type(deadline) not in (int, float):
        raise TimeoutError("delegated read deadline")
    try:
        remaining = deadline - _now()
    except OverflowError:
        raise TimeoutError("delegated read deadline") from None
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("delegated read deadline")
    return remaining


def _text(value: Any, *, maximum: int = 8192) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise VendorError("invalid directory response")
    return value


def _key(value: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9@._+\-]{1,512}", value)
        or "@" in value
        and (value.count("@") != 1 or not all(value.split("@")))
    ):
        raise ValueError("directory key refused")
    return quote(value, safe="")


class DelegatedToken:
    __slots__ = ("_value", "subject", "scope", "deadline")

    def __init__(self, value: str, subject: str, scope: gdrive.ReadScope, deadline: float) -> None:
        self._value, self.subject, self.scope, self.deadline = value, subject, scope, deadline

    def __repr__(self) -> str:
        return "DelegatedToken(<redacted>)"

    def __str__(self) -> str:
        raise TypeError("a delegated token is not a shared credential")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("a delegated token cannot be copied or serialized")


async def _directory_forbidden(response: httpx.Response, max_bytes: int, deadline: float) -> None:
    """Classify bounded reasons without retaining an echoed provider error in the message."""
    raw = await read_capped(response, max_bytes)
    _remaining(deadline)
    if raw is None:
        raise TooLarge("delegated response exceeds its byte bound", status=403)
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise VendorError("delegated read unavailable", status=403) from None
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        raise VendorError("delegated read unavailable", status=403)
    entries = error.get("errors")
    reasons = (
        [
            entry["reason"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("reason"), str) and entry["reason"]
        ]
        if isinstance(entries, list)
        else []
    )
    status = error.get("status")
    if isinstance(status, str) and status:
        reasons.append(status)
    if not reasons:
        # An unreadable quota response is not evidence of a revoked credential.
        raise VendorError("delegated read unavailable", status=403)
    classified = httpx.Response(403, json={"error": {"errors": [{"reason": r} for r in reasons]}})
    raise_for_status(classified, "delegated read")


async def _request(
    method: Literal["GET", "POST"],
    url: str,
    *,
    deadline: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    _positive(max_bytes)
    remaining = _remaining(deadline)
    try:
        async with asyncio.timeout_at(deadline):
            async with get_client().stream(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status != 200:
                    if status == 403 and url.startswith(_DIRECTORY + "/"):
                        await _directory_forbidden(response, max_bytes, deadline)
                    if status == 400 and url == _TOKEN_URL:
                        raise CredentialRefused("delegated read refused", status=status)
                    raise_for_status(httpx.Response(status), "delegated read")
                    raise VendorError("delegated read refused", status=status)
                raw = await read_capped(response, max_bytes)
    except httpx.RequestError:
        raise VendorError("delegated read unavailable") from None
    _remaining(deadline)
    if raw is None:
        raise TooLarge("delegated response exceeds its byte bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise VendorError("invalid delegated response") from None
    if not isinstance(value, dict) or value.get("error") or value.get("errors"):
        raise VendorError("invalid delegated response")
    _remaining(deadline)
    return value


async def delegated_token(
    account: gdrive.ServiceAccount,
    *,
    subject: str,
    scope: gdrive.ReadScope,
    deadline: float,
    max_bytes: int,
) -> DelegatedToken:
    """An exact read-scope grant; neither scope probing nor source admission."""
    _positive(max_bytes)
    _remaining(deadline)
    if not isinstance(subject, str):
        raise CredentialRefused("the delegated subject is not an email")
    assertion = gdrive._signed_assertion(account, scope=scope, subject=subject)
    value = await _request(
        "POST",
        _TOKEN_URL,
        deadline=deadline,
        max_bytes=max_bytes,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
    )
    token = _text(value.get("access_token"))
    expires = value.get("expires_in")
    token_type = value.get("token_type")
    if (
        not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or type(expires) is not int
        or expires <= 0
        or "scope" in value
        and (not isinstance(value["scope"], str) or value["scope"].split() != [scope.value])
    ):
        raise CredentialRefused("delegated token response refused")
    remaining = _remaining(deadline)
    expires_at = _now() + min(expires, remaining)
    return DelegatedToken(token, subject, scope, min(deadline, expires_at))


def _authorization(token: DelegatedToken, scope: gdrive.ReadScope) -> str:
    if token.scope is not scope:
        raise CredentialRefused("directory scope refused")
    _remaining(token.deadline)
    return "Bearer " + _text(token._value)


async def require_write_denied(
    account: gdrive.ServiceAccount,
    *,
    subject: str,
    scope: gdrive.WriteScope,
    deadline: float,
    max_bytes: int,
) -> None:
    """Only a bounded, explicit OAuth scope denial proves this fixed grant absent."""
    _positive(max_bytes)
    if not isinstance(scope, gdrive.WriteScope) or not isinstance(subject, str):
        raise CredentialRefused("write-scope proof refused")
    assertion = gdrive._signed_assertion(account, scope=scope, subject=subject)
    try:
        async with asyncio.timeout_at(deadline):
            async with get_client().stream(
                "POST",
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=_remaining(deadline),
                follow_redirects=False,
            ) as response:
                if response.status_code != 400:
                    raise_for_status(httpx.Response(response.status_code), "write-scope proof")
                    raise CredentialRefused("write delegation is not refused")
                raw = await read_capped(response, max_bytes)
    except httpx.RequestError:
        raise VendorError("write-scope proof unavailable") from None
    _remaining(deadline)
    if raw is None:
        raise TooLarge("write-scope proof exceeds its byte bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise VendorError("write-scope proof unavailable") from None
    if not isinstance(value, dict) or value.get("error") not in {
        "unauthorized_client",
        "invalid_scope",
    }:
        raise CredentialRefused("write-scope proof is not an explicit scope denial")


@dataclass(frozen=True, repr=False)
class DirectoryUser:
    id: str
    email: str
    aliases: tuple[str, ...]
    customer_id: str

    def __repr__(self) -> str:
        return "DirectoryUser(<redacted>)"


def _user(value: dict[str, Any]) -> DirectoryUser:
    if (
        value.get("suspended") is not False
        or value.get("archived") is not False
        or "deletionTime" in value
    ):
        raise CredentialRefused("directory person unavailable")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list):
        raise VendorError("invalid directory response")
    email = _email(value.get("primaryEmail"))
    resolved_aliases = tuple(_email(alias) for alias in aliases)
    return DirectoryUser(
        _text(value.get("id"), maximum=256),
        email,
        resolved_aliases,
        _text(value.get("customerId"), maximum=256),
    )


def _email(value: Any) -> str:
    email = _text(value, maximum=512)
    if email.count("@") != 1 or not all(email.split("@")):
        raise VendorError("invalid directory response")
    return email


async def directory_identity(token: DelegatedToken, *, max_bytes: int) -> DirectoryUser:
    """Resolve the delegated directory principal, without inventing a Loom identity."""
    return await _get_user(token, token.subject, max_bytes=max_bytes)


async def user(
    token: DelegatedToken, key: str, *, customer_id: str, max_bytes: int
) -> DirectoryUser:
    """A current active directory user in the explicitly expected workspace customer."""
    expected = _text(customer_id, maximum=256)
    found = await _get_user(token, key, max_bytes=max_bytes)
    if found.customer_id != expected:
        raise CredentialRefused("directory person unavailable")
    return found


async def _get_user(token: DelegatedToken, key: str, *, max_bytes: int) -> DirectoryUser:
    headers = {"Authorization": _authorization(token, gdrive.ReadScope.DIRECTORY_USERS)}
    value = await _request(
        "GET",
        f"{_DIRECTORY}/users/{_key(key)}",
        headers=headers,
        params={"fields": "id,primaryEmail,aliases,customerId,suspended,archived,deletionTime"},
        deadline=token.deadline,
        max_bytes=max_bytes,
    )
    found = _user(value)
    names = {email.lower() for email in (found.email, *found.aliases)}
    if key != found.id and key.lower() not in names:
        raise VendorError("directory identity mismatch")
    return found


@dataclass(frozen=True, repr=False)
class DirectoryMember:
    id: str
    kind: MemberKind
    email: str | None

    def __repr__(self) -> str:
        return "DirectoryMember(<redacted>)"


def _member(value: Any) -> DirectoryMember:
    if not isinstance(value, dict):
        raise VendorError("invalid directory response")
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in {"USER", "GROUP", "CUSTOMER"}:
        raise VendorError("invalid directory response")
    email = value.get("email")
    if email is not None:
        email = _email(email)
    return DirectoryMember(_text(value.get("id"), maximum=256), cast(MemberKind, kind), email)


async def group_members(
    token: DelegatedToken,
    key: str,
    *,
    max_bytes: int,
    max_pages: int,
    before_page: Callable[[], Awaitable[None]] | None = None,
) -> tuple[DirectoryMember, ...]:
    """Complete bounded pages of metadata; USER/status is no proof of an active employee.

    GROUP and CUSTOMER are explicit results, never automatically expanded. A caller
    must resolve USER identities and customer membership before using them as readers.
    """
    _positive(max_pages)
    headers = {"Authorization": _authorization(token, gdrive.ReadScope.DIRECTORY_MEMBERS)}
    url = f"{_DIRECTORY}/groups/{_key(key)}/members"
    cursor = ""
    cursors: set[str] = set()
    members: dict[tuple[str, str], DirectoryMember] = {}
    for _ in range(max_pages):
        if cursor:
            async with asyncio.timeout_at(token.deadline):
                await _pace(gdrive.PAGE_PACE_S)
        if before_page is not None:
            await before_page()
        value = await _request(
            "GET",
            url,
            deadline=token.deadline,
            max_bytes=max_bytes,
            headers=headers,
            params={
                "maxResults": _PAGE_SIZE,
                "includeDerivedMembership": "false",
                "fields": "members(id,type,email),nextPageToken",
                **({"pageToken": cursor} if cursor else {}),
            },
        )
        rows = value.get("members", [])
        if not isinstance(rows, list) or len(rows) > _PAGE_SIZE:
            raise VendorError("invalid directory response")
        for row in rows:
            member = _member(row)
            identity = member.kind, member.id
            if identity in members:
                raise VendorError("directory membership changed during pagination")
            members[identity] = member
            if len(members) > MAX_PEOPLE:
                raise TooLarge("directory membership exceeds the crawl bound")
        if "nextPageToken" not in value:
            return tuple(members.values())
        cursor = _text(value["nextPageToken"])
        if cursor in cursors:
            raise VendorError("directory pagination did not advance")
        cursors.add(cursor)
    raise TooLarge("directory membership exceeds its page bound")
