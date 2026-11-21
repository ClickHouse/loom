"""A complete Workspace union precedes every content, ACL and membership publication."""

from __future__ import annotations

import dataclasses
import functools
from datetime import UTC, datetime

from server.clients import gdrive
from server.clients._vendor import VendorError
from server.core.ch import sequential_reads, set_query_tag
from server.knowledge.app import crawl_authority, mirrors, private_sync
from server.knowledge.app import source_gdrive_items as items
from server.knowledge.app import source_sync as sync
from server.knowledge.app import source_sync_mirror as mirror_sync
from server.knowledge.domain import access_list
from server.knowledge.domain.source_crawl import Expansion, File, Union
from server.knowledge.domain.sources import (
    KnowledgeSource,
    home,
    json_object,
    mirror_uri,
    mirror_uri_suffix,
)
from server.knowledge.store import crawl_publication, documents
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import sources as store
from server.settings import settings


async def observe(
    authority: crawl_authority.Authority,
    run: sync.Pass,
    *,
    maximum_files: int,
    observations: dict[str, items.Listing] | None = None,
    result: sync.TurnResult | None = None,
) -> tuple[Expansion, tuple[File, ...]]:
    """No partial person's listing, membership or ACL reaches persistent storage."""
    source = authority.source
    expansion = await authority.prepare()
    union = Union(expansion, maximum_files)
    for person in expansion.people:
        if run.budget.exhausted():
            raise TimeoutError("crawl union exceeds the pass budget")
        token = await authority.drive(person)
        listing = await items.list_items(
            token,
            source.items,
            source.config.get("include", []),
            fields=gdrive.LISTED_WITH_PERMISSIONS + ",owners(me,emailAddress)",
            out_of_time=run.budget.exhausted,
            limit=maximum_files,
        )
        await token.check()
        visibility = result if result is not None else sync.TurnResult()
        if (
            not await sync._items_visible(token, source.items, visibility, run.budget.exhausted)
            or not visibility.complete
        ):
            raise VendorError("crawl selected items unavailable")
        if observations is not None:
            observations[person.id] = listing
        for file in listing.files:
            if "permissions" not in file:
                file["permissions"] = await gdrive.file_permissions(token, str(file["id"]))
                await token.check()
        union.add(
            person,
            listing.files,
            whole=(
                listing.whole
                and not listing.ran_out
                and not listing.lost_item
                and not listing.walk_cut
            ),
        )
    await authority.validate()
    files = union.complete()
    addresses = {sync.source_uri_of(file.metadata) for file in files}
    if len(addresses) != len(files) or any(not uri.startswith("https://") for uri in addresses):
        raise ValueError("crawl file address is unavailable or ambiguous")
    return expansion, files


async def turn(
    source: KnowledgeSource,
    state: dict[str, str],
    run: sync.Pass,
    *,
    bounds: crawl_authority.Bounds | None = None,
    membership_bound: int | None = None,
    maximum_files: int | None = None,
) -> sync.TurnResult:
    """Publish only a complete observation under the exact retained registration."""
    set_query_tag("knowledge.crawl.sync")
    bounds = bounds or crawl_authority.configured_bounds()
    maximum_files = settings.knowledge_crawl_max_files if maximum_files is None else maximum_files
    if membership_bound is None:
        # A group can be reached by configured alias, primary address and provider id.
        membership_bound = 3 * bounds.directory.groups + len(source.crawl_options["subjects"])
    authority = await crawl_authority.resolve(source, deadline=run.budget.stop_after, bounds=bounds)
    asked = json_object(state.get("detail", ""))
    known = await mirrors.live_mirror(source.org, source.namespace)
    detail = await mirror_sync._own_state(source, known, asked)
    result = sync.TurnResult(full=bool(detail.get("full")) or sync.full_due(detail))
    sync.start_waits(result, detail)
    observations: dict[str, items.Listing] = {}
    started = datetime.now(UTC)
    try:
        expansion, files = await observe(
            authority, run, maximum_files=maximum_files, observations=observations, result=result
        )
    except (VendorError, ValueError, TimeoutError):
        result.fail("failed", "crawl observation incomplete")
        return result
    async with authority.using(expansion.people):
        mirror = await mirrors.ensure_mirror(
            source.org,
            source.namespace,
            still_current=functools.partial(sync.require_current, source),
        )
        if mirror != known:
            detail = await mirror_sync._own_state(source, "", asked)
            result.full = bool(detail.get("full")) or sync.full_due(detail)
            sync.start_waits(result, detail)
        source = dataclasses.replace(source, mirror=mirror)
        result.mirror = mirror
        async with private_sync.mirror_costs(source.org, mirror, sync.WORKER):
            await _publish(
                source,
                authority,
                expansion,
                files,
                observations,
                started,
                run,
                result,
                membership_bound,
            )
    return result


