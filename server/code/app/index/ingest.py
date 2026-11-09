"""Per-file ingest orchestration shared by the code_ingest worker and the
backfill CLI — parse, chunk, contextual-embed, extract symbols/edges,
tombstone what disappeared, and report what happened.

Content-addressed memoization: chunk_id includes the content hash, so an
unchanged chunk re-ingests to the SAME id — it is simply kept (never
re-embedded, never re-inserted). Only genuinely new content costs an
embedding call. Embedding failures degrade to dim=0 stub rows the
code_embed_recovery worker retries; a file is never lost to a transient
provider error.

All CH I/O goes through CodeRepo; this module owns sequencing only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from server.code.app.index.denylist import Denylist, default_denylist
from server.code.app.index.describe import _describe_new_chunks
from server.code.app.index.ingest_chunks import (
    _callee_outline,
    _chunks_for,
    _moved_chunk_spans,
    chunk_context_texts,
)
from server.code.app.index.ingest_headers import forget_header
from server.code.app.index.ingest_parse import (
    _facets_ride_symbols,
    _statement_records_on,
    parse_for_ingest,
)
from server.code.app.index.ingest_reconcile import (
    _maybe_write_call_guards,
    _reconcile_edges_and_misses,
    _reconcile_facets,
    _reconcile_symbols,
)
from server.code.app.index.ingest_stats import (
    SKIPPED,
    EmbedResult,
    FileIngestStats,
    error_text,
)
from server.code.domain.extract.chunker import Chunk
from server.code.domain.extract.context_text import module_of
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.extractor_stamp import STAGES
from server.code.domain.extract.lang_reference import (
    REFERENCE_LANG,
    UNEMBEDDED_BY_DESIGN,
    reference_head,
)
from server.code.domain.extract.lang_sql_model import SQL_LANG
from server.code.domain.extract.treesitter import ParsedFile, lang_for_path
from server.code.store.index import CodeRepo, build_row
from server.llm import customer

if TYPE_CHECKING:
    from server.code.app.index.gitrepo import BlobTooLarge

logger = logging.getLogger("loom.code_index.ingest")

_EMBED_BATCH = 100


async def _embed_texts(texts: list[str], embedder: Any) -> EmbedResult:
    """Batch embed with per-text fallback so one poison text can't fail the
    batch (embedding_worker convention)."""
    result = EmbedResult()
    embed_many = getattr(embedder, "embed_many", None)
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start : start + _EMBED_BATCH]
        vecs: list[list[float]] | None = None
        if embed_many is not None:
            try:
                got = await embed_many(batch)
                if len(got) == len(batch):
                    vecs = got
                else:
                    logger.warning(
                        "code ingest: embed_many length mismatch (%d != %d), falling back per-text",
                        len(got),
                        len(batch),
                    )
            except customer.HOLDS:
                raise  # the org's model calls are held: the repository waits
            except Exception as e:  # noqa: BLE001 — provider errors degrade to stubs
                logger.warning("code ingest: embed_many failed (%s), per-text fallback", e)
        if vecs is not None:
            for i, v in enumerate(vecs):
                result.vectors[start + i] = v
            continue
        from server.code.app.index.embed_text import embed_shrinking

        for i, text in enumerate(batch):
            try:
                result.vectors[start + i] = await embed_shrinking(embedder, text)
            except customer.HOLDS:
                raise
            except Exception as e:  # noqa: BLE001 — a failed chunk becomes a stub, not a failed ingest
                result.failures[start + i] = str(e)[:300]
    return result


def _declared_not_searched(reason: str, stats: FileIngestStats) -> bool:
    """A generated source file is declared, not searched — its types, functions and
    edges enter the index (the `.pb.go` message types and `_gen.go` columns everything
    else calls), its text does not. True when the entry's generated rule refused the
    file; the stats keep the reason so the counters still say why it has no text."""
    if not reason.startswith("excluded: generated"):
        return False
    stats.excluded = reason
    stats.declared_only = True
    return True


async def ingest_file(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    path: str,
    source: bytes,
    commit_sha: str,
    embedding_model: str,
    embedder: Any = None,
    llm: Any = None,
    max_chunk_chars: int = 6000,
    describe: bool = False,
    embed_reuse_from: str = "",
    edges_only: bool = False,
    denylist: Denylist | None = None,
    context_callees: str = "off",
    callers_namespace: str = "",
    callee_stoplist: frozenset[str] = frozenset(),
    stages: frozenset[str] | None = None,
    live_content_hash: str = "",
    parsed: tuple[ParsedFile | None, str] | None = None,
) -> FileIngestStats:
    """Ingest one file at one commit: chunks (+embeddings), treesitter symbols,
    name-resolved edges; tombstone rows that disappeared. `parsed` is the `(ParsedFile,
    error)` a round's `parse_for_round` already produced for this source, so a batched
    file is parsed once.

    `stages` is a heal's scope: the stages whose extractor stamp moved since the
    namespace last converged (`extractor_stamp.STAGES`). None runs every stage — the
    commit path, the reconcile of a changed file. With a scope, a block whose stage is
    NOT in it is skipped, but only when `live_content_hash` (the
    `code_files.content_hash` the caller read) equals this source's hash: the skipped
    stage's rows were produced from the same bytes by the same extractor, so they are
    the rows a full ingest would key-diff and keep. A file whose content differs from
    the recorded hash runs every stage regardless (`heal_scope`). A scoped call that
    finds nothing to write in any stage writes no file row either and reports
    `identical=True`.

    `context_callees` (default "off") adds the
    callee outline to a function chunk's context text and lexemes: "calls" names what
    the unit calls, "calls_callers" also who calls it, read from `callers_namespace`
    (default: this namespace) BEFORE this file's own edges are reconciled — the outline
    is a retrieval aid derived from the index as it stands, never a reason to re-embed a
    kept row (caller churn would re-embed the corpus). `callee_stoplist` is a df cap the
    caller computed over its corpus.

    `edges_only` is the dependents path: the file's CONTENT did not change this commit,
    only the name table it resolves against, so chunks, symbols and the file row are
    left alone (no reads, no embedding, no description) and just edges + declined sites
    are re-derived and key-diffed. A file whose parse degrades in this mode writes
    nothing: a parser fault must not retire edges the last full ingest derived."""
    stats = FileIngestStats(path=path, edges_only=edges_only)
    refused, declared_only = await _refused(
        repo,
        org,
        ns,
        path=path,
        source=source,
        commit_sha=commit_sha,
        embedding_model=embedding_model,
        denylist=denylist,
        stats=stats,
    )
    if refused:
        return stats

    pf, stats.error = (
        parsed if parsed is not None else await asyncio.to_thread(parse_for_ingest, path, source)
    )
    if pf is None:
        stats.skipped = True
        return stats
    if edges_only:
        if stats.error:
            stats.skipped = True
            return stats
        await _reconcile_edges_and_misses(repo, org, ns, pf, path, commit_sha, stats)
        return stats

    scope = heal_scope(stages, source, live_content_hash)
    if scope is not None:
        stats.stages_run = tuple(s for s in _STAGES if s in scope)
    # A reference fixture has no symbol semantics and no edges: the symbol and edge
    # blocks would read their live rows to write nothing, so they are skipped outright.
    is_reference = pf.lang == REFERENCE_LANG
    # A SQL test has statement symbols and statement → object edges, but no call guards
    # to derive.
    is_sql = pf.lang == SQL_LANG and _statement_records_on()
    run_chunk = scope is None or "chunk" in scope
    run_symbol = (scope is None or "symbol" in scope) and not is_reference
    run_edge = (scope is None or "edge" in scope) and not is_reference
    run_guard = (scope is None or "guard" in scope) and not (is_reference or is_sql)

    # chunking can parse again (a host file's shell, a TLA+ spec): off the event loop
    try:  # A declared-only file has no chunks (the reconcile retires any)
        chunks, structural_prefixes, facets = (
            ([], {}, [])
            if declared_only
            else await asyncio.to_thread(
                _chunks_for, pf, path, source, max_chunk_chars, (org, ns), stats.error
            )
        )
    except Exception as chunk_exc:  # noqa: BLE001 — one bad file must not sink the commit
        # E.g. a RecursionError on a 10K-line generated table: the file row
        # records WHY it has no chunks and the rest of the commit indexes.
        # Existing chunks/symbols for the path are left live until a
        # successful re-ingest replaces them.
        logger.warning("code ingest: chunking failed for %s: %s", path, chunk_exc)
        stats.error = error_text("chunk", chunk_exc)
        await _write_error_row(
            repo, org, ns, path=path, pf=pf, source=source, commit_sha=commit_sha, error=stats.error
        )
        return stats

    # ---- chunks ----------------------------------------------------------
    wrote_chunks = run_chunk and await _chunk_stage(
        repo,
        org,
        ns,
        pf,
        path=path,
        source=source,
        commit_sha=commit_sha,
        embedding_model=embedding_model,
        chunks=chunks,
        structural_prefixes=structural_prefixes,
        stats=stats,
        embedder=embedder,
        llm=llm,
        describe=describe,
        embed_reuse_from=embed_reuse_from,
        context_callees=context_callees,
        callers_namespace=callers_namespace,
        callee_stoplist=callee_stoplist,
        is_reference=is_reference,
    )

    # ---- symbols ----------------------------------------------------------
    wrote_symbols = await _reconcile_symbols(
        repo,
        org,
        ns,
        pf,
        path,
        commit_sha,
        stats,
        run_symbol=run_symbol,
    )

    wrote_edges = await _run_edge_and_guard_stages(
        repo,
        org,
        ns,
        pf,
        path,
        commit_sha,
        stats,
        run_edge=run_edge,
        run_guard=run_guard,
    )
    if run_symbol and _facets_ride_symbols(pf, path, is_sql, facets):
        # Facets ride the symbol stage — same inputs (the parse), same heal scope,
        # key-diffed like the symbols
        wrote_symbols = await _reconcile_facets(repo, org, ns, path, facets, commit_sha) or (
            wrote_symbols
        )

    # ---- file row ----------------------------------------------------------
    # A scoped call that changed nothing leaves the file row alone too: its
    # `indexed_commit` still names a commit the file has not changed since,
    # which is all the reconcile reads from it.
    if scope is not None and not (wrote_chunks or wrote_symbols or wrote_edges):
        stats.identical = True
        return stats
    await repo.insert_rows(
        "code_files",
        [
            _file_row(
                org,
                ns,
                path=path,
                pf=pf,
                source=source,
                commit_sha=commit_sha,
                chunk_count=len(chunks),
                error=stats.error,
            )
        ],
    )
    return stats


async def _refused(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    path: str,
    source: bytes,
    commit_sha: str,
    embedding_model: str,
    denylist: Denylist | None,
    stats: FileIngestStats,
) -> tuple[bool, bool]:
    """(refused, declared_only) before the parse: the denylist's refusal tombstones the
    path; an oversized file records its reason in the file row and indexes nothing else
    (a dependents re-extraction just skips it). A generated file indexed for its
    declarations only is not refused."""
    from server.settings import settings as _settings

    # Denylist first: an excluded path indexes nothing, its existing rows tombstoned
    rules = denylist if denylist is not None else default_denylist()
    reason = rules.refusal(path, source)  # path layers, binary, generated
    declared_only = _declared_not_searched(reason, stats)
    if reason and not declared_only:
        stats.skipped = True
        stats.excluded = reason
        await tombstone_file(repo, org, ns, path=path, embedding_model=embedding_model)
        return True, declared_only

    cap = int(_settings.code_ingest_max_file_bytes)
    if cap and len(source) > cap and stats.edges_only:
        stats.skipped = True
        return True, declared_only
    if cap and len(source) > cap:
        # A multi-megabyte JSON test fixture is not code to chunk: its one chunk
        # would be an embedding input the provider rejects, retried by the
        # recovery worker. Record the file with the reason instead, so
        # code.coverage / code.index_errors show it, and index nothing else.
        stats.error = f"{SKIPPED}: {len(source)} bytes > code_ingest_max_file_bytes ({cap})"
        await _write_error_row(
            repo,
            org,
            ns,
            path=path,
            pf=None,
            source=source,
            commit_sha=commit_sha,
            error=stats.error,
        )
        return True, declared_only
    return False, declared_only


async def record_oversize(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    path: str,
    too_large: BlobTooLarge,
    commit_sha: str,
    embedding_model: str,
    denylist: Denylist | None = None,
) -> FileIngestStats:
    """`ingest_file`'s verdict on a file the mirror holds above
    `code_ingest_max_file_bytes`, from its size and its first bytes: the repository
    chose the size, so the rest is not read. A file the denylist refuses, by its path
    or by those bytes (binary, generated), is tombstoned as `ingest_file` does; any
    other file row records the reason and its blob id, so the reconcile does not ask
    again until the bytes change."""
    from server.settings import settings as _settings

    stats = FileIngestStats(path=path)
    rules = denylist if denylist is not None else default_denylist()
    reason = rules.refusal(path, too_large.head)  # its content rules probe the head only
    if reason and not _declared_not_searched(reason, stats):
        stats.skipped = True
        stats.excluded = reason
        await tombstone_file(repo, org, ns, path=path, embedding_model=embedding_model)
        return stats
    cap = int(_settings.code_ingest_max_file_bytes)
    stats.error = f"{SKIPPED}: {too_large.size} bytes > code_ingest_max_file_bytes ({cap})"
    row = _file_row(
        org,
        ns,
        path=path,
        pf=None,
        source=b"",
        commit_sha=commit_sha,
        chunk_count=0,
        error=stats.error,
    )
    # The file row of the unread bytes: no content hash, the blob id git reported.
    row.update(content_hash="", blob_sha=too_large.blob, size_bytes=too_large.size, loc=0)
    await repo.insert_rows("code_files", [row])
    return stats


async def read_capped(
    show_file: Callable[..., Awaitable[bytes | None]],
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    git_dir: str,
    path: str,
    commit_sha: str,
    embedding_model: str,
    denylist: Denylist | None,
) -> tuple[bytes | None, FileIngestStats | Literal[False] | None]:
    """`path`'s bytes at `commit_sha`, read under `code_ingest_max_file_bytes`, None
    when the commit holds no file there. A larger file is not read: `record_oversize`
    records its verdict and the bytes come back None. The second item is that record's
    stats, False when it failed (logged), None for a file within the cap."""
    from server.code.app.index.gitrepo import BlobTooLarge
    from server.settings import settings as _settings

    cap = int(_settings.code_ingest_max_file_bytes)
    try:
        return await show_file(git_dir, commit_sha, path, max_bytes=cap), None
    except BlobTooLarge as e:
        try:
            return None, await record_oversize(
                repo,
                org,
                ns,
                path=path,
                too_large=e,
                commit_sha=commit_sha,
                embedding_model=embedding_model,
                denylist=denylist,
            )
        except Exception as err:  # noqa: BLE001 — per-file, keep going
            logger.warning("%s: recording oversize %s failed: %s", git_dir, path, err)
            return None, False


async def _write_error_row(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    path: str,
    pf: ParsedFile | None,
    source: bytes,
    commit_sha: str,
    error: str,
) -> None:
    """The file row of a file that indexes no chunk, with the reason."""
    await repo.insert_rows(
        "code_files",
        [
            _file_row(
                org,
                ns,
                path=path,
                pf=pf,
                source=source,
                commit_sha=commit_sha,
                chunk_count=0,
                error=error,
            )
        ],
    )


async def _chunk_stage(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    *,
    path: str,
    source: bytes,
    commit_sha: str,
    embedding_model: str,
    chunks: list[Any],
    structural_prefixes: dict[int, str],
    stats: FileIngestStats,
    embedder: Any,
    llm: Any,
    describe: bool,
    embed_reuse_from: str,
    context_callees: str,
    callers_namespace: str,
    callee_stoplist: frozenset[str],
    is_reference: bool,
) -> bool:
    """The chunk stage of `ingest_file`: a content-addressed reconcile of the file's
    chunks against its live rows, the new ones described and embedded. True when it
    wrote a row."""

    # The reconcile compares ids and spans, so it reads only those; the full rows — vector included,
    # a mirrored version must carry every column — are fetched afterwards for just the chunks it
    # rewrites.
    existing = await repo.live_chunk_index_for_path(org, ns, path, embedding_model)
    existing_by_id = {str(r["chunk_id"]): r for r in existing}
    new_ids = {c.chunk_id(ns) for c in chunks}

    to_insert = [c for c in chunks if c.chunk_id(ns) not in existing_by_id]
    moved_spans = _moved_chunk_spans(chunks, existing_by_id, ns)
    gone_ids = [cid for cid in existing_by_id if cid not in new_ids]
    full_rows: dict[str, dict[str, Any]] = {}
    if moved_spans or gone_ids:
        full_rows = {
            str(r["chunk_id"]): r
            for r in await repo.chunks_full_by_ids(
                org, ns, path, embedding_model, [*moved_spans, *gone_ids]
            )
        }
    moved_rows: list[dict[str, Any]] = [
        CodeRepo.moved_of(
            "code_chunks",
            full_rows[cid],
            start_line=span[0],
            end_line=span[1],
            kind=span[2],
            indexed_commit=commit_sha,
        )
        for cid, span in moved_spans.items()
        if cid in full_rows
    ]
    stats.chunks_moved = len(moved_rows)
    stats.chunks_kept = len(chunks) - len(to_insert) - len(moved_rows)

    tombstones = [
        CodeRepo.tombstone_of("code_chunks", full_rows[cid]) for cid in gone_ids if cid in full_rows
    ]
    stats.chunks_tombstoned = len(tombstones)

    llm_descriptions = await _describe_new_chunks(
        llm if describe else None,
        to_insert,
        stats,
        card_source=(repo, org, ns, embedding_model, path),
    )
    llm_described: set[int] = set(llm_descriptions)
    outline_mode = context_callees or "off"
    calls_by_idx, callers_by_idx = await _callee_outline(
        repo,
        org,
        callers_namespace or ns,
        pf,
        to_insert,
        mode=outline_mode,
        stoplist=callee_stoplist,
    )
    descriptions, context_texts = chunk_context_texts(
        ns,
        path,
        pf,
        chunks,
        to_insert,
        structural_prefixes,
        llm_descriptions,
        calls_by_idx,
        callers_by_idx,
    )
    if is_reference:
        # No embedding input at all: the row is found by name, by grep
        # and by a point read, never by a vector. The description carries
        # the truncation flag; `embed_last_error` carries the marker the
        # embed-recovery read excludes, so the row is not a stub to fill.
        context_texts = ["" for _ in to_insert]
        _head, truncated = reference_head(source.decode("utf-8", "replace"))
        descriptions = {i: truncated for i in range(len(to_insert)) if truncated}

    embed_res = EmbedResult()
    if embedder is not None and context_texts and not is_reference:
        embed_res, stats.embed_reused = await _embed_new_chunks(
            repo,
            org,
            ns,
            path=path,
            embedding_model=embedding_model,
            embedder=embedder,
            embed_reuse_from=embed_reuse_from,
            to_insert=to_insert,
            context_texts=context_texts,
        )

    chunk_rows = []
    for i, c in enumerate(to_insert):
        vec = embed_res.vectors.get(i, [])
        err = UNEMBEDDED_BY_DESIGN if is_reference else embed_res.failures.get(i, "")
        chunk_rows.append(
            build_row(
                "code_chunks",
                org=org,
                namespace=ns,
                embedding_model=embedding_model,
                path=path,
                chunk_id=c.chunk_id(ns),
                scope=c.scope,
                kind=c.kind,
                start_line=c.start_line,
                end_line=c.end_line,
                content=c.content,
                content_hash=c.content_hash,
                context_text=context_texts[i],
                context_description=descriptions.get(i, ""),
                context_source="llm" if i in llm_described and not is_reference else "structural",
                lexemes=c.lexemes,
                symbol_ids=list(c.symbol_ids),
                embedding=vec,
                embedding_dim=len(vec),
                embed_attempts=1 if err and not is_reference else 0,
                embed_last_error=err,
                indexed_commit=commit_sha,
            )
        )
    stats.chunks_new = len(chunk_rows)
    stats.embedded = len(embed_res.vectors)
    stats.embed_stubs = (
        0
        if is_reference
        else len(embed_res.failures)
        + (
            len(to_insert) - len(embed_res.vectors) - len(embed_res.failures)
            if embedder is None
            else 0
        )
    )

    await repo.insert_rows("code_chunks", chunk_rows + moved_rows + tombstones)
    return bool(chunk_rows or moved_rows or tombstones)


_STAGES: tuple[str, ...] = STAGES


async def _run_edge_and_guard_stages(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    commit_sha: str,
    stats: FileIngestStats,
    *,
    run_edge: bool,
    run_guard: bool,
) -> bool:
    """The edge and guard blocks of `ingest_file`; True when either wrote a row. The
    guard stage derives its records from the edges the edge stage just resolved when
    that stage ran. A heal scoped to `guard` alone — `call_guards.py` moved, or a
    namespace whose stage keys predate the stage — reads the path's LIVE edges instead:
    they are the rows an edge stage on unchanged content would key-diff and keep, so the
    guard heal costs one keyed read per path, not a name resolution."""
    edges: list[Edge] | None = None
    wrote = False
    if run_edge:
        edges = await _reconcile_edges_and_misses(
            repo,
            org,
            ns,
            pf,
            path,
            commit_sha,
            stats,
            write_guards=False,
        )
        wrote = bool(
            stats.edges_new
            or stats.edges_tombstoned
            or stats.misses_new
            or stats.misses_refreshed
            or stats.misses_tombstoned
        )
    if run_guard:
        if edges is None:
            edges = await _live_edges_as_input(repo, org, ns, path)
        await _maybe_write_call_guards(repo, org, ns, pf, path, commit_sha, edges, stats)
        wrote = wrote or bool(stats.guards_new or stats.guards_tombstoned)
    return wrote


async def _live_edges_as_input(repo: CodeRepo, org: str, ns: str, path: str) -> list[Edge]:
    """The path's live parser-tier edges as the extractor's `Edge` records — the guard
    stage's input when the edge stage did not run."""
    rows = await repo.live_edges_for_path(org, ns, path, "treesitter")
    return [
        Edge(
            edge_kind=str(r.get("edge_kind") or ""),
            from_symbol_id=str(r.get("from_symbol_id") or ""),
            to_symbol_id=str(r.get("to_symbol_id") or ""),
            from_qualified_name=str(r.get("from_qualified_name") or ""),
            to_qualified_name=str(r.get("to_qualified_name") or ""),
            path=str(r.get("path") or path),
            line=int(r.get("line") or 0),
            confidence=float(r.get("confidence") or 0.0),
        )
        for r in rows
    ]


