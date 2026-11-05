"""Minimal client for a shared-folder store, read with a service account.

The credential is the service account's JSON key. It is exchanged for a short
access token at the store's token endpoint (a JWT signed with the key's private
key, the OAuth 2.0 JWT bearer grant) and the token goes in the `Authorization`
header only. Both hosts are constants here: a key's own `token_uri` is never
dialled.

Errors are the shared ones of `server.clients._vendor`; a 403 whose error reason
is a rate limit raises `RateLimited`, not `CredentialRefused`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

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
    answer = await exchange_token(
        get_client(),
        _TOKEN_URL,
        "token exchange",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
    )
    return str(answer["access_token"])


async def _get_bytes(
    url: str, token: str, params: dict[str, Any], what: str, max_bytes: int
) -> bytes:
    """The body of a GET, read up to `max_bytes`; TooLarge past it."""
    return await get_capped(get_client(), url, token, what, max_bytes=max_bytes, params=params)


async def require_folder(token: str, folder_id: str) -> None:
    """NotFound when the account cannot see `folder_id` or it is no folder."""
    what = f"folder {folder_id}"
    data = await bearer_get(
        get_client(),
        f"{_FILES_URL}/{folder_id}",
        token,
        what,
        {"fields": "mimeType,trashed", "supportsAllDrives": "true"},
    )
    if data.get("mimeType") != _FOLDER_MIME or data.get("trashed"):
        raise NotFound(f"{what}: not a folder")


def _folder_query(folder_ids: list[str], mime_types: list[str]) -> str:
    """The listing query for files of `mime_types` in any of `folder_ids`, not trashed.
    The ids and types were checked by the caller (`parse_config`)."""
    parents = " or ".join(f"'{f}' in parents" for f in folder_ids)
    kinds = " or ".join(f"mimeType = '{m}'" for m in mime_types)
    return f"({parents}) and trashed = false and ({kinds})"


async def list_files(
    token: str, folder_ids: list[str], mime_types: list[str]
) -> tuple[list[dict[str, Any]], bool]:
    """One page (1000 at most) of the files of `mime_types` in any of `folder_ids`,
    and whether more follow: one call for every folder."""
    data = await bearer_get(
        get_client(),
        _FILES_URL,
        token,
        "listing",
        {
            "q": _folder_query(folder_ids, mime_types),
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "1000",
            "orderBy": "modifiedTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
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
) -> tuple[list[dict[str, Any]], bool]:
    """Every file of `mime_types` in any of `folder_ids`, oldest change first, or only
    those changed after `modified_after` (a `valid_stamp`), and whether the store
    searched everything: False when any page says `incompleteSearch`. Pages are
    paced; a failure on any page raises, so every page was read."""
    if modified_after and not valid_stamp(modified_after):
        raise ValueError("modified_after is not a modification stamp")
    query = _folder_query(folder_ids, mime_types)
    if modified_after:
        query += f" and modifiedTime > '{modified_after}'"
    params = {
        "q": query,
        "fields": (
            "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,webViewLink,size)"
        ),
        "pageSize": "1000",
        "orderBy": "modifiedTime",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    files: list[dict[str, Any]] = []
    whole = True
    while True:
        data = await bearer_get(get_client(), _FILES_URL, token, "listing", params)
        whole = whole and not data.get("incompleteSearch")
        files.extend(f for f in data.get("files") or [] if isinstance(f, dict))
        page_token = data.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return files, whole
        params = {**params, "pageToken": page_token}
        await asyncio.sleep(pace_s)


async def file_state(token: str, file_id: str) -> dict[str, Any]:
    """A file's `trashed` flag and `parents`; NotFound when the account cannot see it."""
    return await bearer_get(
        get_client(),
        _file_url(file_id),
        token,
        f"file {file_id}",
        {"fields": "trashed,parents", "supportsAllDrives": "true"},
    )


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
        raise VendorError(f"{what}: body is not JSON") from None
    if not isinstance(deck, dict):
        raise VendorError(f"{what}: body is not an object")
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
    "NotFound",
    "RateLimited",
    "ServiceAccount",
    "TooLarge",
    "VendorError",
    "access_token",
    "download",
    "export_markdown",
    "file_state",
    "list_all",
    "list_files",
    "parse_service_account",
    "presentation",
    "require_folder",
    "valid_file_id",
    "valid_stamp",
]
