"""Minimal clients for the two meeting stores a `recording` source reads: their
transcripts only, never the audio or video.

`meet`: the credential is an OAuth `authorized_user` JSON file (client id, client
secret and the refresh token of a user who can see the spaces, granted the
`meetings.space.readonly` scope). The refresh token is exchanged for a short access
token at the token endpoint. A space's conference records are listed, and each
record's transcripts read as their timed entries.

`zoom`: the credential is a server-to-server app's JSON (`account_id`, `client_id`,
`client_secret`), exchanged with the account-credentials grant. A meeting's ended
instances are listed, and each instance's recording files read for its
`TRANSCRIPT` file, a WebVTT file downloaded with the token.

Both token exchanges are refused (`CredentialRefused`) when the answer's `scope`
holds anything beyond reads (for `zoom`, every scope `<resource>:read...`) or names
no scope. Every host is a constant here, and a transcript download goes only to the
store's own https hosts. Errors are the shared ones of `server.clients._vendor`.
"""

from __future__ import annotations

import asyncio
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

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_MEET_URL = "https://meet.googleapis.com/v2"
_ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
_ZOOM_URL = "https://api.zoom.us/v2"
# Seconds between listing pages, so a long walk stays under the store's rate limit.
PAGE_PACE_S = 0.2  # not a setting: the stores' limits, the same on every deployment
# Resource names the meeting store hands out; they go into request paths.
_ID = r"[A-Za-z0-9_-]{1,256}"
_RECORD_RE = re.compile(rf"conferenceRecords/{_ID}")
_TRANSCRIPT_RE = re.compile(rf"conferenceRecords/{_ID}/transcripts/{_ID}")
# A meeting instance's uuid (base64 with `+`, `/` and `=`).
_UUID_RE = re.compile(r"[A-Za-z0-9+/=_-]{1,128}")
# The only scope a `meet` token may carry: read the spaces' conference records.
MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"


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


def _read_only_meet(scopes: list[str]) -> bool:
    return set(scopes) == {MEET_SCOPE}


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


async def _pages(
    url: str, token: str, params: dict[str, Any], key: str, what: str
) -> list[dict[str, Any]]:
    """Every item under `key` across the pages of a listing; a failing page raises."""
    items: list[dict[str, Any]] = []
    while True:
        data = await bearer_get(get_client(), url, token, what, params=params)
        items.extend(i for i in data.get(key) or [] if isinstance(i, dict))
        page_token = data.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return items
        params = {**params, "pageToken": page_token}
        await asyncio.sleep(PAGE_PACE_S)


# meet


async def meet_token(credential: str) -> str:
    """An access token from an `authorized_user` credential's refresh token."""
    fields = _credential(
        credential, ("client_id", "client_secret", "refresh_token"), "an authorized_user JSON file"
    )
    return await _token(
        _GOOGLE_TOKEN_URL,
        "token exchange",
        _read_only_meet,
        data={"grant_type": "refresh_token", **fields},
    )


async def meet_space(token: str, space_id: str) -> dict[str, Any]:
    """The space (its canonical `name`, `meetingUri`); NotFound when the user cannot
    see it. `space_id` was checked by the caller (`parse_config`)."""
    return await bearer_get(
        get_client(), f"{_MEET_URL}/spaces/{space_id}", token, f"space {space_id}"
    )


async def meet_records(
    token: str, space_name: str, *, label: str, ended_after: str = "", page_size: int = 100
) -> list[dict[str, Any]]:
    """The space's ended conference records, or those that ended at or after
    `ended_after` (an RFC 3339 time). `space_name` is the space's own `name`, for the
    filter; `label` names the space in errors (`space <configured id>`). A record
    still running has no `endTime` and is left out."""
    query = f'space.name = "{space_name}"'
    if ended_after:
        query += f' AND end_time >= "{ended_after}"'
    found = await _pages(
        f"{_MEET_URL}/conferenceRecords",
        token,
        {"filter": query, "pageSize": str(page_size)},
        "conferenceRecords",
        label,
    )
    return [r for r in found if r.get("endTime")]


def valid_record(name: str) -> bool:
    return bool(_RECORD_RE.fullmatch(name))


async def meet_transcripts(token: str, record: str) -> list[dict[str, Any]]:
    """A conference record's transcripts (`name`, `state`, `startTime`)."""
    if not valid_record(record):
        raise ValueError("not a conference record name")
    return await _pages(
        f"{_MEET_URL}/{record}/transcripts", token, {"pageSize": "100"}, "transcripts", record
    )


async def meet_participants(token: str, record: str) -> dict[str, str]:
    """{participant name: display name} of a conference record."""
    if not valid_record(record):
        raise ValueError("not a conference record name")
    found = await _pages(
        f"{_MEET_URL}/{record}/participants", token, {"pageSize": "250"}, "participants", record
    )
    names: dict[str, str] = {}
    for p in found:
        for kind in ("signedinUser", "anonymousUser", "phoneUser"):
            shown = (p.get(kind) or {}).get("displayName")
            if isinstance(shown, str) and shown:
                names[str(p.get("name") or "")] = shown
    return names


async def meet_entries(token: str, transcript: str, *, max_bytes: int) -> list[dict[str, Any]]:
    """A transcript's entries in time order (`participant`, `text`, `startTime`,
    `endTime`); TooLarge once their text passes `max_bytes`."""
    if not _TRANSCRIPT_RE.fullmatch(transcript):
        raise ValueError("not a transcript name")
    url = f"{_MEET_URL}/{transcript}/entries"
    params: dict[str, Any] = {"pageSize": "100"}
    entries: list[dict[str, Any]] = []
    size = 0
    while True:
        data = await bearer_get(get_client(), url, token, transcript, params=params)
        for entry in data.get("transcriptEntries") or []:
            if isinstance(entry, dict):
                entries.append(entry)
                size += len(str(entry.get("text") or "").encode())
        if size > max_bytes:
            raise TooLarge(f"{transcript}: over {max_bytes} bytes")
        page_token = data.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return entries
        params = {**params, "pageToken": page_token}
        await asyncio.sleep(PAGE_PACE_S)


# zoom


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
    "PAGE_PACE_S",
    "CredentialRefused",
    "NotFound",
    "RateLimited",
    "TooLarge",
    "VendorError",
    "meet_entries",
    "meet_participants",
    "meet_records",
    "meet_space",
    "meet_token",
    "meet_transcripts",
    "uuid_path",
    "valid_record",
    "valid_uuid",
    "zoom_download",
    "zoom_download_allowed",
    "zoom_instances",
    "zoom_recording",
    "zoom_token",
]