async def _permissions(authority: crawl_authority.Authority, file: File) -> access_list.AccessList:
    denied = await authority.exclusions.excluded_addresses(file.access.emails)
    return dataclasses.replace(
        file.access, emails=tuple(e for e in file.access.emails if e not in denied)
    )


async def _publish(
    source: KnowledgeSource,
    authority: crawl_authority.Authority,
    expansion: Expansion,
    files: tuple[File, ...],
    observations: dict[str, items.Listing],
    started: datetime,
    run: sync.Pass,
    result: sync.TurnResult,
    membership_bound: int,
) -> None:
    result.documents_seen = len(files)
    with sequential_reads():
        copies = await store.every_copy(source.org, *home(source))
    stored = sync.index_with_room(result, [c for c in copies if c["searchable"]])
    people = {person.id: person for person in expansion.people}
    listed = {
        mirror_uri(sync.source_uri_of(file.metadata), source.source_id): file for file in files
    }
    # Narrow every old ACL before fresh content or a complete-listing proof can land.
    lists = {copy["source_uri"]: access_list.UNREAD for copy in copies}
    for uri, file in listed.items():
        permissions = await _permissions(authority, file)
        previous = next((copy for copy in copies if copy["source_uri"] == uri), None)
        if previous is not None and previous["source_revision"] != str(
            file.metadata.get("modifiedTime") or ""
        ):
            permissions = access_list.common(
                access_list.from_json(previous["access_json"]), permissions
            )
        lists[uri] = permissions
    if not await mirror_sync._write_lists(source, lists):
        result.fail("failed", "crawl permissions changed ownership")
        return
    for uri, file in listed.items():
        selected = tuple(people[id] for id in file.presenters)
        async with authority.using(selected):
            token = await authority.drive(selected[0])
            read_access = functools.partial(_permissions, authority, file)
            if not await sync.sync_file(
                source,
                token,
                file.metadata,
                uri,
                stored.get(file.id),
                run,
                result,
                read_access=read_access,
            ):
                break
    if not result.complete or result.status != "ok" or run.budget.exhausted():
        return
    if await documents.publications_owed(
        source.org, source.mirror, mirror_uri_suffix(source.source_id)
    ):
        return
    for copy in copies:
        if copy["source_uri"] in listed:
            continue
        if run.spent():
            result.mark(sync.TurnMark.PASS_BOUND)
            return
        run.left -= 1
        gone = await _gone(authority, expansion, observations, copy["file_id"], result)
        if gone is None:
            return
        if gone:
            async with sync.publication_guard(source):
                if await sync.retire(source, copy["source_uri"]):
                    result.removed += 1
    async with sync.publication_guard(source):
        await crawl_publication.groups(source, expansion, started, maximum=membership_bound)
        await mirror_store.record_listing(
            source.org,
            source.mirror,
            source.source_id,
            source.name,
            started,
            crawl_exclusion_generation=await authority.exclusions.version(),
            crawl_generation=source.crawl_generation,
        )


async def _gone(
    authority: crawl_authority.Authority,
    expansion: Expansion,
    observations: dict[str, items.Listing],
    file_id: str,
    result: sync.TurnResult,
) -> bool | None:
    # Page movement can omit a reached file. Every eligible presenter must confirm absence.
    if not expansion.people:
        return False
    for person in expansion.people:
        token = await authority.drive(person)
        gone = await sync.file_gone(token, file_id, observations[person.id], result)
        await token.check()
        if not gone:
            return gone
    return True
