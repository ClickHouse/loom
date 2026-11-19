"""An operator's bounded import into a separately owned tracker record."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from server.clients import github_feed as provider
from server.core.lease_writes import fenced
from server.core.repo_base import _CHClient
from server.core.secrets_at_rest import resealed, seal, unseal
from server.memory.app.provenance import writer_actor
from server.tracker.domain import github_feed as domain
from server.tracker.store import github_feed as store

VISIBILITY_TTL_S = 900  # not a setting: fixed freshness ceiling for stored access observations
TURN_TIMEOUT_S = 120  # not a setting: execution ceiling for one operator CLI turn


@dataclass(frozen=True)
class _Observation:
    repository_id: int
    private: bool
    observed: datetime


def _restricts(current: dict[str, Any], proof: _Observation | None) -> bool:
    return proof is not None and (
        current["repository_id"] != proof.repository_id
        or proof.private
        and not current["is_private"]
    )


async def _invalidate(
    ch: _CHClient, current: dict[str, Any], uncertain: Callable[[bool], None]
) -> None:
    uncertain(True)
    await _put(ch, current | {"available": 0, "visible_until": domain.EPOCH})
    uncertain(False)


async def _observe(ch: _CHClient, repo: str, token: str) -> _Observation:
    observed = await store.clock(ch)
    data = await provider.metadata(repo, token)
    ident, private = _metadata(repo, data)
    return _Observation(ident, private, observed)


def identity(org: str) -> dict[str, str]:
    if not org or org == "-":
        raise domain.FeedInputError("feed requires an explicit organization")
    return dict(
        org=org,
        namespace="-",
        agent="-",
        user_id="-",
        session_id="-",
        actor=writer_actor("process:tracker-github-feed"),
        source_class="imported",
    )


def _key(database_uuid: str) -> str:
    return f"tracker-feed.writer/{database_uuid}"


async def require_owner(ch: _CHClient, org: str, owner_uuid: str) -> None:
    row = await store.owner(ch)
    if (
        not row
        or row["org"] != org
        or str(row["owner_uuid"]) != owner_uuid
        or row["schema_version"] != 1
    ):
        raise domain.FeedInputError("record belongs to another deployment or schema")


async def initialize(ch: _CHClient, org: str, owner_uuid: str) -> None:
    target = await store.database_uuid(ch)
    if target == owner_uuid:
        raise domain.FeedInputError("record database must differ from the deployment database")
    async with fenced(_key(target)) as uncertain:
        found = {row["name"] for row in await store.objects(ch)}
        if found:
            if "feed_owner" not in found or found - set(store.TABLES + store.VIEWS):
                raise domain.FeedInputError("feed initialization requires an empty database")
        # The CREATE's comment claims ownership atomically across deployments;
        # an interrupted first insert remains resumable by that owner alone.
        tag = store.owner_tag(org, owner_uuid)
        uncertain(True)
        await store.claim_owner(ch, org, owner_uuid)
        if await store.owner_marker(ch) != tag:
            raise domain.FeedInputError("record belongs to another deployment or organization")
        if await store.owner(ch) is None:
            await ch.insert(
                "feed_owner",
                [
                    identity(org)
                    | dict(
                        owner_uuid=uuid.UUID(owner_uuid),
                        schema_version=1,
                    )
                ],
            )
        await require_owner(ch, org, owner_uuid)
        uncertain(False)
        for statement in store.statements()[1:]:
            uncertain(True)
            await ch.execute(statement)
            uncertain(False)


def _metadata(repo: str, data: dict[str, Any]) -> tuple[int, bool]:
    name = data.get("full_name")
    ident = data.get("id")
    private = data.get("private")
    if not isinstance(name, str) or name.lower() != repo.lower() or not isinstance(private, bool):
        raise domain.FeedInputError("repository identity or visibility is unverifiable")
    return domain.positive_id(ident), private


async def _put(ch: _CHClient, row: dict[str, Any]) -> None:
    rotated = resealed(domain.SECRET_PURPOSE, row["token_enc"])
    if rotated is not None:
        row = row | {"token_enc": rotated}
    await store.put_repository(ch, row | {"revision": uuid.uuid4()})


async def configure(
    ch: _CHClient,
    *,
    org: str,
    owner_uuid: str,
    repo: str,
    token: str,
    allowed_repos: set[str],
    max_pages: int = 5,
) -> None:
    repo = domain.repository(repo)
    registered = {name for name in allowed_repos if name.lower() == repo.lower()}
    if not registered:
        raise domain.FeedInputError("repository is outside the registered source allowlist")
    if len(registered) > 1:
        raise domain.FeedInputError("source allowlist must use one spelling per repository")
    repo = registered.pop()
    if not token or not 2 <= max_pages <= 100:
        raise domain.FeedInputError("a read-only token and 2..100 max-pages are required")
    await require_owner(ch, org, owner_uuid)
    target = await store.database_uuid(ch)
    snapshot = await store.repository(ch, org, repo)
    try:
        proof = await _observe(ch, repo, token)
    except (provider.FeedProviderError, domain.FeedInputError):
        if snapshot:
            await _hide(ch, snapshot, target, owner_uuid)
        raise
    async with fenced(_key(target)) as uncertain:
        await require_owner(ch, org, owner_uuid)
        existing = await store.repository(ch, org, repo)
        if existing and existing["repository_id"] != proof.repository_id:
            await _invalidate(ch, existing, uncertain)
            raise domain.FeedInputError("repository identity changed; use a separate record")
        if (existing is None) != (snapshot is None) or (
            existing and snapshot and str(existing["revision"]) != str(snapshot["revision"])
        ):
            if existing and _restricts(existing, proof):
                await _invalidate(ch, existing, uncertain)
            raise domain.FeedInputError("feed changed while configuring; retry the turn")
        row = identity(org) | dict(
            repository=repo,
            repository_id=proof.repository_id,
            owner_uuid=uuid.UUID(owner_uuid),
            token_enc=seal(domain.SECRET_PURPOSE, token),
            is_private=int(proof.private),
            available=1,
            visible_until=proof.observed + timedelta(seconds=VISIBILITY_TTL_S),
            cursors=existing["cursors"]
            if existing and (proof.private or not existing["is_private"])
            else "{}",
            max_pages=max_pages,
        )
        uncertain(True)
        await _put(ch, row)
        uncertain(False)


async def _hide(
    ch: _CHClient,
    row: dict[str, Any],
    target: str,
    owner_uuid: str,
    *,
    proof: _Observation | None = None,
) -> None:
    async with fenced(_key(target)) as uncertain:
        await require_owner(ch, row["org"], owner_uuid)
        current = await store.repository(ch, row["org"], row["repository"])
        if not current:
            return
        if str(current["revision"]) == str(row["revision"]) or _restricts(current, proof):
            await _invalidate(ch, current, uncertain)


async def _fetch(
    row: dict[str, Any], stream: str, since: datetime | None, token: str
) -> tuple[provider.Batch, dict[str, list[dict[str, Any]]]]:
    repo = row["repository"]
    batch = await provider.pages(repo, token, stream, since, max_pages=row["max_pages"])
    records: dict[str, list[dict[str, Any]]] = {}
    for item in batch.items:
        if stream == "items":
            is_pr = "pull_request" in item
            data = (
                await provider.pull_request(repo, token, domain.positive_id(item.get("number")))
                if is_pr
                else item
            )
            record = domain.item_row(repo, data, is_pr=is_pr)
            table = "feed_prs" if is_pr else "feed_issues"
        else:
            record = domain.comment_row(repo, item, review=stream == "review_comments")
            table = "feed_comments"
        records.setdefault(table, []).append(record)
    return batch, records


async def _publish(
    ch: _CHClient,
    row: dict[str, Any],
    owner_uuid: str,
    target: str,
    stream: str,
    cursor: datetime | None,
    records: dict[str, list[dict[str, Any]]],
    proof: _Observation,
) -> bool:
    async with fenced(_key(target)) as uncertain:
        await require_owner(ch, row["org"], owner_uuid)
        current = await store.repository(ch, row["org"], row["repository"])
        if not current:
            return False
        if str(current["revision"]) != str(row["revision"]):
            if _restricts(current, proof):
                await _invalidate(ch, current, uncertain)
            return False
        # Reset before the first public write so an interrupted replay resumes.
        if current["is_private"] and not proof.private:
            current["cursors"] = "{}"
        current |= dict(
            is_private=int(proof.private),
            available=1,
            visible_until=proof.observed + timedelta(seconds=VISIBILITY_TTL_S),
        )
        uncertain(True)
        await _put(ch, current)
        for table, items in records.items():
            # Readers can learn this table's part before repository metadata.
            stamped = [item | {"is_private": int(proof.private)} for item in items]
            await store.put_items(ch, table, stamped, identity(row["org"]), row["repository"])
        cursors = json.loads(current["cursors"])
        if cursor is not None:
            cursors[stream] = cursor.isoformat()
        await _put(ch, current | {"cursors": json.dumps(cursors, sort_keys=True)})
        uncertain(False)
        return True


async def sync(
    ch: _CHClient, *, org: str, owner_uuid: str, repo: str, allowed_repos: set[str]
) -> dict[str, int]:
    repo = domain.repository(repo)
    if repo.lower() not in {name.lower() for name in allowed_repos}:
        raise domain.FeedInputError("repository is outside the registered source allowlist")
    await require_owner(ch, org, owner_uuid)
    target = await store.database_uuid(ch)
    counts: dict[str, int] = {}
    async with asyncio.timeout(TURN_TIMEOUT_S):
        for stream in provider.STREAMS:
            row = await store.repository(ch, org, repo)
            if not row:
                raise domain.FeedInputError("repository feed is not configured")
            token = unseal(domain.SECRET_PURPOSE, row["token_enc"])
            if not token:
                await _hide(ch, row, target, owner_uuid)
                raise domain.FeedInputError("feed credential cannot be opened")
            cursors = json.loads(row["cursors"])
            since = domain.timestamp(cursors[stream]) if stream in cursors else None
            proof = None
            try:
                proof = await _observe(ch, repo, token)
                if proof.repository_id != row["repository_id"]:
                    raise domain.FeedInputError("repository identity changed")
                if row["is_private"] and not proof.private:
                    since = None
                batch, records = await _fetch(row, stream, since, token)
                cursor = domain.next_cursor(batch.items, complete=batch.complete, previous=since)
            except (Exception, asyncio.CancelledError):  # rejected scans invalidate visibility
                await _hide(ch, row, target, owner_uuid, proof=proof)
                raise
            published = await _publish(ch, row, owner_uuid, target, stream, cursor, records, proof)
            if not published:
                raise domain.FeedInputError("feed changed while fetching; retry the turn")
            counts[stream] = len(batch.items)
    return counts
