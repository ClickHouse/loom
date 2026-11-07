"""One sync turn of a `recording` source: the transcripts of a meeting store's
recordings (`server.clients.recording`), never their audio or video. The pass, its
bounds, the waits and the state it records are `server.knowledge.app.source_sync`'s;
this module lists recordings, reads their transcripts and writes them. The probe
(`source_probes`) uses the same listing.

A recording is one ended conference record of a configured space (`meet`) or one
ended instance of a configured meeting (`zoom`). It is listed as an item of the
shared helpers (`id`, `name` as its title, `modifiedTime` as its revision: when the
meeting ended, or for `zoom` when the instance started) with its address, the
store's API address of the recording, unique and stable. A recording whose stored
copy has the listed revision is skipped without a call.

Neither listing says whether a recording has a transcript (records and instances
carry no artifacts), so a recording not stored yet costs one check. One with no
finished transcript is counted `no_transcript`: until `SETTLE_S` after its revision
it is checked again whenever listed, at none of the pass bound; after that it is
given up on its revision, and `start_waits` grants it a retry on each completed
periodic full listing and on a full sync request. A transcript link off the store's
own hosts is given up on as `transcript_off_host`.

A turn lists every recording on its first turn, when asked and on the periodic full
listing (for `zoom`, the instances of the last `ZOOM_LISTING_DAYS`; `meet` keeps no
older records), otherwise those since the stamp less `SETTLE_S`, so a transcript the
store finishes a while after the meeting is still found. A recording the store no
longer lists is never retired: both stores delete recordings on their own schedule,
so absence is not the writer asking.

A space or meeting the account no longer sees leaves the turn failed while the other
ids' recordings are written. A rate limit and a refused credential are the turn's;
trouble with one transcript waits for that recording alone, as for a folder's files.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from server.clients import recording
from server.core.errors import ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.app import source_sync as sync
from server.knowledge.domain.sources import KnowledgeSource, json_object
from server.knowledge.domain.transcripts import Entry, parse_time, parse_vtt, transcript_markdown
from server.knowledge.store import sources as store
from server.settings import settings

log = logging.getLogger("loom.knowledge.source_sync")
# How far before the stamp an incremental listing reaches: the time a store may take
# to finish a transcript after the meeting.
SETTLE_S = 86400.0  # not a setting: the stores' processing delay, the same on every deployment
# How far back a `zoom` full listing reads instances, as `meet` keeps conference
# records: an old instance of a long recurring meeting with no transcript would
# otherwise cost a check on every full listing. Documents written from older
# instances stay, since absence retires nothing.
ZOOM_LISTING_DAYS = 30  # not a setting: matches the other store's record retention
_MEET_SPACE_RE = re.compile(r"spaces/[A-Za-z0-9_-]{1,128}")
# A transcript is read once its file is generated: the reference promises nothing
# about the entries of a transcript that has only ended.
_MEET_READABLE = "FILE_GENERATED"


class NoTranscript(Exception):
    """The store has no finished transcript for this recording (yet)."""


class TranscriptOffHost(Exception):
    """The recording's transcript link is not on the store's own hosts, so the
    token is never sent to it."""


@dataclass(frozen=True)
class Session:
    provider: str
    # Kept out of repr, so a logged or raised session never shows the token.
    token: str = field(repr=False)


async def open_session(provider: str, credential: str) -> Session:
    """A store session from the credential; recording.CredentialRefused when refused."""
    if provider == "meet":
        return Session(provider, await recording.meet_token(credential))
    return Session(provider, await recording.zoom_token(credential))


def _since(stamp: str) -> str:
    """`stamp` less SETTLE_S, as the stores write times; '' for a full listing."""
    seen = parse_time(stamp) if stamp else None
    if seen is None:
        return ""
    return (seen - timedelta(seconds=SETTLE_S)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(stamp: str) -> str:
    """`2026-01-02 10:00 UTC` from an RFC 3339 time."""
    seen = parse_time(stamp)
    return seen.strftime("%Y-%m-%d %H:%M UTC") if seen is not None else stamp


async def list_recordings(
    session: Session, ids: list[str], *, stamp: str = ""
) -> tuple[list[dict[str, Any]], list[str]]:
    """The recordings of every configured id, every one (no `stamp`) or those since
    the stamp, and the errors of the ids the account cannot see or read
    (`<what>: http_404`, `<what>: http_403`). Any other failure raises: the
    listing is the turn's."""
    since = _since(stamp)
    items: list[dict[str, Any]] = []
    lost: list[str] = []
    for one in ids:
        try:
            if session.provider == "meet":
                items.extend(await _meet_listing(session.token, one, since))
            else:
                items.extend(await _zoom_listing(session.token, one, since))
        except recording.NotFound as e:
            lost.append(str(e))
        except recording.CredentialRefused as e:
            # A 403 on one id is that space or meeting lost to the account, not a
            # refused credential: the token exchange said the credential holds, and
            # the other ids are still read.
            if e.status != 403:
                raise
            lost.append(str(e))
    # Two configured ids can name one space (its id and its meeting code): each
    # recording is handled once, as first listed.
    unique: dict[str, dict[str, Any]] = {}
    for f in items:
        unique.setdefault(f["id"], f)
    return list(unique.values()), lost


