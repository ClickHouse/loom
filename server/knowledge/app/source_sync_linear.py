"""One sync turn of a `linear` source: a project tracker's documents, read over its
GraphQL API (`server.clients.linear`). The pass, its bounds, the waits and the
state it records are `server.knowledge.app.source_sync`'s; this module lists,
fetches and retires the documents of one source.

A project's document list is small, so every turn reads each project's whole list
(ids, titles, stamps and links, every page), with no incremental listing. A
document whose stored copy has the listed revision (`updatedAt`) and address is
skipped without a fetch; a changed one is fetched (its content is Markdown
already) and written with `knowledge.upsert` like a folder's files. Its address
is the document's own link, which changes with its title, so a renamed document is
written at its new address and its old one retired.

The list is the positive evidence for removals: only when every project was read
on every page is a stored document missing from it looked up, and retired when
the tracker reports it not found, trashed, archived or in none of the source's
projects. A project the key cannot see, or any other listing failure, leaves the
turn failed and retires nothing, while the other projects' documents are written.

Trouble with one document is that document's, as for a folder's files: a refusal
or a document over the size bound is given up on its revision at once, any other
failure on it waits. A rate limit (a 429 or a GraphQL error typed `ratelimited`)
and a refused key are the turn's.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from server.clients import linear
from server.core.errors import ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
from server.knowledge.app import source_sync as sync
from server.knowledge.app import writes
from server.knowledge.domain import convert
from server.knowledge.domain.sources import KnowledgeSource, json_object, source_tag
from server.knowledge.store import sources as store
from server.settings import settings
from server.workers import pass_context

log = logging.getLogger("loom.knowledge.source_sync")


def _as_file(node: dict[str, Any]) -> dict[str, Any]:
    """A listed document under the keys the shared wait helpers read (`id`,
    `modifiedTime`), with its title and its address."""
    return {
        "id": str(node.get("id") or ""),
        "name": str(node.get("title") or ""),
        "modifiedTime": str(node.get("updatedAt") or ""),
        # Without query or fragment, so it stays one citation token.
        "uri": str(node.get("url") or "").split("#", 1)[0].split("?", 1)[0],
    }


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync._Pass) -> sync.TurnResult:
    """List every project's documents, write the changed ones, and retire those a
    complete list no longer has."""
    detail = json_object(state.get("detail", ""))
    result = sync.TurnResult()
    # Every list is whole; `full` (the first turn, a full request, the periodic
    # one) only grants the given-up documents their retry.
    result.full = bool(detail.get("full")) or not state.get("state_value") or sync._full_due(detail)
    sync._start_waits(result, detail)
    key = unseal("knowledge_sources.credential", src.credential_enc)
    if not key:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        result.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return result
    projects = list(src.config.get("project_ids") or [])
    listed_at = datetime.now(UTC)
    files: dict[str, dict[str, Any]] = {}
    for project_id in projects:
        try:
            nodes = await linear.project_documents(key, project_id)
        except linear.RateLimited:
            result.fail("rate_limited", "rate_limited")
            return result
        except linear.CredentialRefused as e:
            result.fail("credential_invalid", sync._error(e))
            return result
        except linear.LinearError as e:
            # This project's documents are unknown: nothing is retired this turn,
            # and the other projects' documents are still written.
            result.fail("failed", sync._error(e))
            continue
        for node in nodes:
            f = _as_file(node)
            if linear.valid_id(f["id"]):
                files.setdefault(f["id"], f)
    # Only a list read whole, every project on every page, says what is absent.
    trusted = result.complete
    result.documents_seen = len(files)
    newest = max((f["modifiedTime"] for f in files.values()), default="")
    result.newest = sync._capped(newest, listed_at)
    if trusted:
        result.waits = {k: v for k, v in result.waits.items() if k in files}
    stored = {
        d["file_id"]: d for d in await store.source_revisions(src.org, src.namespace, src.name)
    }
    for f in files.values():
        if not await _sync_document(src, key, f, stored.get(f["id"]), run, result):
            break
    if trusted and result.complete:
        # A document listed without an https link counts as absent, so its stored
        # copy is kept only if the lookup finds it still in a project.
        listed = {k: f["uri"] for k, f in files.items() if f["uri"].startswith("https://")}
        await _retire_absent(src, key, listed, run, result)
    return result


async def _sync_document(
    src: KnowledgeSource,
    key: str,
    f: dict[str, Any],
    prior: dict[str, str] | None,
    run: sync._Pass,
    result: sync.TurnResult,
) -> bool:
    """Handle one listed document; False when the turn must stop."""
    uri, revision = f["uri"], f["modifiedTime"]
    if not uri.startswith("https://"):
        result.unconverted_for("no_address")
        return True
    if prior and prior["source_revision"] == revision and prior["source_uri"] == uri:
        result.waits.pop(f["id"], None)
        return True  # unchanged: no fetch, no embedding
    entry = sync._waiting(result, f)
    if entry is not None and int(entry[1]) >= sync._WAIT_PASSES:
        # It waited its passes on this revision: unconverted until it changes.
        result.unconverted_for(str(entry[2]))
        return True
    # Before any fetch: an address another writer holds costs nothing.
    if not await sync._may_write(src, uri):
        result.waits.pop(f["id"], None)
        result.skipped += 1
        return True
    if run.spent():
        result.stop_at_bound()
        return False
    run.left -= 1
    outcome = await _fetch_and_write(src, key, f, result)
    if outcome == "stop":
        return False
    if outcome not in ("waiting", "given_up"):
        result.waits.pop(f["id"], None)
    if outcome == "written" and prior and prior["source_uri"] != uri:
        if await sync.retire(src, prior["source_uri"]):
            result.removed += 1
    return True


async def _fetch_and_write(
    src: KnowledgeSource, key: str, f: dict[str, Any], result: sync.TurnResult
) -> str:
    """Fetch and write one document: `written`, `waiting`, `given_up` or `stop`, as
    `source_sync._fetch_and_write` answers for a folder's file."""
    max_bytes = int(settings.knowledge_source_sync_max_file_bytes)
    try:
        doc = await linear.document(key, f["id"], max_bytes=max_bytes)
    except linear.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return "stop"
    except linear.CredentialRefused as e:
        result.fail("credential_invalid", sync._error(e))
        return "stop"
    except (linear.TooLarge, linear.NotFound) as e:
        # Over the bound, or gone since the listing: the next list says which.
        return sync._give_up(f, result, e.reason)
    except linear.LinearError as e:
        if e.refused:
            return sync._give_up(f, result, e.reason)
        # A 5xx, a timeout, an answer without data: this document may fetch next time.
        return sync._wait(f, result, e.reason, sync._error(e))
    await sync._require_current(src)
    try:
        await _write(src, f, str(doc.get("content") or ""))
    except ValidationError as e:
        # The write refuses this document whatever the pass: it will not go through.
        code = e.to_detail().get("code") or "invalid"
        return sync._give_up(f, result, f"write_refused:{code}")
    except Exception as e:  # noqa: BLE001 — one document's write must not stop the others
        log.warning("knowledge_source_sync: writing %s failed: %s", f["id"], e)
        return sync._wait(f, result, f"write_failed:{type(e).__name__}", type(e).__name__)
    result.changed += 1
    return "written"


