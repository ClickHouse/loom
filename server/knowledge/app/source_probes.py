"""The probe of a document source: an authenticated check and listing with the
credential, before anything is stored (add, rotate) or on request (check),
bounded as a whole by LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S.

Each kind has one probe function. It answers a `Probe` or raises
`SourceUnreachable` / `CredentialRefused`; the credential is held only for the
call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from server.knowledge.domain.sources import GDRIVE_MIME_TYPES
from server.settings import settings

# Titles a probe returns, at most.
SAMPLE_TITLES = 5


@dataclass(frozen=True)
class Probe:
    # Documents the source lists now: the first 1000 at most (gdrive), the first
    # page of each project (linear), the recordings listed, the first 1000 at most
    # (recording).
    documents: int
    # Up to five of their titles, most recently changed first.
    sample: list[str] = field(default_factory=list)
    # True when more match than were listed, so `documents` is a lower bound.
    more: bool = False


class SourceUnreachable(Exception):
    pass


class CredentialRefused(Exception):
    pass


async def _require_folders(gdrive: Any, token: str, folder_ids: list[str]) -> None:
    """Check every folder at once; the first failure cancels the rest and is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            for folder_id in folder_ids:
                group.create_task(gdrive.require_folder(token, folder_id))
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None


async def _probe_gdrive(config: dict[str, Any], credential: str) -> Probe:
    from server.clients import gdrive

    mime_types = [GDRIVE_MIME_TYPES[kind] for kind in config["include"]]
    try:
        account = gdrive.parse_service_account(credential)
        token = await gdrive.access_token(account)
        # Every folder must be one the account sees (an unshared folder lists
        # empty, not refused); the checks run together, then one listing covers all.
        await _require_folders(gdrive, token, config["folder_ids"])
        found, more = await gdrive.list_files(token, config["folder_ids"], mime_types)
    except gdrive.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except gdrive.NotFound as e:
        raise SourceUnreachable(f"{e}; share the folder with the service account") from None
    except gdrive.DriveError as e:
        raise SourceUnreachable(str(e)) from None
    # The listing is ordered most recently changed first.
    titles = [str(f.get("name") or "") for f in found[:SAMPLE_TITLES]]
    return Probe(documents=len(found), sample=titles, more=more)


async def _probe_linear(config: dict[str, Any], credential: str) -> Probe:
    from server.clients import linear

    try:
        # One page of each project: every project must be one the key sees.
        async with asyncio.TaskGroup() as group:
            pages = [
                group.create_task(linear.documents_page(credential, project_id))
                for project_id in config["project_ids"]
            ]
    except ExceptionGroup as failed:
        e = failed.exceptions[0]
        if isinstance(e, linear.CredentialRefused):
            raise CredentialRefused(str(e)) from None
        if isinstance(e, linear.NotFound):
            raise SourceUnreachable(f"{e}; the key's account must see the project") from None
        if isinstance(e, linear.LinearError):
            raise SourceUnreachable(str(e)) from None
        raise e from None
    found = [node for page in pages for node in page.result()[0]]
    found.sort(key=lambda node: str(node.get("updatedAt") or ""), reverse=True)
    titles = [str(node.get("title") or "") for node in found[:SAMPLE_TITLES]]
    more = any(page.result()[1] for page in pages)
    return Probe(documents=len(found), sample=titles, more=more)


async def _probe_recording(config: dict[str, Any], credential: str) -> Probe:
    from server.clients import recording
    from server.knowledge.app import source_sync_recording

    try:
        session = await source_sync_recording.open_session(config["provider"], credential)
        found, lost = await source_sync_recording.list_recordings(session, config["ids"])
    except recording.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except recording.RecordingError as e:
        raise SourceUnreachable(str(e)) from None
    if lost:
        raise SourceUnreachable(f"{lost[0]}; check the id and that the account can see it")
    found.sort(key=lambda r: r["modifiedTime"], reverse=True)
    titles = [r["name"] for r in found[:SAMPLE_TITLES]]
    return Probe(documents=min(len(found), 1000), sample=titles, more=len(found) > 1000)


PROBES: dict[str, Callable[[dict[str, Any], str], Awaitable[Probe]]] = {
    "gdrive": _probe_gdrive,
    "linear": _probe_linear,
    "recording": _probe_recording,
}


async def probe(kind: str, config: dict[str, Any], credential: str) -> Probe:
    """The kind's probe, bounded by LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S as a whole."""
    limit = float(settings.knowledge_source_probe_timeout_s)
    try:
        async with asyncio.timeout(limit):
            return await PROBES[kind](config, credential)
    except TimeoutError:
        raise SourceUnreachable(f"the source did not answer within {limit:g} s") from None


__all__ = ["PROBES", "CredentialRefused", "Probe", "SourceUnreachable", "probe"]
