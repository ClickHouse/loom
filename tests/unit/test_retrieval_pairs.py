"""Retrieval-pair export: positives from acted-on outcomes, ranked hard
negatives from the same trace, grep-fallbacks as negative-only rows."""

from __future__ import annotations

import asyncio
import json

from server.code.app.index.retrieval_pairs import export_pairs
from server.code.store.index import CodeRepo


class _CH:
    def __init__(self, rows):
        self._rows = rows

    async def query_all(self, sql, params=None):
        return self._rows


def _trace(query, ids, paths, action, targets, route="hybrid"):
    return {
        "query": query,
        "route": route,
        "ts": "2026-09-08 10:00:00",
        "ids": ids,
        "paths": paths,
        "action": action,
        "targets": targets,
    }


def test_pairs_negatives_and_fallbacks(tmp_path) -> None:
    out = tmp_path / "pairs.jsonl"
    rows = [
        _trace(
            "how does the merge selector pick parts",
            ["u1", "u2", "u3"],
            ["src/A.cpp", "src/B.cpp", "src/C.cpp"],
            "opened_file",
            ["src/B.cpp"],
        ),
        _trace("who calls foo and why does it matter", ["u4"], ["src/D.cpp"], "fell_back_grep", []),
        _trace("short one", ["u5"], ["src/E.cpp"], "opened_file", ["src/E.cpp"]),
        _trace(
            "a query whose target matches nothing returned",
            ["u6"],
            ["src/F.cpp"],
            "used_in_answer",
            ["src/Z.cpp"],
        ),
    ]
    res = asyncio.run(export_pairs(CodeRepo(_CH(rows)), "o", "n", out_path=str(out)))
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert res.pairs == 1 and res.negative_only == 1 and res.skipped_no_match == 1
    pair = next(x for x in lines if x["positive"])
    assert pair["positive"]["path"] == "src/B.cpp"
    assert [n["path"] for n in pair["negatives"]] == ["src/A.cpp", "src/C.cpp"]
    neg_only = next(x for x in lines if x["positive"] is None)
    assert neg_only["negatives"][0]["path"] == "src/D.cpp"
    # "short one" skipped: 2 tokens of query is impression data, not supervision
    assert not any(x["query"] == "short one" for x in lines)
