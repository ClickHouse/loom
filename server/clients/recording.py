"""Zoom transcripts for a `recording` source, never audio or video.

The credential is a server-to-server app's JSON (account_id, client_id and
client_secret), exchanged with the account-credentials grant. Ended meeting
instances are listed and each instance's TRANSCRIPT WebVTT file is downloaded.

Tokens must report read-only scopes. API hosts are fixed, and transcript downloads
go only to the store's HTTPS hosts. Errors are shared with server.clients._vendor.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

from server.clients._http import get_client
from server.clients._vendor import (
    CredentialRefused,
    NotFound,
    RateLimited,
    TooLarge,
    VendorError,
    bearer_get,
    exchange_token,
    get_capped,
)

_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
_ZOOM_URL = "https://api.zoom.us/v2"
# A meeting instance's uuid (base64 with `+`, `/` and `=`).
_UUID_RE = re.compile(r"[A-Za-z0-9+/=_-]{1,128}")


def _credential(credential: str, fields: tuple[str, ...], what: str) -> dict[str, str]:
    """The string `fields` of a JSON credential; CredentialRefused when one is missing."""
    try:
        data = json.loads(credential)
    except ValueError:
        raise CredentialRefused(f"the credential is not {what}") from None
    if not isinstance(data, dict):
        raise CredentialRefused(f"the credential is not {what}")
    values = {f: data.get(f) for f in fields}
    if not all(isinstance(v, str) and v for v in values.values()):
        raise CredentialRefused(f"the credential is not {what}: it needs {', '.join(fields)}")
    return {f: str(v) for f, v in values.items()}


def _read_only_zoom(scopes: list[str]) -> bool:
    """Every scope reads: `<resource>:read[:<detail>...]`."""
    return bool(scopes) and all(s.split(":")[1:2] == ["read"] for s in scopes)


async def _token(url: str, what: str, read_only: Callable[[list[str]], bool], **kw: Any) -> str:
    answer = await exchange_token(get_client(), url, what, **kw)
    # Fail closed: a credential that can change the store, or that does not say what
    # it can do, is refused, so a stored credential never holds more than reads.
    scope = answer.get("scope")
    if not isinstance(scope, str) or not read_only(scope.split()):
        raise CredentialRefused(f"{what}: the credential grants more than read-only scopes")
    return str(answer["access_token"])


async def zoom_token(credential: str) -> str:
    """An access token from a server-to-server app's credential."""
    fields = _credential(
        credential, ("account_id", "client_id", "client_secret"), "a server-to-server app's JSON"
    )
    basic = base64.b64encode(f"{fields['client_id']}:{fields['client_secret']}".encode()).decode()
    return await _token(
        _ZOOM_TOKEN_URL,
        "token exchange",
        _read_only_zoom,
        data={"grant_type": "account_credentials", "account_id": fields["account_id"]},
        headers={"Authorization": f"Basic {basic}"},
    )


async def zoom_instances(token: str, meeting_id: str) -> list[dict[str, Any]]:
    """A meeting's ended instances (`uuid`, `start_time`); NotFound when the account
    has no such meeting. `meeting_id` was checked by the caller (`parse_config`)."""
    data = await bearer_get(
        get_client(),
        f"{_ZOOM_URL}/past_meetings/{meeting_id}/instances",
        token,
        f"meeting {meeting_id}",
    )
    return [m for m in data.get("meetings") or [] if isinstance(m, dict)]


def valid_uuid(uuid: str) -> bool:
    return bool(_UUID_RE.fullmatch(uuid))


def uuid_path(uuid: str) -> str:
    """An instance's uuid as a path segment: encoded twice when it starts with `/` or
    holds `//`, as the store asks, once otherwise."""
    if not valid_uuid(uuid):
        raise ValueError("not a meeting instance uuid")
    once = quote(uuid, safe="")
    return quote(once, safe="") if uuid.startswith("/") or "//" in uuid else once


async def zoom_recording(token: str, uuid: str) -> dict[str, Any] | None:
    """One meeting instance's recording (`share_url`, `recording_files`), or None
    when the instance was not recorded."""
    try:
        return await bearer_get(
            get_client(), f"{_ZOOM_URL}/meetings/{uuid_path(uuid)}/recordings", token, "recording"
        )
    except NotFound:
        return None


def zoom_download_allowed(url: str) -> bool:
    """Whether a transcript's download link is on the store's own https hosts, the
    only place its token may go."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "zoom.us" or host.endswith(".zoom.us"))


async def zoom_download(token: str, url: str, *, max_bytes: int) -> str:
    """A transcript file's text, read up to `max_bytes`; TooLarge past it."""
    if not zoom_download_allowed(url):
        raise ValueError("not a download link of the store")
    body = await get_capped(get_client(), url, token, "transcript download", max_bytes=max_bytes)
    return body.decode("utf-8", errors="replace")


__all__ = [
    "CredentialRefused",
    "NotFound",
    "RateLimited",
    "TooLarge",
    "VendorError",
    "uuid_path",
    "valid_uuid",
    "zoom_download",
    "zoom_download_allowed",
    "zoom_instances",
    "zoom_recording",
    "zoom_token",
]