async def _write(src: KnowledgeSource, f: dict[str, Any], content: str) -> None:
    tags = ["source-kind:design_doc", source_tag(src.name)]
    if repo := sync._repo_tag(src):
        tags.insert(1, repo)
    pass_context.check_stop()  # a stopped pass or a lost lease writes nothing
    await writes.handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org=src.org,
            namespace=src.namespace,
            source_uri=f["uri"],
            content=content,
            title=f["name"],
            description=convert.description_of(content),
            tags=tags,
            metadata={
                "source_kind": src.kind,
                "source_name": src.name,
                "source_revision": f["modifiedTime"],
                "ingested_from": f["id"],
                "ingested_format": "markdown",
            },
            source_class="imported",
        )
    )


async def _retire_absent(
    src: KnowledgeSource,
    key: str,
    listed: dict[str, str],
    run: sync._Pass,
    result: sync.TurnResult,
) -> None:
    """Retire the source's documents a complete list lacks, or that sit at an address
    that is no longer their document's. A document missing from the list is looked
    up first: a list paged while documents changed can miss one."""
    docs = await store.source_revisions(src.org, src.namespace, src.name, searchable_only=False)
    projects = set(src.config.get("project_ids") or [])
    for doc in docs:
        address = listed.get(doc["file_id"])
        if address == doc["source_uri"]:
            continue
        if address is None and not linear.valid_id(doc["file_id"]):
            continue  # no id the tracker could be asked about: no evidence, no retirement
        if run.spent():
            result.stop_at_bound()
            return
        run.left -= 1
        if address is None:
            gone = await _document_gone(key, doc["file_id"], projects, result)
            if gone is None:
                return
            if not gone:
                continue
        await sync._require_current(src)
        if await sync.retire(src, doc["source_uri"]):
            result.removed += 1


async def _document_gone(
    key: str, document_id: str, projects: set[str], result: sync.TurnResult
) -> bool | None:
    """Whether the tracker says the document is gone: not found, trashed, archived,
    or in none of the source's projects. A lookup that fails retires nothing; a rate
    limit or a refused key is the turn's, and None stops the retiring."""
    try:
        state = await linear.document_state(key, document_id)
    except linear.NotFound:
        return True
    except linear.RateLimited:
        result.fail("rate_limited", "rate_limited")
        return None
    except linear.CredentialRefused as e:
        result.fail("credential_invalid", sync._error(e))
        return None
    except linear.LinearError:
        return False
    project = state.get("project")
    project_id = project.get("id") if isinstance(project, dict) else None
    return bool(state.get("trashed") or state.get("archivedAt")) or project_id not in projects


__all__ = ["turn"]
