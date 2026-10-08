"""Export (query -> positive unit, negatives) pairs from real agent traffic.

The prerequisite for retrieval fine-tuning (an in-domain fine-tuned small
embedder can beat a zero-shot large one on issue->edit retrieval) and for
reranker distillation. Loom already records both halves:

  code_search_trace   what was asked and what ranked (ids + paths, order)
  code_search_outcome what the agent DID (opened_file / used_in_answer /
                      fell_back_grep / ignored), joined by trace_run_id

A pair is emitted when a trace has a positive outcome (opened_file /
used_in_answer) whose target matches a returned path or unit id: the
matched unit is the positive; same-trace returned-but-not-acted-on units
become ranked hard negatives (they beat the corpus at retrieval time and
lost at act time — exactly the discrimination an embedder/reranker needs).
fell_back_grep traces export as hard-negative-only rows (the whole result
set failed the agent).

Rows are JSONL: {query, positive: {unit_id, path}, negatives: [...],
route, ts, namespace}. Queries under 3 tokens and traces without
outcomes are skipped — impression data is not supervision.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.retrieval_pairs")

_POSITIVE_ACTIONS = ("opened_file", "used_in_answer")


@dataclass
class ExportResult:
    traces_scanned: int = 0
    pairs: int = 0
    negative_only: int = 0
    skipped_no_match: int = 0
    by_route: dict[str, int] = field(default_factory=dict)


def _match_target(target: str, ids: list[str], paths: list[str]) -> int | None:
    """Index of the returned unit a target refers to (path or unit id)."""
    for i, (uid, p) in enumerate(zip(ids, paths, strict=False)):
        if target == uid or target == p or p.endswith("/" + target):
            return i
    return None


async def export_pairs(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    days: int = 90,
    out_path: str,
    min_query_tokens: int = 3,
) -> ExportResult:
    res = ExportResult()
    rows = await repo.trace_outcomes(org, ns, days=days)
    lines: list[str] = []

    def _emit(obj: dict[str, Any]) -> None:
        lines.append(json.dumps(obj))

    for r in rows:
        res.traces_scanned += 1
        query = str(r["query"]).strip()
        if len(query.split()) < min_query_tokens:
            continue
        ids = [str(x) for x in r["ids"]]
        paths = [str(x) for x in r["paths"]]
        # which leg found each unit; '' when the row has none or the
        # arrays do not align
        via = [str(x) for x in (r.get("via") or [])]
        if len(via) != len(ids):
            via = [""] * len(ids)
        units = [
            {"unit_id": u, "path": p, "via": v} for u, p, v in zip(ids, paths, via, strict=False)
        ]
        action = str(r["action"])
        if action == "fell_back_grep":
            res.negative_only += 1
            _emit(
                {
                    "query": query,
                    "positive": None,
                    "negatives": units,
                    "route": str(r["route"]),
                    "ts": str(r["ts"]),
                    "namespace": ns,
                }
            )
            continue
        if action not in _POSITIVE_ACTIONS:
            continue
        hit = None
        for target in (str(t) for t in r["targets"]):
            hit = _match_target(target, ids, paths)
            if hit is not None:
                break
        if hit is None:
            res.skipped_no_match += 1
            continue
        res.pairs += 1
        res.by_route[str(r["route"])] = res.by_route.get(str(r["route"]), 0) + 1
        _emit(
            {
                "query": query,
                "positive": units[hit],
                "negatives": [u for i, u in enumerate(units) if i != hit],
                "route": str(r["route"]),
                "ts": str(r["ts"]),
                "namespace": ns,
            }
        )
    # one blocking write, off the loop (ASYNC230)
    import asyncio

    await asyncio.to_thread(Path(out_path).write_text, "\n".join(lines) + ("\n" if lines else ""))
    logger.info(
        "retrieval pairs: %d traces -> %d pairs, %d negative-only, %d unmatched",
        res.traces_scanned,
        res.pairs,
        res.negative_only,
        res.skipped_no_match,
    )
    return res
