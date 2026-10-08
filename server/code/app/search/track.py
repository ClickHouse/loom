"""The track_path diagnostic: where one file's chunks stood under the tokens,
idfs and query vector the search used."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.search.fusion import leg_provenance
from server.code.app.search.state import _POOL, _model_name
from server.code.app.spans import code_span, span_attrs
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


# track_path: a prefix resolving to more files than this is a directory,
# not a file — the caller is told so instead of paying a directory scan.
_TRACK_MAX_FILES = 8


async def _track(
    repo: CodeRepo,
    org: str,
    ns: str,
    path: str,
    embedder: Any,
    tokens: list[str],
    idfs: list[float],
    qvec: list[float] | None,
    lexical_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    *,
    served_ids: list[str],
    model: str = "",
) -> dict[str, Any]:
    """track_path diagnostic: score ONE file's chunks under the same tokens,
    idfs and query vector this search used and say where they stood. One
    read under the (org, namespace, embedding_model, path) key prefix; a
    failure reports itself instead of failing the search."""
    # the lexical expression zips tokens with idfs: when the lexical leg did
    # not run (legs="vector") there are no idfs, so no lexical side either
    if len(idfs) != len(tokens):
        tokens, idfs = [], []
    with code_span("search.track_path") as sp:
        try:
            # a path names a file; a prefix (bench gold: src/Access/QuotaCache)
            # names the few files under it — resolve on the key first
            model = model or _model_name(embedder)
            found = await repo.paths_under_prefix(org, ns, model, path, limit=_TRACK_MAX_FILES + 1)
            if path in found:
                found = [path]
            else:
                # a prefix ends at a path boundary: QuotaCache names
                # QuotaCache.cpp / QuotaCache.h / QuotaCache/…, not QuotaCacheFoo.cpp
                # (the bench's recall_at_k rule)
                stem = path.rstrip("/")
                found = [f for f in found if f.startswith(stem + "/") or f.startswith(stem + ".")]
            if len(found) > _TRACK_MAX_FILES:
                return {
                    "path": path,
                    "verdict": (
                        f"prefix matches more than {_TRACK_MAX_FILES} files; pass a file path"
                    ),
                    "chunks": 0,
                    "pool": _POOL,
                    "files": found[:_TRACK_MAX_FILES],
                }
            rows = (
                await repo.track_path_chunks(
                    org,
                    ns,
                    model,
                    found,
                    tokens,
                    idfs,
                    qvec,
                )
                if found
                else []
            )
        except Exception as e:  # noqa: BLE001 — a diagnostic never sinks a search
            logger.warning("code.search track_path read failed: %s", e)
            return {"path": path, "verdict": f"track read failed: {type(e).__name__}"}
        out = track_verdict(
            path,
            rows,
            lexical_rows,
            vector_rows,
            served_ids=served_ids,
            pool=_POOL,
            vector_ran=qvec is not None,
            lexical_ran=bool(tokens),
        )
        out["files"] = found
        span_attrs(sp, results=len(rows))
    return out


def track_verdict(
    path: str,
    rows: list[dict[str, Any]],
    lexical_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    *,
    served_ids: list[str],
    pool: int,
    vector_ran: bool,
    lexical_ran: bool,
) -> dict[str, Any]:
    """Pure: the tracked file's standing. verdict reads one of
      'not indexed'                    — no live chunks under this path
      'served at rank r'               — a chunk of the file is in the response
      'rank r in <leg> pool'           — a leg returned it; fusion/priors/top_k cut it
      'no lexeme match[; ...]'         — no query token occurs in any chunk's lexemes
      'beyond pool: ...'               — scored, but under the lexical pool floor /
                                         past the vector pool ceiling (numbers given)
    `best` is the chunk that came closest, with both leg ranks and raw scores."""
    if not rows:
        return {"path": path, "verdict": "not indexed", "chunks": 0, "pool": pool}
    prov = leg_provenance(lexical_rows, vector_rows)
    served = {cid: i for i, cid in enumerate(served_ids, start=1)}
    lex_floor = min((float(r.get("lex_score") or 0.0) for r in lexical_rows), default=0.0)
    vec_ceiling = max((float(r.get("distance") or 0.0) for r in vector_rows), default=0.0)
    lex_pool_full = len(lexical_rows) >= pool
    vec_pool_full = len(vector_rows) >= pool

    def _dist(r: dict[str, Any]) -> float | None:
        d = r.get("distance")
        if d is None or not vector_ran:
            return None
        d = float(d)
        return None if d < 0 else d

    def _rank_key(r: dict[str, Any]) -> tuple[int, int, float, float]:
        cid = str(r.get("chunk_id") or "")
        _via, lr, vr = prov.get(cid, ("", 0, 0))
        best_rank = min(x for x in (lr or 10**9, vr or 10**9))
        d = _dist(r)
        return (
            served.get(cid, 10**9),
            best_rank,
            -float(r.get("lex_score") or 0.0),
            d if d is not None else 9.0,
        )

    best_row = min(rows, key=_rank_key)
    cid = str(best_row.get("chunk_id") or "")
    _via, lr, vr = prov.get(cid, ("", 0, 0))
    lex_score = float(best_row.get("lex_score") or 0.0)
    dist = _dist(best_row)
    best = {
        "chunk_id": cid,
        "path": str(best_row.get("path") or path),
        "scope": str(best_row.get("scope") or ""),
        "start_line": int(best_row.get("start_line") or 0),
        "lexical_rank": lr,
        "vector_rank": vr,
        "lex_score": round(lex_score, 3),
        "distance": round(dist, 4) if dist is not None else None,
        "served_rank": served.get(cid, 0),
    }
    if cid in served:
        verdict = f"served at rank {served[cid]}"
    elif lr or vr:
        leg, r = ("lexical", lr) if lr and (not vr or lr <= vr) else ("vector", vr)
        verdict = f"rank {r} in {leg} pool"
    else:
        parts: list[str] = []
        if lexical_ran:
            if lex_score <= 0.0:
                parts.append("no lexeme match")
            elif lex_pool_full:
                parts.append(f"beyond pool: lexical {lex_score:.2f} under floor {lex_floor:.2f}")
            else:
                parts.append(f"lexical {lex_score:.2f} but not returned by the leg")
        if vector_ran:
            if dist is None:
                parts.append("not embedded")
            elif vec_pool_full:
                parts.append(f"beyond pool at distance {dist:.3f} (ceiling {vec_ceiling:.3f})")
            else:
                parts.append(f"distance {dist:.3f} but not returned by the leg")
        verdict = "; ".join(parts) or "not scored (no leg ran)"
    return {
        "path": path,
        "verdict": verdict,
        "chunks": len(rows),
        "pool": pool,
        "best": best,
        "lexical_floor": round(lex_floor, 3),
        "vector_ceiling": round(vec_ceiling, 4),
    }