async def _meet_listing(token: str, space_id: str, since: str) -> list[dict[str, Any]]:
    space = await recording.meet_space(token, space_id)
    name = str(space.get("name") or "")
    if not _MEET_SPACE_RE.fullmatch(name):
        raise recording.VendorError(f"space {space_id}: no space name in the answer")
    out = []
    for record in await recording.meet_records(
        token, name, label=f"space {space_id}", ended_after=since
    ):
        record_name = str(record.get("name") or "")
        if not recording.valid_record(record_name):
            continue
        out.append(
            {
                "id": record_name.removeprefix("conferenceRecords/"),
                "name": f"Meeting {_day(str(record.get('startTime') or ''))}",
                "modifiedTime": str(record.get("endTime") or ""),
                "uri": f"https://meet.googleapis.com/v2/{record_name}",
                "started": str(record.get("startTime") or ""),
            }
        )
    return out


async def _zoom_listing(token: str, meeting_id: str, since: str) -> list[dict[str, Any]]:
    if not since:
        oldest = datetime.now(UTC) - timedelta(days=ZOOM_LISTING_DAYS)
        since = oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for instance in await recording.zoom_instances(token, meeting_id):
        uuid = str(instance.get("uuid") or "")
        started = str(instance.get("start_time") or "")
        if not recording.valid_uuid(uuid) or started < since:
            continue
        out.append(
            {
                "id": uuid,
                "name": f"Meeting {meeting_id} {_day(started)}",
                "modifiedTime": started,
                "uri": f"https://api.zoom.us/v2/meetings/{recording.uuid_path(uuid)}/recordings",
            }
        )
    return out


async def transcript(session: Session, item: dict[str, Any], *, max_bytes: int) -> str:
    """The recording's transcript as Markdown sections; NoTranscript when it has none."""
    if session.provider == "meet":
        text = await _meet_transcript(session.token, item, max_bytes)
    else:
        text = await _zoom_transcript(session.token, item, max_bytes)
    if not text:
        raise NoTranscript(str(item["id"]))
    return text


async def _meet_transcript(token: str, item: dict[str, Any], max_bytes: int) -> str:
    record = f"conferenceRecords/{item['id']}"
    found = await recording.meet_transcripts(token, record)
    # Every transcript, finished: the record's revision does not change once one is
    # written, so a later transcript of the same record would never be read.
    if not found or any(t.get("state") != _MEET_READABLE for t in found):
        return ""
    speakers = await recording.meet_participants(token, record)
    zero = parse_time(str(item.get("started") or ""))
    entries: list[Entry] = []
    # The bound is the recording's, across its transcripts.
    left = max_bytes
    for t in found:
        read = await recording.meet_entries(token, str(t.get("name") or ""), max_bytes=left)
        left -= sum(len(str(e.get("text") or "").encode()) for e in read)
        for e in read:
            start, end = (
                parse_time(str(e.get("startTime") or "")),
                parse_time(str(e.get("endTime") or "")),
            )
            if zero is None or start is None:
                continue
            offset = (start - zero).total_seconds()
            until = (end - zero).total_seconds() if end is not None else offset
            speaker = " ".join(speakers.get(str(e.get("participant") or ""), "").split())
            text = " ".join(str(e.get("text") or "").split())
            entries.append(Entry(offset, until, speaker, text))
    return transcript_markdown(entries)


