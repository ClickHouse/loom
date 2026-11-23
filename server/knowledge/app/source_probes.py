"""Bounded credential and transcript checks for shared recording sources."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from server.settings import settings

SAMPLE_TITLES = 5


@dataclass(frozen=True)
class Probe:
    documents: int
    sample: list[str] = field(default_factory=list)
    more: bool = False


class SourceUnreachable(Exception):
    pass


class CredentialRefused(Exception):
    pass


async def _probe_recording(
    items: list[dict[str, Any]], config: dict[str, Any], credential: str
) -> Probe:
    from server.clients import recording
    from server.knowledge.app import source_sync_recording

    if not isinstance(credential, str):
        raise CredentialRefused("a recording source requires its shared credential")
    try:
        session = await source_sync_recording.open_session(config["provider"], credential)
        found, lost = await source_sync_recording.list_recordings(session, config["ids"])
    except recording.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except recording.VendorError as e:
        raise SourceUnreachable(str(e)) from None
    if lost:
        raise SourceUnreachable(f"{lost[0]}; check the id and that the account can see it")
    found.sort(key=lambda r: r["modifiedTime"], reverse=True)
    titles = [r["name"] for r in found[:SAMPLE_TITLES]]
    return Probe(documents=min(len(found), 1000), sample=titles, more=len(found) > 1000)


async def probe(kind: str, config: dict[str, Any], credential: str) -> Probe:
    if kind != "recording":
        raise SourceUnreachable("this source kind is unavailable")
    limit = float(settings.knowledge_source_probe_timeout_s)
    try:
        async with asyncio.timeout(limit):
            return await _probe_recording([], config, credential)
    except TimeoutError:
        raise SourceUnreachable(f"the source did not answer within {limit:g} s") from None