async def _embed_new_chunks(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    path: str,
    embedding_model: str,
    embedder: Any,
    embed_reuse_from: str,
    to_insert: list[Chunk],
    context_texts: list[str],
) -> tuple[EmbedResult, int]:
    """Vectors for the chunks a file ingest is about to insert: copied from
    `embed_reuse_from` where that namespace holds the same content under the
    same context text, embedded fresh (clipped, per-text fallback) for the
    rest. Returns the result and how many vectors were reused."""
    embed_res = EmbedResult()
    reused: dict[int, list[float]] = {}
    if embed_reuse_from and embed_reuse_from != ns:
        from server.code.app.index.embed_reuse import reusable_vectors

        reused = await reusable_vectors(
            repo,
            org,
            embed_reuse_from,
            embedding_model=embedding_model,
            path=path,
            content_hashes=[c.content_hash for c in to_insert],
            context_texts=context_texts,
        )
    todo = [i for i in range(len(context_texts)) if i not in reused]
    if todo:
        from server.code.app.index.embed_text import clip_for_embedding

        counter = getattr(embedder, "_count_tokens", None)
        clipped = {i: clip_for_embedding(context_texts[i], count_tokens=counter) for i in todo}
        # The round embedded these exact texts already
        hints = getattr(repo, "embed_hints", None) or {}
        for i in todo:
            vec = hints.get(clipped[i])
            if vec is not None:
                embed_res.vectors[i] = list(vec)
        todo = [i for i in todo if i not in embed_res.vectors]
        fresh = await _embed_texts([clipped[i] for i in todo], embedder)
        for j, i in enumerate(todo):
            if j in fresh.vectors:
                embed_res.vectors[i] = fresh.vectors[j]
            if j in fresh.failures:
                embed_res.failures[i] = fresh.failures[j]
    embed_res.vectors.update(reused)
    return embed_res, len(reused)


