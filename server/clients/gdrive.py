"""Minimal client for a shared-folder store, read with a service account.

The credential is the service account's JSON key. It is exchanged for a short
access token at the store's token endpoint (a JWT signed with the key's private
key, the OAuth 2.0 JWT bearer grant) and the token goes in the `Authorization`
header only. Both hosts are constants here: a key's own `token_uri` is never
dialled.

Errors are the shared ones of `server.clients._vendor`.
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
    json_object,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVES_URL = "https://www.googleapis.com/drive/v3/drives"
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
# Parent folders one listing query names, at most, so a query stays well inside the
# store's query length.
MAX_PARENTS = 50  # not a setting: a bound on one query, the same on every deployment
# What a listing reads of each file.
_LISTED = "id,name,mimeType,modifiedTime,webViewLink,size"
# What a probe's first listing page reads of each file.
_PAGE_FIELDS = "id,name,mimeType,modifiedTime"


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


async def require_folder(token: str, folder_id: str) -> dict[str, Any]:
    """The folder's metadata, with its `driveId` (a shared drive's own id names its
    root folder); NotFound when the account cannot see `folder_id` or it is no folder."""
    what = f"folder {folder_id}"
    data = await bearer_get(
        get_client(),
        f"{_FILES_URL}/{folder_id}",
        token,
        what,
        params={"fields": "mimeType,trashed,driveId", "supportsAllDrives": "true"},
    )
    if data.get("mimeType") != _FOLDER_MIME or data.get("trashed"):
        raise NotFound(f"{what}: not a folder")
    return data


async def require_drive(token: str, drive_id: str) -> None:
    """NotFound when the account is no member of the shared drive `drive_id`."""
    if not valid_file_id(drive_id):
        raise ValueError("not a drive id")
    await bearer_get(get_client(), f"{_DRIVES_URL}/{drive_id}", token, f"shared drive {drive_id}")


def quoted(value: str) -> str:
    """`value` as a quoted string of the store's query language."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _mime_clause(mime_types: list[str]) -> str:
    return "(" + " or ".join(f"mimeType = {quoted(m)}" for m in mime_types) + ")"


def folder_query(folder_ids: list[str], mime_types: list[str]) -> str:
    """The listing query for files of `mime_types` in any of `folder_ids`, not trashed.
    The ids were checked by the caller (`parse_items`)."""
    parents = " or ".join(f"{quoted(f)} in parents" for f in folder_ids)
    return f"({parents}) and trashed = false and {_mime_clause(mime_types)}"


def drive_query(mime_types: list[str]) -> str:
    """The listing query for a shared drive's files of `mime_types`, not trashed."""
    return f"trashed = false and {_mime_clause(mime_types)}"


def filter_query(item: dict[str, Any], mime_types: list[str]) -> str:
    """The listing query of a `filter` item: each field it names, every value
    quoted, so a field never widens the query past what it says."""
    parts = ["trashed = false", _mime_clause(mime_types)]
    if "name_contains" in item:
        parts.append(f"name contains {quoted(item['name_contains'])}")
    if "modified_after" in item:
        parts.append(f"modifiedTime > {quoted(item['modified_after'] + 'T00:00:00')}")
    if "in" in item:
        parts.append(f"{quoted(item['in'])} in parents")
    return " and ".join(parts)


async def list_files(
    token: str, query: str, *, drive_id: str = "", fields: str = _PAGE_FIELDS
) -> tuple[list[dict[str, Any]], bool]:
    """One page (1000 at most) of the files `query` matches, most recently changed
    first, and whether more may match: a next page, or a search the store did not
    finish; with `drive_id`, of that shared drive only."""
    data = await bearer_get(
        get_client(),
        _FILES_URL,
        token,
        "listing",
        params={
            **_corpus(drive_id),
            "q": query,
            "fields": f"nextPageToken,incompleteSearch,files({fields})",
            "pageSize": "1000",
            "orderBy": "modifiedTime desc",
        },
    )
    files = [f for f in data.get("files") or [] if isinstance(f, dict)]
    return files, bool(data.get("nextPageToken") or data.get("incompleteSearch"))


def _corpus(drive_id: str) -> dict[str, str]:
    """The listing parameters that say where to look: the account's own corpus (the
    files shared with it, and any folder's files by parent), or one shared drive."""
    params = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
    if drive_id:
        if not valid_file_id(drive_id):
            raise ValueError("not a drive id")
        params.update({"corpora": "drive", "driveId": drive_id})
    return params


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
    searched everything (`list_query`); MAX_PARENTS folders per query."""
    files: list[dict[str, Any]] = []
    whole = True
    for start in range(0, len(folder_ids), MAX_PARENTS):
        query = folder_query(folder_ids[start : start + MAX_PARENTS], mime_types)
        found, complete = await list_query(
            token, query, modified_after=modified_after, pace_s=pace_s
        )
        files.extend(found)
        whole = whole and complete
    return files, whole


async def list_query(
    token: str,
    query: str,
    *,
    drive_id: str = "",
    modified_after: str = "",
    fields: str = _LISTED,
    pace_s: float = PAGE_PACE_S,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Every file `query` matches (with `drive_id`, in that shared drive), oldest
    change first, or only those changed after `modified_after` (a `valid_stamp`),
    and whether the store searched everything: False when any page says
    `incompleteSearch`. Pages are paced; a failure on any page raises, so every page
    was read. With `limit`, paging stops once more than `limit` files are in."""
    if modified_after and not valid_stamp(modified_after):
        raise ValueError("modified_after is not a modification stamp")
    if modified_after:
        query += f" and modifiedTime > {quoted(modified_after)}"
    params = {
        **_corpus(drive_id),
        "q": query,
        "fields": f"nextPageToken,incompleteSearch,files({fields})",
        "pageSize": "1000",
        "orderBy": "modifiedTime",
    }
    files: list[dict[str, Any]] = []
    whole = True
    while True:
        data = await bearer_get(get_client(), _FILES_URL, token, "listing", params=params)
        whole = whole and not data.get("incompleteSearch")
        files.extend(f for f in data.get("files") or [] if isinstance(f, dict))
        page_token = data.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return files, whole
        if limit is not None and len(files) > limit:
            return files, whole
        params = {**params, "pageToken": page_token}
        await asyncio.sleep(pace_s)


