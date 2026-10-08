"""The vector leg: the query embedding (one per embedder per search), the
served scan or a second model's shortlist rescored on the served vectors, and
which embedder the leg reads with."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.app.search.state import _POOL, _model_name
from server.code.app.spans import code_span, span_attrs
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


_DEFAULT_VECTOR_MODEL_WARNED: set[str] = set()


def _warn_default_vector_model_once(spec: str) -> None:
    """A misconfigured served default degrades to the full scan and says so once per
    process, not once per search."""
    if spec not in _DEFAULT_VECTOR_MODEL_WARNED:
        _DEFAULT_VECTOR_MODEL_WARNED.add(spec)
        logger.warning(
            "code.search: LOOM_CODE_SEARCH_VECTOR_MODEL_DEFAULT=%r is not usable "
            "(not allow-listed or not the served model's prefix); vector leg falls "
            "back to the full scan",
            spec,
        )


async def _embed_timed(embedder: Any, text: str) -> list[float]:
    """The query embedding under the stage wall's `embed` span — a cache hit or a provider round
    trip alike, so the `vector` stage is the statement alone. The embedder's own OTel span
    (embed.compute.*) is unchanged."""
    with code_span("search.embed"):
        vec: list[float] = await embedder.embed(text)
    return vec


class _QueryVecMemo:
    """One query embedding per embedder per search, shared by the legs that run beside
    each other: the first awaiter pays the provider round trip (counted once), the
    others await the same future. A prefix twin's vector is the served one cut down, no
    second provider call."""

    def __init__(self, text: str, work: dict[str, Any]) -> None:
        self._text, self._work = text, work
        self._futures: dict[int, asyncio.Future[list[float]]] = {}

    async def __call__(self, emb: Any) -> list[float]:
        base = getattr(emb, "base", None)
        if base is not None and hasattr(emb, "project"):
            return list(emb.project(await self(base)))
        fut = self._futures.get(id(emb))
        if fut is None:
            self._work["embedding_calls"] += 1
            fut = asyncio.ensure_future(_embed_timed(emb, self._text))
            self._futures[id(emb)] = fut
        # shielded: a leg dropped by the deadline must not cancel the embedding the
        # other legs are awaiting
        return await asyncio.shield(fut)

    def release(self) -> None:
        """Cancel an embedding no leg is still waiting on, and mark a failed one's
        error retrieved. Called once the legs are over."""
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()


async def _vector_leg_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    embedder: Any,
    vec_embedder: Any,
    run_vector: bool,
    shortlist_n: int,
    path_prefix: str,
    scope: SearchScope | None,
    query_vec: Any,
    work: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[float] | None]:
    """The vector leg: the served scan, or a shortlist on the second model's or the
    twin's vectors rescored exactly on the served ones; an empty shortlist falls back to
    the scan."""
    rows: list[dict[str, Any]] = []
    qvec: list[float] | None = None
    if vec_embedder is None or not run_vector:
        return rows, qvec
    # the embedding call is the stage wall's `embed`
    with code_span("search.vector_leg") as sp:
        try:
            qvec = await query_vec(vec_embedder)
            work["vector_queries"] += 1
            if shortlist_n > 0 and vec_embedder is not embedder:
                # The second model shortlists, the served model's vectors rank the
                # shortlist exactly.
                short = await repo.vector_shortlist_ids(
                    org,
                    ns,
                    _model_name(vec_embedder),
                    qvec,
                    n=shortlist_n,
                    path_prefix=path_prefix,
                    scope=scope,
                )
                qvec = await query_vec(embedder)
                work["vector_queries"] += 1
                work["vector_shortlist"] = len(short)
                if short:
                    rows = await repo.exact_rerank_by_ids(
                        org,
                        ns,
                        model,
                        qvec,
                        [(str(r["path"]), str(r["chunk_id"])) for r in short],
                        pool=_POOL,
                    )
                else:
                    # an unfilled twin: the full scan answers as it did before the
                    # default existed
                    work["vector_shortlist_fallback"] = 1
                    rows = await repo.vector_search_chunks(
                        org,
                        ns,
                        model,
                        qvec,
                        path_prefix=path_prefix,
                        pool=_POOL,
                        scope=scope,
                    )
            else:
                rows = await repo.vector_search_chunks(
                    org,
                    ns,
                    # the served table's label: the request's embedding_model override
                    # unless a second model is asked for
                    model if vec_embedder is embedder else _model_name(vec_embedder),
                    qvec,
                    path_prefix=path_prefix,
                    pool=_POOL,
                    scope=scope,
                )
                if vec_embedder is not embedder:
                    # a second model's query vector cannot score the
                    # served model's rows (track_path reads those)
                    qvec = None
        except Exception as e:  # noqa: BLE001 — lexical-only degrade
            logger.warning("code.search vector leg failed: %s", e)
            work["vector_leg_failed"] = 1
        span_attrs(sp, results=len(rows))
    return rows, qvec


def _resolve_vector_arm(
    vector_model: str,
    vector_shortlist: int | None,
    embedder: Any,
    scope: SearchScope | None,
) -> tuple[Any, int, str]:
    """The vector leg's embedder and shortlist depth: a request-named second model, else
    the deployment's prefix twin as the default for an unconstrained search (a
    constrained scope's predicates read content, which the twin does not hold; a
    misconfigured default warns once and degrades to the served leg), else the served
    embedder. Returns (embedder, shortlist_n, default label or "")."""
    vec_embedder: Any = embedder
    default_vm = ""
    if not vector_model and embedder is not None and (scope is None or not scope.constrained):
        default_vm = str(settings.code_search_vector_model_default).strip()
        if default_vm and int(settings.code_search_vector_shortlist) <= 0:
            default_vm = ""
    if vector_model or default_vm:
        from server.embedding.embedder import get_secondary_embedder

        try:
            vec_embedder = get_secondary_embedder(vector_model or default_vm)
        except ValueError:
            if vector_model:
                raise
            _warn_default_vector_model_once(default_vm)
            default_vm = ""
    shortlist_n = 0
    if vector_model or default_vm:
        shortlist_n = int(
            settings.code_search_vector_shortlist if vector_shortlist is None else vector_shortlist
        )
        if embedder is None:
            shortlist_n = 0  # nothing to rescore with
    return vec_embedder, shortlist_n, default_vm
