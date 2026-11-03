"""Minimal client for a shared-folder store, read with a service account.

The credential is the service account's JSON key. It is exchanged for a short
access token at the store's token endpoint (a JWT signed with the key's private
key, the OAuth 2.0 JWT bearer grant) and the token goes in the `Authorization`
header only. Both hosts are constants here: a key's own `token_uri` is never
dialled.

Errors carry the HTTP status, never the store's response body, which can name
files or the store's own hosts. A rate limit raises `RateLimited`, a refused
key or token `CredentialRefused`, an id the account cannot see `NotFound`, a
file over the caller's size bound `TooLarge`, and any other failure
`DriveError`. Nothing here retries: the sync worker's next pass is the retry.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from server.clients._http import get_client

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_SLIDES_URL = "https://slides.googleapis.com/v1/presentations"
_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_FOLDER_MIME = "application/vnd.google-apps.folder"
# Seconds between listing pages, so a long walk stays under the store's per-user
# rate limit.
PAGE_PACE_S = 0.7  # not a setting: the store's limit, the same on every deployment
# A modification stamp as the store reports it (RFC 3339, UTC). A stamp goes into the
# listing's query string, so nothing else is let through.
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
# A file id goes into a request path; the store's own ids are in this alphabet.
_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class DriveError(RuntimeError):
    """`status` is the HTTP status the store answered, 0 when it gave none (a
    network error, a body that is not JSON, a bound passed here)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        """The store refused this request (a 4xx), as opposed to failing to answer."""
        return 400 <= self.status < 500


class RateLimited(DriveError):
    pass


class CredentialRefused(DriveError):
    pass


class NotFound(DriveError):
    pass


class TooLarge(DriveError):
    pass


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    # Kept out of repr, so a logged or raised account never shows the key.
    private_key: str = field(repr=False)


def parse_service_account(credential: str) -> ServiceAccount:
    """The service account a JSON key names; CredentialRefused when it is not one."""
    try:
        data = json.loads(credential)
    except ValueError:
        raise CredentialRefused("the credential is not a service account JSON key") from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise CredentialRefused("the credential is not a service account JSON key")
    email = data.get("client_email")
    key = data.get("private_key")
    if not isinstance(email, str) or not email or not isinstance(key, str) or not key:
        raise CredentialRefused("the service account key has no client_email or private_key")
    return ServiceAccount(client_email=email, private_key=key)


# The `reason` (or `status`) the store's error body gives a 403 that is a rate
# limit, not a refusal; compared lower-cased.
_RATE_LIMIT_REASONS = {
    "ratelimitexceeded",
    "userratelimitexceeded",
    "dailylimitexceeded",
    "quotaexceeded",
    "rate_limit_exceeded",
    "resource_exhausted",
}


def _error_reasons(resp: httpx.Response) -> set[str]:
    """The lower-cased `error.errors[].reason` and `error.status` of an error body, or
    the OAuth token endpoint's `error` string."""
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return set()
    if isinstance(error, str):
        return {error.lower()} if error else set()
    if not isinstance(error, dict):
        return set()
    reasons = {str(e.get("reason") or "") for e in error.get("errors") or [] if isinstance(e, dict)}
    reasons.add(str(error.get("status") or ""))
    return {r.lower() for r in reasons if r}


def _rate_limited(resp: httpx.Response) -> bool:
    """A 429, or a 403 whose error reason is a rate limit rather than a refusal."""
    return resp.status_code == 429 or (
        resp.status_code == 403 and bool(_error_reasons(resp) & _RATE_LIMIT_REASONS)
    )


def _raise_for(resp: httpx.Response, what: str) -> None:
    if _rate_limited(resp):
        raise RateLimited(f"{what}: rate_limited", status=resp.status_code)
    if resp.status_code in (401, 403):
        raise CredentialRefused(f"{what}: http_{resp.status_code}", status=resp.status_code)
    if resp.status_code == 404:
        raise NotFound(f"{what}: http_404", status=404)
    if resp.status_code >= 400:
        raise DriveError(f"{what}: http_{resp.status_code}", status=resp.status_code)


