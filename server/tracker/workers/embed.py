"""The `tracker_embed` pass: every thirty minutes (the leg's
query-time catch-up covers the gap on the call), for EVERY configured tracker namespace
(`configured_namespaces()` — the source map; a namespace added to it is
embedded on the next pass, and at boot when it holds no row — never a list of
its own), read the issues and
PRs of the embedding window — open, or closed within
`LOOM_TRACKER_EMBED_CLOSED_DAYS` — that changed since the last pass
(an (updated_at, source_id) keyset cursor per kind in `loom.code_index_state`),
embed `title + "\\n" + left(body, LOOM_TRACKER_EMBED_BODY_CHARS)` with loom's own
embedder (the memory rows' model, batched), and write
`loom.qa_item_embeddings` under (org, namespace); a row whose text digest is
already stored is skipped, a row whose item left the window is tombstoned.

Reads each namespace through ITS OWN source (`tracker_source(org, ns)`: the namespace's
credential, database, repositories and privacy predicate), so a private row lands only in
the restricted namespace's rows and never under a standard one; one keys read and one paged
items read per kind. The table is a derived index — keys, filter facts and vectors, no title
or body text; the leg hydrates its hits from the upstream item read. A namespace whose
upstream `embedding` column is populated is a no-op (one log line): the canonical
alternative — QA filling its own column — makes this pass redundant and the leg reads
upstream first. The cursor advances after every page, so a pass the supervisor cuts at its
deadline resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now_seconds as _now
from server.llm import customer
from server.settings import settings
from server.tracker.app.embed_index import (
    KINDS,
    build_rows,
    cursor_state,
    embed_many,
    embed_text,
    text_sha256,
    window_days,
)
from server.tracker.store.embed import StoredEmbedding, build_embed_row
from server.workers.first_pass import OwnedTable
from server.workers.pass_context import note_failure, set_namespace
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.tracker_embed")

PAGE = 2000
# ~4 characters a token for English + code (the estimate the pass logs; the
# provider's `usage` is what is billed and what the embedding span records)
CHARS_PER_TOKEN = 4.0

# The table this pass fills; a configured namespace it holds
# no row for makes the first pass due at boot instead of after the stagger.
OWNED_TABLES = (OwnedTable("loom.qa_item_embeddings", namespace_column="namespace"),)


def due_scope() -> list[tuple[str, str]]:
    """(org, namespace) of every configured tracker namespace, restricted ones
    included — each is embedded from its own source into its own rows."""
    from server.tracker.domain.namespaces import configured_namespaces

    return sorted((cfg.org, ns) for ns, cfg in configured_namespaces().items())


# namespaces whose upstream column turned out populated: said once per process
_UPSTREAM_LOGGED: set[str] = set()


@dataclass
class TrackerEmbedResult:
    namespaces: int = 0
    skipped_upstream: int = 0
    items_seen: int = 0
    rows_embedded: int = 0
    rows_skipped: int = 0
    rows_tombstoned: int = 0
    # window items with no live row, read by number this pass
    rows_missing: int = 0
    # kinds whose window-keys read hit its cap: nothing tombstoned for them
    tombstones_skipped: list[str] = field(default_factory=list)
    tokens_estimate: int = 0
    statements: int = 0
    wall_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def outcome(self) -> dict[str, Any]:
        return {
            "items_planned": self.items_seen,
            "items_done": self.rows_embedded + self.rows_skipped,
            "rows_written": self.rows_embedded + self.rows_tombstoned,
            "namespaces": self.namespaces,
            "skipped_upstream": self.skipped_upstream,
            "rows_embedded": self.rows_embedded,
            "rows_skipped": self.rows_skipped,
            "rows_tombstoned": self.rows_tombstoned,
            "rows_missing": self.rows_missing,
            "tombstones_skipped": len(self.tombstones_skipped),
            "tokens_estimate": self.tokens_estimate,
            "statements": self.statements,
            "wall_ms": self.wall_ms,
            "errors": len(self.errors),
        }


async def run_pass() -> TrackerEmbedResult:
    result = TrackerEmbedResult()
    with record_pass("tracker_embed") as rec:
        await _run(result)
        log_pass_errors(logger, "tracker_embed", result.errors)
        rec.outcome = result.outcome()
    logger.info(
        "tracker_embed: %d namespace(s): embedded %d, skipped %d (unchanged), tombstoned %d, "
        "~%d tokens, %d statements, %d ms",
        result.namespaces,
        result.rows_embedded,
        result.rows_skipped,
        result.rows_tombstoned,
        result.tokens_estimate,
        result.statements,
        result.wall_ms,
    )
    return result


async def _run(
    result: TrackerEmbedResult,
    *,
    now: datetime | None = None,
    repo: Any = None,
    source_factory: Any = None,
    embedder: Any = None,
    state: Any = None,
) -> None:
    from server.tracker.app.source import tracker_source
    from server.tracker.domain.errors import TrackerUnavailable
    from server.tracker.domain.namespaces import configured_namespaces
    from server.tracker.store.embed import TrackerEmbedRepo

    started = time.monotonic()
    repo = repo or TrackerEmbedRepo()
    state = state or cursor_state()
    make = source_factory or tracker_source
    emb = embedder or _embedder()
    for ns, cfg in sorted(configured_namespaces().items()):
        if await customer.switched_off(cfg.org):
            continue  # its cursor stays put: the pages wait until the org is back on
        # every configured namespace, restricted included: its own source, its
        # own predicate, its own rows (the transport's gate decides who reads them)
        set_namespace(ns)
        src = make(cfg.org, ns)
        try:
            if await src.upstream_embeddings_populated(cfg.org, ns):
                # the canonical alternative landed: QA fills its own column and
                # the leg reads it first; the two never both run
                result.skipped_upstream += 1
                if ns not in _UPSTREAM_LOGGED:
                    _UPSTREAM_LOGGED.add(ns)
                    logger.info(
                        "tracker_embed: %s: the upstream embedding column is populated; "
                        "the pass is a no-op for it",
                        ns,
                    )
                continue
            await embed_namespace(
                src,
                repo,
                emb,
                cfg.org,
                ns,
                result=result,
                now=now or _now(),
                state=state,
            )
            result.namespaces += 1
        except TrackerUnavailable as e:
            result.errors.append(f"{ns}: tracker_unavailable: {e}")
            note_failure(e, f"tracker_embed:{ns}")
        except customer.HOLDS:
            continue  # a held call is no failure: its cursor stays where the last page left it
        except Exception as e:
            result.errors.append(f"{ns}: {type(e).__name__}: {e}")
            note_failure(e, f"tracker_embed:{ns}")
            logger.exception("tracker_embed: namespace %s failed", ns)
    result.wall_ms = int((time.monotonic() - started) * 1000)


def _embedder() -> Any:
    from server.embedding.embedder import get_embedder

    return get_embedder()


async def embed_namespace(
    src: Any,
    repo: Any,
    emb: Any,
    org: str,
    ns: str,
    *,
    result: TrackerEmbedResult,
    now: datetime,
    state: Any,
) -> None:
    """One namespace's pass: the stored digests once, then per kind the
    window's keys (for the tombstones) and the changed items a page at a
    time, embedded in batches and written; the cursor advances per page."""
    body_chars = int(settings.tracker_embed_body_chars)
    batch = max(1, int(settings.tracker_embed_batch))
    stored = await repo.stored(org, ns)
    result.statements += 1
    stamp = now.isoformat()
    for kind in KINDS:
        closed_days = window_days(kind)
        keys = await src.embed_window_keys(org, ns, kind=kind, closed_days=closed_days, stamp=stamp)
        result.statements += 1
        window = {_number_of(k["source_id"]) for k in keys}
        seen = await _embed_kind(
            src,
            repo,
            emb,
            org,
            ns,
            kind,
            stored,
            result=result,
            now=now,
            state=state,
            closed_days=closed_days,
            body_chars=body_chars,
            batch=batch,
            stamp=stamp,
        )
        await _embed_missing(
            src,
            repo,
            emb,
            org,
            ns,
            kind,
            stored,
            window - seen,
            result=result,
            now=now,
            body_chars=body_chars,
            batch=batch,
            stamp=stamp,
        )
        from server.tracker.store.source.embed import WINDOW_KEYS_LIMIT

        if len(keys) >= WINDOW_KEYS_LIMIT:
            # the read may have stopped short: an item past the cap would read
            # as gone and its live row be tombstoned. Skip; the next pass retries.
            result.tombstones_skipped.append(f"{ns}:{kind}")
            logger.warning(
                "tracker_embed: %s/%s %s window read hit its cap (%d); not tombstoning",
                org,
                ns,
                kind,
                WINDOW_KEYS_LIMIT,
            )
            continue
        await _tombstone_left(repo, org, ns, kind, stored, window, result=result, now=now)


def _number_of(source_id: str) -> int:
    from server.tracker.domain.namespaces import split_source_id

    return split_source_id(source_id)[1]


async def _embed_kind(
    src: Any,
    repo: Any,
    emb: Any,
    org: str,
    ns: str,
    kind: str,
    stored: dict[tuple[str, int], StoredEmbedding],
    *,
    result: TrackerEmbedResult,
    now: datetime,
    state: Any,
    closed_days: int,
    body_chars: int,
    batch: int,
    stamp: str,
) -> set[int]:
    """The cursor walk; returns the numbers it saw (embedded or skipped), so
    the missing-items read never repeats them."""
    cursor, cursor_id = await state.get_cursor(org, ns, kind)
    pending: list[dict[str, Any]] = []
    seen: set[int] = set()
    while True:
        page = await src.embed_items(
            org,
            ns,
            kind=kind,
            since=cursor,
            since_id=cursor_id,
            closed_days=closed_days,
            body_chars=body_chars,
            limit=PAGE,
            stamp=stamp,
        )
        result.statements += 1
        if not page:
            break
        for it in page:
            result.items_seen += 1
            seen.add(int(it["number"]))
            text = embed_text(it["title"], it["body"], body_chars=body_chars)
            sha = text_sha256(text)
            prev = stored.get((kind, int(it["number"])))
            if prev is not None and _current(prev, emb.model_name) and prev["text_sha256"] == sha:
                result.rows_skipped += 1
                continue
            pending.append({**it, "text": text, "sha": sha})
            if len(pending) >= batch:
                await _flush(repo, emb, org, ns, pending, result=result, now=now)
                pending = []
        await _flush(repo, emb, org, ns, pending, result=result, now=now)
        pending = []
        # the page is ordered by (updated_at, source_id): its last row is the keyset
        # the next page resumes after, so rows sharing one second never straddle a skip
        last = page[-1]
        if last.get("updated_at") is None:
            break  # no keyset to resume after: re-reading would loop on this page
        cursor, cursor_id = last["updated_at"], str(last["source_id"])
        await state.set(org, ns, kind, cursor, cursor_id)
        result.statements += 1
        if len(page) < PAGE:
            break
        await asyncio.sleep(0)
    return seen


def _current(prev: StoredEmbedding, model: str) -> bool:
    """A live row embedded by the served model. After a model change every
    row is stale whatever its digest: nearest() filters on the served width,
    and a same-width model would compare two spaces."""
    return prev.get("tombstoned_at") is None and prev.get("embedding_model") == model


MISSING_PAGE = 500


async def _embed_missing(
    src: Any,
    repo: Any,
    emb: Any,
    org: str,
    ns: str,
    kind: str,
    stored: dict[tuple[str, int], StoredEmbedding],
    window: set[int],
    *,
    result: TrackerEmbedResult,
    now: datetime,
    body_chars: int,
    batch: int,
    stamp: str,
) -> None:
    """The window's items with no live row — the cursor walk
    reads `updated_at > cursor`, so an item that entered the window by a
    wider `closed_days` (last touched years ago) is never a page's row.
    Read by number, embedded through the same digest compare and flush;
    at zero missing the pass pays nothing."""
    live = {n for (k, n), v in stored.items() if k == kind and _current(v, emb.model_name)}
    missing = sorted(window - live)
    if not missing:
        return
    result.rows_missing += len(missing)
    pending: list[dict[str, Any]] = []
    for i in range(0, len(missing), MISSING_PAGE):
        page = await src.embed_items_by_number(
            org,
            ns,
            kind=kind,
            numbers=missing[i : i + MISSING_PAGE],
            body_chars=body_chars,
            stamp=stamp,
        )
        result.statements += 1
        for it in page:
            result.items_seen += 1
            text = embed_text(it["title"], it["body"], body_chars=body_chars)
            pending.append({**it, "text": text, "sha": text_sha256(text)})
            if len(pending) >= batch:
                await _flush(repo, emb, org, ns, pending, result=result, now=now)
                pending = []
        await asyncio.sleep(0)
    await _flush(repo, emb, org, ns, pending, result=result, now=now)


async def _flush(
    repo: Any,
    emb: Any,
    org: str,
    ns: str,
    pending: list[dict[str, Any]],
    *,
    result: TrackerEmbedResult,
    now: datetime,
) -> None:
    if not pending:
        return
    with customer.for_org(org, namespace=ns):
        vectors = await embed_many(emb, [p["text"] for p in pending])
    rows = build_rows(
        org,
        ns,
        pending,
        [p["sha"] for p in pending],
        vectors,
        model=str(getattr(emb, "model_name", "")),
    )
    await repo.insert_rows(rows)
    result.statements += 1
    result.rows_embedded += len(rows)
    result.tokens_estimate += int(sum(len(p["text"]) for p in pending) / CHARS_PER_TOKEN)


async def _tombstone_left(
    repo: Any,
    org: str,
    ns: str,
    kind: str,
    stored: dict[tuple[str, int], StoredEmbedding],
    window: set[int],
    *,
    result: TrackerEmbedResult,
    now: datetime,
) -> None:
    """Rows of `kind` whose item is no longer in the window: re-inserted
    with `tombstoned_at` and every other column carried forward."""
    gone = [
        n
        for (k, n), st in stored.items()
        if k == kind and st.get("tombstoned_at") is None and n not in window
    ]
    if not gone:
        return
    version = int(time.time_ns())
    for i in range(0, len(gone), 500):
        rows = await repo.rows_by_key(org, ns, kind, gone[i : i + 500])
        result.statements += 1
        out = [build_embed_row(**{**r, "tombstoned_at": now, "version": version}) for r in rows]
        await repo.insert_rows(out)
        result.statements += 1
        result.rows_tombstoned += len(out)


__all__ = [
    "KINDS",
    "OWNED_TABLES",
    "TrackerEmbedResult",
    "due_scope",
    "embed_many",
    "embed_namespace",
    "embed_text",
    "run_pass",
    "text_sha256",
]