def heal_scope(
    stages: frozenset[str] | None, source: bytes, live_content_hash: str
) -> frozenset[str] | None:
    """The stages a scoped ingest may confine itself to, or None for all.

    Pure. A scope is honoured only when the caller proved the content did not
    change: `live_content_hash` is the `code_files.content_hash` of the live
    row and it equals sha256(source). An empty recorded hash (file not yet
    indexed, or the caller did not look) is not proof, so every stage runs.
    An empty scope is treated as no scope: a heal that names no stage is a
    heal, not a no-op."""
    if stages is None:
        return None
    wanted = frozenset(s for s in stages if s in _STAGES)
    if not wanted:
        return None
    if not live_content_hash:
        return None
    if hashlib.sha256(source).hexdigest() != str(live_content_hash).lower():
        return None
    return wanted


def _file_row(
    org: str,
    ns: str,
    *,
    path: str,
    pf: ParsedFile | None,
    source: bytes,
    commit_sha: str,
    chunk_count: int,
    error: str,
) -> dict[str, Any]:
    """The `code_files` row — `index_error` is what `code.index_errors`,
    `code.coverage.files_errored` and `loom.code.files_errored` read.
    `pf` is None for a file that was never parsed (size cap)."""
    from server.code.app.index.gitrepo import blob_sha

    text = source.decode("utf-8", "replace")
    return build_row(
        "code_files",
        org=org,
        namespace=ns,
        path=path,
        lang=pf.lang if pf else lang_for_path(path),
        module=module_of(path),
        content_hash=hashlib.sha256(source).hexdigest(),
        blob_sha=blob_sha(source),
        size_bytes=len(source),
        loc=text.count("\n") + 1,
        chunk_count=chunk_count,
        symbol_count=len(pf.symbols) if pf else 0,
        index_error=error,
        indexed_commit=commit_sha,
    )