def _json(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        raise DriveError(f"{what}: body is not JSON") from None
    if not isinstance(data, dict):
        raise DriveError(f"{what}: body is not an object")
    return data


async def access_token(account: ServiceAccount) -> str:
    """A read-only access token for the service account."""
    import jwt

    now = int(time.time())
    claims = {
        "iss": account.client_email,
        "scope": _SCOPE,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 600,
    }
    try:
        assertion = jwt.encode(claims, account.private_key, algorithm="RS256")
    # A public key or a non-RSA key raises AttributeError or InvalidKeyError here.
    except (ValueError, TypeError, AttributeError, jwt.PyJWTError):
        raise CredentialRefused("the service account's private_key is not an RSA key") from None
    try:
        resp = await get_client().post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    except httpx.RequestError as e:
        raise DriveError(f"token exchange: {type(e).__name__}") from e
    # A rate limit first; otherwise the token endpoint answers a key it does not know
    # or a revoked one with 400, 401 or 403.
    if _rate_limited(resp):
        raise RateLimited("token exchange: rate_limited")
    if resp.status_code in (400, 401, 403):
        raise CredentialRefused(f"token exchange: http_{resp.status_code}")
    _raise_for(resp, "token exchange")
    token = _json(resp, "token exchange").get("access_token")
    if not isinstance(token, str) or not token:
        raise DriveError("token exchange: no access_token in the answer")
    return token


async def _get(url: str, token: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        resp = await get_client().get(
            url, headers={"Authorization": f"Bearer {token}"}, params=params
        )
    except httpx.RequestError as e:
        raise DriveError(f"{what}: {type(e).__name__}") from e
    _raise_for(resp, what)
    return _json(resp, what)


async def _get_bytes(
    url: str, token: str, params: dict[str, Any], what: str, max_bytes: int
) -> bytes:
    """The body of a GET, read up to `max_bytes`; TooLarge past it."""
    try:
        async with get_client().stream(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, params=params
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()  # the error reasons tell a rate limit from a refusal
                _raise_for(resp, what)
            body = bytearray()
            async for piece in resp.aiter_bytes():
                body.extend(piece)
                if len(body) > max_bytes:
                    raise TooLarge(f"{what}: over {max_bytes} bytes")
    except httpx.RequestError as e:
        raise DriveError(f"{what}: {type(e).__name__}") from e
    return bytes(body)


async def require_folder(token: str, folder_id: str) -> None:
    """NotFound when the account cannot see `folder_id` or it is no folder."""
    what = f"folder {folder_id}"
    data = await _get(
        f"{_FILES_URL}/{folder_id}",
        token,
        {"fields": "mimeType,trashed", "supportsAllDrives": "true"},
        what,
    )
    if data.get("mimeType") != _FOLDER_MIME or data.get("trashed"):
        raise NotFound(f"{what}: not a folder")


async def list_files(
    token: str, folder_ids: list[str], mime_types: list[str]
) -> tuple[list[dict[str, Any]], bool]:
    """One page (1000 at most) of the files of `mime_types` in any of `folder_ids`,
    and whether more follow: one call for every folder. The ids and types were
    checked by the caller (`parse_config`)."""
    parents = " or ".join(f"'{f}' in parents" for f in folder_ids)
    kinds = " or ".join(f"mimeType = '{m}'" for m in mime_types)
    data = await _get(
        _FILES_URL,
        token,
        {
            "q": f"({parents}) and trashed = false and ({kinds})",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "1000",
            "orderBy": "modifiedTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        "listing",
    )
    files = [f for f in data.get("files") or [] if isinstance(f, dict)]
    return files, bool(data.get("nextPageToken"))


def valid_file_id(file_id: str) -> bool:
    """Whether `file_id` is an id the store hands out, safe in a request path."""
    return bool(_FILE_ID_RE.fullmatch(file_id))


def _file_url(file_id: str, suffix: str = "") -> str:
    if not valid_file_id(file_id):
        raise ValueError("not a file id")
    return f"{_FILES_URL}/{file_id}{suffix}"


def valid_stamp(stamp: str) -> bool:
    """Whether `stamp` is a modification time as the store reports it."""
    return bool(_STAMP_RE.fullmatch(stamp))


async def list_all(
    token: str,
    folder_ids: list[str],
    mime_types: list[str],
    *,
    modified_after: str = "",
    pace_s: float = PAGE_PACE_S,
) -> list[dict[str, Any]]:
    """Every file of `mime_types` in any of `folder_ids`, oldest change first, or only
    those changed after `modified_after` (a `valid_stamp`). Pages are paced; a failure
    on any page raises, so a list returned is the whole listing."""
    if modified_after and not valid_stamp(modified_after):
        raise ValueError("modified_after is not a modification stamp")
    parents = " or ".join(f"'{f}' in parents" for f in folder_ids)
    kinds = " or ".join(f"mimeType = '{m}'" for m in mime_types)
    query = f"({parents}) and trashed = false and ({kinds})"
    if modified_after:
        query += f" and modifiedTime > '{modified_after}'"
    params = {
        "q": query,
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,size)",
        "pageSize": "1000",
        "orderBy": "modifiedTime",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    files: list[dict[str, Any]] = []
    while True:
        data = await _get(_FILES_URL, token, params, "listing")
        files.extend(f for f in data.get("files") or [] if isinstance(f, dict))
        page_token = data.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return files
        params = {**params, "pageToken": page_token}
        await asyncio.sleep(pace_s)


async def export_markdown(token: str, file_id: str, *, max_bytes: int) -> str:
    """A native document exported as Markdown."""
    data = await _get_bytes(
        _file_url(file_id, "/export"),
        token,
        {"mimeType": "text/markdown"},
        f"export {file_id}",
        max_bytes,
    )
    return data.decode("utf-8", errors="replace")


async def presentation(token: str, file_id: str, *, max_bytes: int) -> dict[str, Any]:
    """A native presentation's slides, shapes and speaker notes, its description
    read up to `max_bytes`."""
    if not valid_file_id(file_id):
        raise ValueError("not a file id")
    what = f"presentation {file_id}"
    data = await _get_bytes(f"{_SLIDES_URL}/{file_id}", token, {}, what, max_bytes)
    try:
        deck = json.loads(data)
    except ValueError:
        raise DriveError(f"{what}: body is not JSON") from None
    if not isinstance(deck, dict):
        raise DriveError(f"{what}: body is not an object")
    return deck


async def download(token: str, file_id: str, *, max_bytes: int) -> bytes:
    """An uploaded file's bytes."""
    return await _get_bytes(
        _file_url(file_id),
        token,
        {"alt": "media", "supportsAllDrives": "true"},
        f"download {file_id}",
        max_bytes,
    )


__all__ = [
    "PAGE_PACE_S",
    "CredentialRefused",
    "DriveError",
    "NotFound",
    "RateLimited",
    "ServiceAccount",
    "TooLarge",
    "access_token",
    "download",
    "export_markdown",
    "list_all",
    "list_files",
    "parse_service_account",
    "presentation",
    "require_folder",
    "valid_file_id",
    "valid_stamp",
]
