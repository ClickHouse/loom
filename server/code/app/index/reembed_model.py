"""Write a namespace's chunk rows under a second embedding model's label — an
in-database re-embed, no checkout.

The served model's live rows already hold `context_text`, the text the provider
embedded. A second model embeds that same text and its row copies every other column, so
the two labels describe identical chunks (`chunk_id` is derived from namespace, path,
scope and content hash — the same for both). The rows are inserted through
`CodeRepo.insert_rows`, which routes a mapped label to its own table
(`LOOM_CODE_VECTOR_TABLES`), because the coded vector column is one width per table.

Resumable: a `(path, chunk_id)` already live under the target label is
skipped, so a stopped run continues where it was. Reads walk the served
label's key range in path order, one prefix at a time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from server.code.app.index.embed_text import clip_for_embedding
from server.code.store.index import CodeRepo, build_row

logger = logging.getLogger(__name__)

_COPY_COLS = (
    "path",
    "chunk_id",
    "scope",
    "kind",
    "start_line",
    "end_line",
    "content",
    "content_hash",
    "context_text",
    "context_description",
    "context_source",
    "lexemes",
    "symbol_ids",
    "indexed_commit",
)


@dataclass
class ReembedStats:
    paths_seen: int = 0
    rows_written: int = 0
    rows_skipped: int = 0
    embed_seconds: float = 0.0
    insert_seconds: float = 0.0


async def _source_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    source_model: str,
    *,
    path_prefix: str,
    after_path: str,
    limit: int,
    only_path: str = "",
) -> list[dict[str, Any]]:
    """The served label's live rows after `after_path`, path order, every
    column the second label copies. Plain WHERE on the key prefix.
    `only_path` reads that one path whole instead (no LIMIT)."""
    return await repo.chunk_page_for_copy(
        org,
        ns,
        source_model,
        _COPY_COLS,
        path_prefix=path_prefix,
        after_path=after_path,
        limit=limit,
        only_path=only_path,
    )


def _target_row(r: dict[str, Any], vec: Any, *, org: str, ns: str, target: str) -> dict[str, Any]:
    """One source row copied under the `target` label with its new vector;
    FixedString columns come back as bytes and go in as text."""
    cols = {k: r[k] for k in _COPY_COLS}
    cols["chunk_id"] = str(cols["chunk_id"])
    for key in ("content_hash", "indexed_commit"):
        v = cols.get(key)
        if isinstance(v, bytes):
            cols[key] = v.decode("utf-8", "replace").rstrip("\x00")
    return build_row(
        "code_chunks",
        org=org,
        namespace=ns,
        embedding_model=target,
        embedding=list(vec),
        embedding_dim=len(vec),
        **cols,
    )


async def reembed_namespace(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    source_model: str,
    embedder: Any,
    path_prefix: str = "",
    batch_rows: int = 2000,
    progress: Any = None,
) -> ReembedStats:
    """Every live chunk of `source_model` under `path_prefix`, written under
    `embedder.model_name` with that model's vector. Whole paths per batch:
    the cursor is the last complete path, so a resumed run never splits
    one file across two passes."""
    stats = ReembedStats()
    target = str(getattr(embedder, "model_name", "") or "")
    if not target or target == source_model:
        raise ValueError(f"target label {target!r} must differ from the source label")
    after = ""
    while True:
        rows = await _source_rows(
            repo,
            org,
            ns,
            source_model,
            path_prefix=path_prefix,
            after_path=after,
            limit=batch_rows,
        )
        if not rows:
            break
        last_path = str(rows[-1]["path"])
        if len(rows) == batch_rows:
            # drop the (possibly partial) last path; it leads the next batch
            rows = [r for r in rows if str(r["path"]) != last_path]
            if not rows:  # one path wider than the batch: take it whole, however wide
                rows = await _source_rows(
                    repo,
                    org,
                    ns,
                    source_model,
                    path_prefix=path_prefix,
                    after_path=after,
                    limit=batch_rows,
                    only_path=last_path,
                )
                if not rows:  # retired between the two reads
                    after = last_path
                    continue
            after = str(rows[-1]["path"])
        else:
            after = last_path
        paths = sorted({str(r["path"]) for r in rows})
        stats.paths_seen += len(paths)
        have = await repo.vector_ids_for_paths(org, ns, target, paths)
        todo = [r for r in rows if (str(r["path"]), str(r["chunk_id"])) not in have]
        stats.rows_skipped += len(rows) - len(todo)
        if todo:
            t0 = time.perf_counter()
            texts = [clip_for_embedding(str(r["context_text"]) or str(r["content"])) for r in todo]
            embed_many = getattr(embedder, "embed_many", None)
            if embed_many is not None:
                vecs = await embed_many(texts)
            else:
                vecs = [await embedder.embed(t) for t in texts]
            stats.embed_seconds += time.perf_counter() - t0
            out = [
                _target_row(r, vec, org=org, ns=ns, target=target)
                for r, vec in zip(todo, vecs, strict=True)
            ]
            t1 = time.perf_counter()
            await repo.insert_rows("code_chunks", out)
            stats.insert_seconds += time.perf_counter() - t1
            stats.rows_written += len(out)
        if progress is not None:
            progress(stats, after)
    return stats