async def _zoom_transcript(token: str, item: dict[str, Any], max_bytes: int) -> str:
    found = await recording.zoom_recording(token, str(item["id"]))
    files = [
        f
        for f in (found or {}).get("recording_files") or []
        if isinstance(f, dict)
        and f.get("file_type") == "TRANSCRIPT"
        and f.get("status", "completed") == "completed"
    ]
    if not files:
        return ""
    if not recording.zoom_download_allowed(str(files[0].get("download_url") or "")):
        raise TranscriptOffHost(str(item["id"]))
    vtt = await recording.zoom_download(token, str(files[0]["download_url"]), max_bytes=max_bytes)
    return transcript_markdown(parse_vtt(vtt))


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    """List the source's recordings and write the transcripts not stored yet."""
    detail = json_object(state.get("detail", ""))
    stamp = state.get("state_value", "")
    result = sync.TurnResult()
    result.full = bool(detail.get("full")) or parse_time(stamp) is None or sync.full_due(detail)
    sync.start_waits(result, detail)
    credential = unseal("knowledge_sources.credential", src.credential_enc)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        result.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return result
    listed_at = datetime.now(UTC)
    try:
        session = await open_session(src.config["provider"], credential)
        items, lost = await list_recordings(
            session, list(src.config.get("ids") or []), stamp="" if result.full else stamp
        )
    except recording.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return result
    except recording.CredentialRefused as e:
        result.fail("credential_invalid", sync.client_error(e))
        return result
    except recording.VendorError as e:
        result.fail("failed", sync.client_error(e))
        return result
    if lost:
        result.fail("failed", lost[0][:200])
    result.documents_seen = len(items)
    newest = max((f["modifiedTime"] for f in items), default="")
    result.newest = sync.capped(newest, listed_at)
    if result.full and result.complete:
        listed_ids = {f["id"] for f in items}
        result.waits = {k: v for k, v in result.waits.items() if k in listed_ids}
    stored = {
        d["file_id"]: d for d in await store.source_revisions(src.org, src.namespace, src.name)
    }
    for f in items:
        fetch_and_write = functools.partial(_fetch_and_write, src, session, f, run, result)
        prior = stored.get(f["id"])
        if not await sync.sync_item(src, f, f["uri"], prior, run, result, fetch_and_write):
            break
    return result


def _settled(f: dict[str, Any]) -> bool:
    """Whether a recording's revision is more than SETTLE_S ago: a transcript still
    missing then is given up on."""
    seen = parse_time(str(f.get("modifiedTime") or ""))
    if seen is None:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return (datetime.now(UTC) - seen).total_seconds() > SETTLE_S


async def _fetch_and_write(
    src: KnowledgeSource,
    session: Session,
    f: dict[str, Any],
    run: sync.Pass,
    result: sync.TurnResult,
) -> str:
    """Read and write one recording's transcript: `written`, `skipped` (another writer
    took the address during the fetch), `waiting`, `given_up`,
    `no_transcript` (a recent recording with none yet, its check given back to the
    pass bound) or `stop`, as `sync.sync_item` expects."""
    max_bytes = int(settings.knowledge_source_sync_max_file_bytes)
    try:
        text = await transcript(session, f, max_bytes=max_bytes)
    except NoTranscript:
        if _settled(f):
            return sync.give_up(f, result, "no_transcript")
        run.left += 1  # a check that found no transcript fetched no document
        result.unconverted_for("no_transcript")
        return "no_transcript"
    except TranscriptOffHost:
        return sync.give_up(f, result, "transcript_off_host")
    except recording.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return "stop"
    except recording.TooLarge:
        return sync.give_up(f, result, "too_large")
    except recording.VendorError as e:
        if e.status == 401:
            result.fail("credential_invalid", sync.client_error(e))
            return "stop"
        if e.refused:
            return sync.give_up(f, result, f"http_{e.status}")
        # A 5xx, a timeout, an answer that is not JSON: it may read next time.
        reason = f"http_{e.status}" if e.status else "unreachable"
        return sync.wait(f, result, reason, sync.client_error(e))
    except Exception as e:  # noqa: BLE001 — one transcript's conversion must not stop the others
        log.warning("knowledge_source_sync: reading %s failed: %s", f["id"], e)
        return sync.wait(f, result, f"fetch_failed:{type(e).__name__}", type(e).__name__)
    await sync.require_current(src)
    try:
        written = await sync.write_document(src, f, f["uri"], text, ingested_format="transcript")
    except ValidationError as e:
        # The write refuses this transcript whatever the pass: it will not go through.
        code = e.to_detail().get("code") or "invalid"
        return sync.give_up(f, result, f"write_refused:{code}")
    except Exception as e:  # noqa: BLE001 — one transcript's write must not stop the others
        log.warning("knowledge_source_sync: writing %s failed: %s", f["id"], e)
        return sync.wait(f, result, f"write_failed:{type(e).__name__}", type(e).__name__)
    return sync.counted(result, written)


__all__ = [
    "SETTLE_S",
    "ZOOM_LISTING_DAYS",
    "NoTranscript",
    "Session",
    "TranscriptOffHost",
    "list_recordings",
    "open_session",
    "transcript",
    "turn",
]