async def tombstone_file(
    repo: CodeRepo, org: str, ns: str, *, path: str, embedding_model: str
) -> FileIngestStats:
    """A file deleted at HEAD: tombstone its chunks, treesitter symbols, edges, declined
    sites and the file row. Returns the names and ids it retired, so the commit's
    dependents set can re-resolve the files that called into this one."""
    stats = FileIngestStats(path=path)
    forget_header(org, ns, path)
    chunks = await repo.live_chunks_for_path(org, ns, path, embedding_model, with_embedding=True)
    await repo.insert_rows("code_chunks", [CodeRepo.tombstone_of("code_chunks", r) for r in chunks])
    stats.chunks_tombstoned = len(chunks)
    syms = await repo.live_symbols_for_path(org, ns, path, "treesitter")
    await repo.insert_rows("code_symbols", [CodeRepo.tombstone_of("code_symbols", r) for r in syms])
    stats.symbols_tombstoned = len(syms)
    for r in syms:
        stats.symbol_names_removed.append(
            str(r.get("name") or str(r.get("qualified_name") or "").split("::")[-1])
        )
        stats.symbols_tombstoned_ids.append(str(r.get("symbol_id") or ""))
    edges = await repo.live_edges_for_path(org, ns, path, "treesitter")
    await repo.insert_rows("code_edges", [CodeRepo.tombstone_of("code_edges", r) for r in edges])
    stats.edges_tombstoned = len(edges)
    # Caller-guard rows of a deleted file go with its edges (a namespace the writer
    # never ran on has none; one keyed probe)
    try:
        guards = await repo.live_call_guards_for_path(org, ns, path)
        if guards:
            await repo.insert_rows(
                "code_caller_guards",
                [CodeRepo.tombstone_of("code_caller_guards", r) for r in guards],
            )
            stats.guards_tombstoned = len(guards)
    except Exception as guard_exc:  # noqa: BLE001 — a disclosure table must never fail a delete
        logger.warning("code ingest: call-guard tombstones failed for %s: %s", path, guard_exc)
    try:
        misses = await repo.live_call_misses_for_path(org, ns, path)
        await repo.insert_rows(
            "code_call_sites_unresolved",
            [CodeRepo.tombstone_of("code_call_sites_unresolved", r) for r in misses],
        )
        stats.misses_tombstoned = len(misses)
    except Exception as miss_exc:  # noqa: BLE001 — a disclosure table must never fail a delete
        logger.warning("code ingest: declined-call tombstones failed for %s: %s", path, miss_exc)
    files = await repo.live_files_for_path(org, ns, path)
    await repo.insert_rows("code_files", [CodeRepo.tombstone_of("code_files", r) for r in files])
    return stats