async def subfolders(
    token: str, parent_ids: list[str], *, limit: int, pace_s: float = PAGE_PACE_S
) -> tuple[list[str], bool]:
    """The ids of the folders directly in any of `parent_ids`, and whether the store
    searched everything; MAX_PARENTS parents per query, and no page read once more
    than `limit` ids are in."""
    found: list[str] = []
    whole = True
    for start in range(0, len(parent_ids), MAX_PARENTS):
        if len(found) > limit:
            break
        query = folder_query(parent_ids[start : start + MAX_PARENTS], [_FOLDER_MIME])
        folders, complete = await list_query(
            token, query, fields="id", pace_s=pace_s, limit=limit - len(found)
        )
        found.extend(str(f.get("id") or "") for f in folders)
        whole = whole and complete
    return [f for f in found if valid_file_id(f)], whole


async def any_subfolder(token: str, parent_ids: list[str], seen: set[str]) -> bool:
    """Whether any of `parent_ids` holds a folder not in `seen`, from one page per
    MAX_PARENTS parents; a page with more to follow, or from a search the store did not
    finish, counts as holding one."""
    for start in range(0, len(parent_ids), MAX_PARENTS):
        query = folder_query(parent_ids[start : start + MAX_PARENTS], [_FOLDER_MIME])
        files, unsure = await list_files(token, query, fields="id")
        if unsure or any(str(f.get("id") or "") not in seen for f in files):
            return True
    return False


async def file_meta(token: str, file_id: str) -> dict[str, Any]:
    """One file as a listing reads it, with `trashed`; NotFound when the account
    cannot see it."""
    return await bearer_get(
        get_client(),
        _file_url(file_id),
        token,
        f"file {file_id}",
        params={"fields": f"{_LISTED},trashed", "supportsAllDrives": "true"},
    )


async def file_state(token: str, file_id: str) -> dict[str, Any]:
    """What says whether a file is still a source's: `trashed`, `parents`, `driveId`,
    and what a filter matches (`name`, `mimeType`, `modifiedTime`); NotFound when the
    account cannot see it."""
    return await bearer_get(
        get_client(),
        _file_url(file_id),
        token,
        f"file {file_id}",
        params={
            "fields": "trashed,parents,driveId,name,mimeType,modifiedTime",
            "supportsAllDrives": "true",
        },
    )


async def export_markdown(token: str, file_id: str, *, max_bytes: int) -> str:
    """A native document exported as Markdown."""
    data = await get_capped(
        get_client(),
        _file_url(file_id, "/export"),
        token,
        f"export {file_id}",
        max_bytes=max_bytes,
        params={"mimeType": "text/markdown"},
    )
    return data.decode("utf-8", errors="replace")


async def presentation(token: str, file_id: str, *, max_bytes: int) -> dict[str, Any]:
    """A native presentation's slides, shapes and speaker notes, its description
    read up to `max_bytes`."""
    if not valid_file_id(file_id):
        raise ValueError("not a file id")
    what = f"presentation {file_id}"
    data = await get_capped(
        get_client(), f"{_SLIDES_URL}/{file_id}", token, what, max_bytes=max_bytes
    )
    return json_object(data, what)


async def download(token: str, file_id: str, *, max_bytes: int) -> bytes:
    """An uploaded file's bytes."""
    return await get_capped(
        get_client(),
        _file_url(file_id),
        token,
        f"download {file_id}",
        max_bytes=max_bytes,
        params={"alt": "media", "supportsAllDrives": "true"},
    )


__all__ = [
    "MAX_PARENTS",
    "PAGE_PACE_S",
    "CredentialRefused",
    "NotFound",
    "RateLimited",
    "ServiceAccount",
    "TooLarge",
    "VendorError",
    "access_token",
    "any_subfolder",
    "download",
    "drive_query",
    "export_markdown",
    "file_meta",
    "file_state",
    "filter_query",
    "folder_query",
    "list_all",
    "list_files",
    "list_query",
    "parse_service_account",
    "presentation",
    "quoted",
    "require_drive",
    "require_folder",
    "subfolders",
    "valid_file_id",
    "valid_stamp",
]
