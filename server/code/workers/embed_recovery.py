"""Embedding recovery for the code plane (mirror of the memory plane's
embedding worker).

The ingest path embeds inline and degrades to `embedding_dim = 0` stub rows
on provider failure; retrieval filters `embedding_dim > 0`, so stubs are
invisible until this loop fills them. Per-text fallback on batch failure so
one poison text can't fail its neighbours; rows that exhaust
`code_embed_max_attempts` are poisoned (`embed_failed_at` set) and left to
the lexical leg.

The pass polls the work, not the tables. One key scan per
table per pass covers every configured repo, keyed above a version
watermark (the newest stub version the previous scan returned) so the stub
versions of rows already replaced are not read again; the full rows are
read under FINAL by exact key only for the keys the scan returned; the
replacement rows go out in one insert per table per pass. A pass that
found nothing marks itself idle (`pass_context.mark_idle`), and the
supervisor sleeps `code_embed_recovery_idle_interval_seconds`, instead of a
per-repo scan and a per-repo FINAL read every 30 s whatever the backlog.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.core.datetimes import naive_utc_now
from server.llm import customer
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass, set_worker_identity

logger = logging.getLogger("loom.workers.code_embed_recovery")


def _batch() -> int:
    """`LOOM_CODE_EMBED_RECOVERY_BATCH` — keys per configured
    repo per scan and texts per embed call."""
    return int(settings.code_embed_recovery_batch)


# Keys per FINAL read: the bound for a keyed batch read.
_KEYS_PER_READ = 500
_TABLES: tuple[tuple[str, Callable[[dict[str, Any]], str]], ...] = (
    ("code_chunks", lambda r: str(r.get("context_text") or r.get("content") or "")),
    ("code_summaries", lambda r: str(r.get("summary") or "")),
)

# The watermark per table — the newest `version` the key scan
# returned — and the slow clock that owes a scan from version 0. Module
# state, like the embedding worker's orphan clock: one
# watermark per process, wherever the supervisor runs the pass.
_watermark: dict[str, int] = {}
_full_scan_at: float = 0.0


def _full_scan_due(*, now: float | None = None) -> bool:
    """Whether this pass scans from version 0: the first pass after boot,
    once per `code_embed_recovery_full_scan_seconds`, or always when that
    clock is 0. Marks the run when it says yes."""
    global _full_scan_at
    every = int(settings.code_embed_recovery_full_scan_seconds)
    if every <= 0:
        return True
    t = time.monotonic() if now is None else now
    if _full_scan_at <= 0.0 or t - _full_scan_at >= every:
        _full_scan_at = t
        return True
    return False


@dataclass
class CodeEmbedRecoveryResult:
    chunks_embedded: int = 0
    summaries_embedded: int = 0
    failed: int = 0
    poisoned: int = 0
    rows_written: int = 0
    scans: int = 0
    keys_seen: int = 0
    full_scan: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        """No stub version above the watermark in either table. A pass that
        read keys and wrote nothing (stale versions of replaced rows) is not
        idle: there may be more behind them, and the busy cadence stays."""
        return self.keys_seen == 0


async def run_pass() -> CodeEmbedRecoveryResult:
    result = CodeEmbedRecoveryResult()
    with record_pass("code_embed_recovery") as rec:
        await _run(result)
        if result.idle:
            pass_context.mark_idle()
        rec.outcome = {
            "chunks_embedded": result.chunks_embedded,
            "summaries_embedded": result.summaries_embedded,
            "failed": result.failed,
            "poisoned": result.poisoned,
            "rows_written": result.rows_written,
            "scans": result.scans,
            "keys_seen": result.keys_seen,
            "full_scan": result.full_scan,
            "idle": result.idle,
        }
    return result


async def _run(result: CodeEmbedRecoveryResult) -> None:
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo
    from server.embedding.embedder import get_embedder

    repo = CodeRepo()
    embedder = get_embedder()
    max_attempts = settings.code_embed_max_attempts
    repos = [(cfg.org, cfg.namespace) for cfg in configured_repos()]
    result.full_scan = _full_scan_due()
    for table, text_of in _TABLES:
        embedded = await _recover_table(
            table,
            repos,
            repo,
            embedder,
            max_attempts,
            result,
            text_of=text_of,
            from_zero=result.full_scan,
        )
        if table == "code_chunks":
            result.chunks_embedded += embedded
        else:
            result.summaries_embedded += embedded


# Scans a pass may chain while the pages hold only
# stale stub versions. A heal or a re-chunk leaves the superseded stub
# version of every row in the table (ReplacingMergeTree), and the flat scan
# returns those first; with one scan a pass, a pass would embed nothing while
# the backlog of stale versions ahead of the live stubs drained at one page
# per pass (stub versions can outnumber live stubs several-fold).
# The chain is bounded by wall time: dozens of chains × a keyed FINAL read per
# page on `code_summaries` would overrun the pass deadline, the supervisor
# would cancel the pass and the fetched rows would be lost.
# A page's keys pass through `live_stub_keys` (keyed argMax) so superseded
# versions never reach the FINAL row read, and the flat candidate scan stays
# cheap (a namespace-wide aggregate would read hundreds of MB a scan).
_MAX_SCANS_PER_TABLE = 8
_CHAIN_BUDGET_S = 20.0


async def _recover_table(
    table: str,
    repos: list[tuple[str, str]],
    repo: Any,
    embedder: Any,
    max_attempts: int,
    result: CodeEmbedRecoveryResult,
    *,
    text_of: Callable[[dict[str, Any]], str],
    from_zero: bool,
) -> int:
    """One key scan for the table (`_batch()` keys per configured repo), the
    full rows in pages of `_KEYS_PER_READ` keys, one embed call per namespace
    and `_batch()` texts, one insert. A scan per round would re-read every
    repo's flag columns to find the next page while a backlog drains; one
    scan a pass reads them once — unless the page held only
    stale stub versions (rows FINAL says are already embedded): then the
    scan chains above its watermark, up to `_MAX_SCANS_PER_TABLE` a pass,
    until a page yields live stubs or the backlog ends."""
    floor = 0 if from_zero else int(_watermark.get(table, 0))
    limit = _batch() * max(1, len(repos))
    rows: list[dict[str, Any]] = []
    chain_started = time.monotonic()
    for scan_no in range(_MAX_SCANS_PER_TABLE):
        if scan_no and time.monotonic() - chain_started > _CHAIN_BUDGET_S:
            break  # embed what the chain found; the watermark carries the rest over
        keys = await repo.pending_embed_keys(
            table,
            repos,
            embedder.model_name,
            max_attempts,
            limit,
            min_version=floor,
        )
        result.scans += 1
        if not keys:
            break
        result.keys_seen += len(keys)
        # Every stub version at or below the newest returned has now been
        # returned once (ORDER BY version), so the next scan starts above it.
        floor = max(int(k["version"]) for k in keys)
        _watermark[table] = floor
        # Drop the page's superseded versions with one keyed
        # aggregate before the FINAL row read pays for them.
        live: list[dict[str, Any]] = []
        for i in range(0, len(keys), _KEYS_PER_READ):
            live.extend(
                await repo.live_stub_keys(
                    table,
                    embedder.model_name,
                    keys[i : i + _KEYS_PER_READ],
                    max_attempts,
                )
            )
        for i in range(0, len(live), _KEYS_PER_READ):
            rows.extend(
                await repo.pending_embed_rows_by_keys(
                    table, embedder.model_name, live[i : i + _KEYS_PER_READ], max_attempts
                )
            )
        if rows or len(keys) < limit:
            break  # live work found, or the backlog is exhausted
    if not rows:
        return 0
    return await _embed_rows(
        repo,
        table,
        rows,
        embedder,
        text_of=text_of,
        max_attempts=max_attempts,
        result=result,
    )


# The failure recorded for a row with no text: providers reject an empty
# input, and there is nothing to retry until the row's content changes.
EMPTY_TEXT = "empty text: nothing to embed"


async def _vectors_for(
    embedder: Any, texts: list[str]
) -> tuple[dict[int, list[float]], dict[int, str]]:
    """`embed_many` for the batch's non-empty texts, per-text on its failure
    so one poison text cannot fail its neighbours; `(vectors, failures)` by
    index. An empty text is never sent: one would fail the whole batch."""
    failures: dict[int, str] = {i: EMPTY_TEXT for i, t in enumerate(texts) if not t.strip()}
    todo = [i for i in range(len(texts)) if i not in failures]
    if not todo:
        return {}, failures
    embed_many = getattr(embedder, "embed_many", None)
    if embed_many is not None:
        try:
            got = await embed_many([texts[i] for i in todo])
            if len(got) == len(todo):
                return dict(zip(todo, got, strict=True)), failures
        except Exception as e:  # noqa: BLE001 — fall back per-text
            logger.warning("code_embed_recovery: batch failed (%s), per-text", e)
    from server.code.app.index.embed_text import embed_shrinking

    vectors: dict[int, list[float]] = {}
    for i in todo:
        try:
            vectors[i] = await embed_shrinking(embedder, texts[i])
        except Exception as e:  # noqa: BLE001 — one failing chunk must not fail its group
            failures[i] = str(e)[:300]
    return vectors, failures


async def _embed_rows(
    repo: Any,
    table: str,
    rows: list[dict[str, Any]],
    embedder: Any,
    *,
    text_of: Callable[[dict[str, Any]], str],
    max_attempts: int,
    result: CodeEmbedRecoveryResult,
) -> int:
    """Embed the pass's rows per `(org, namespace)` — the provider spans and
    spend stay attributed to the namespace — `_batch()` texts an embed call,
    and write every replacement row of the table in one insert."""
    if not rows:
        return 0
    from server.code.app.index.embed_text import clip_for_embedding

    counter = getattr(embedder, "_count_tokens", None)
    replacements: list[dict[str, Any]] = []
    embedded = 0
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(r)
    for (org, ns), whole in groups.items():
        set_worker_identity("code_embed_recovery", org=org, namespace=ns)
        pass_context.set_namespace(ns)
        batch = _batch()
        for start in range(0, len(whole), batch):
            group = whole[start : start + batch]
            texts = [clip_for_embedding(text_of(r), count_tokens=counter) for r in group]
            with customer.for_org(org):
                vectors: dict[int, list[float]]
                if not customer.names_one():
                    vectors, failures = {}, dict.fromkeys(range(len(group)), customer.NO_CUSTOMER)
                else:
                    vectors, failures = await _vectors_for(embedder, texts)
            embedded += _stage_replacements(
                repo, table, group, vectors, failures, max_attempts, result, replacements
            )
    await repo.insert_rows(table, replacements)
    result.rows_written += len(replacements)
    return embedded


def _stage_replacements(
    repo: Any,
    table: str,
    group: list[dict[str, Any]],
    vectors: dict[int, list[float]],
    failures: dict[int, str],
    max_attempts: int,
    result: CodeEmbedRecoveryResult,
    replacements: list[dict[str, Any]],
) -> int:
    """The replacement row per live stub of one embed call: the vector on
    success, the attempt count (and the poison stamp at the cap) otherwise."""
    embedded = 0
    for i, live in enumerate(group):
        # Mirror EVERY column onto the replacement row (RMT invariant),
        # then override the embed-state fields.
        row = repo.tombstone_of(table, live)  # full mirror + fresh version
        row["tombstoned_at"] = None
        if i in vectors:
            row["embedding"] = vectors[i]
            row["embedding_dim"] = len(vectors[i])
            row["embed_last_error"] = ""
            row["embed_failed_at"] = None
            embedded += 1
        else:
            attempts = int(live.get("embed_attempts") or 0) + 1
            row["embed_attempts"] = attempts
            row["embed_last_error"] = failures.get(i, "")
            result.failed += 1
            if attempts >= max_attempts or failures.get(i) in (EMPTY_TEXT, customer.NO_CUSTOMER):
                row["embed_failed_at"] = naive_utc_now()
                result.poisoned += 1
        replacements.append(row)
    return embedded
