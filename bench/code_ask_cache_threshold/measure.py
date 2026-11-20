"""Re-measure the code.ask answer-cache threshold for the served embedder.

`server/code/app/answers_cache.py` serves a cached answer when the nearest
cached question's cosine similarity clears a per-model threshold
(`server/code/app/answers_entry.py`). This script embeds the labelled pairs in
`pairs.json` (paraphrases that deserve the same answer, different questions that do
not) with the repo's own embedder, prints both similarity distributions, and names
the lowest threshold on a 0.01 grid that keeps every different-question pair a miss.

    python bench/code_ask_cache_threshold/measure.py --env-file .env \
        --out bench/code_ask_cache_threshold/summary.json

The identifier guard (`load_bearing_tokens` in `server/code/app/answers_cache.py`: a
semantic hit is served only when both questions name the same identifiers, settings,
paths, numbers, quoted strings and error codes) is measured on 22 `different_identifier`
near-miss pairs. The summary carries two choices: `chosen` (cosine alone, every
different pair) and `chosen_with_guard` (the lowest 0.01 step with no different pair
that BOTH clears the cosine and passes the guard), each with its paraphrase hits.

`--model` overrides LOOM_EMBEDDING_MODEL (e.g. text-embedding-3-small for
summary_3small.json). `--seed-from-ch` also prints the live cached
questions (read-only: `SELECT question FROM loom.code_answers FINAL`), the
source the pairs were written from. Cost: one embedding call per distinct
question (~130), a few cents on OpenAI.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))


_ENV_KEYS = ("LOOM_EMBEDDING_", "OPENAI_", "GEMINI_", "LOOM_CH_")


def _load_env(env_file: str | None) -> None:
    """Only the embedder's and the ClickHouse client's keys: a deploy .env
    carries inline comments other settings would fail to parse."""
    if not env_file or not Path(env_file).exists():
        return
    for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if not k.strip().startswith(_ENV_KEYS):
                continue
            v = v.split(" #", 1)[0]
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _dist(values: list[float]) -> dict[str, float]:
    s = sorted(values)
    return {
        "n": len(s),
        "min": round(s[0], 4),
        "p10": round(s[len(s) // 10], 4),
        "median": round(statistics.median(s), 4),
        "p90": round(s[(9 * len(s)) // 10], 4),
        "max": round(s[-1], 4),
    }


def choose(pos: list[float], neg: list[float]) -> dict[str, Any]:
    """The lowest 0.01-grid threshold above every different-question pair,
    and what it keeps of the paraphrases."""
    t = math.floor(max(neg) * 100 + 1) / 100.0
    hits = sum(1 for p in pos if p >= t)
    return {
        "threshold": round(t, 2),
        "paraphrase_hits": hits,
        "paraphrase_total": len(pos),
        "false_hits": sum(1 for n in neg if n >= t),
        "margin_above_max_different": round(t - max(neg), 4),
        "margin_below_min_paraphrase": round(min(pos) - t, 4),
    }


def choose_guarded(pos: list[tuple[float, bool]], neg: list[tuple[float, bool]]) -> dict[str, Any]:
    """`choose` with the guard on: a pair counts only when it passes the
    guard (a guarded pair is a miss whatever its cosine)."""
    live_neg = [s for s, ok in neg if ok]
    t = math.floor(max(live_neg) * 100 + 1) / 100.0 if live_neg else 0.0
    return {
        "threshold": round(t, 2),
        "paraphrase_hits": sum(1 for s, ok in pos if ok and s >= t),
        "paraphrase_total": len(pos),
        "paraphrase_guarded": sum(1 for _, ok in pos if not ok),
        "false_hits": sum(1 for s, ok in neg if ok and s >= t),
        "different_guarded": sum(1 for _, ok in neg if not ok),
        "different_total": len(neg),
        "margin_above_max_unguarded_different": round(t - max(live_neg), 4) if live_neg else None,
    }


def _sweep(pos: list[float], neg: list[float]) -> list[dict[str, Any]]:
    out = []
    for i in range(40, 91, 5):
        t = i / 100.0
        out.append(
            {
                "threshold": t,
                "paraphrase_hits": sum(p >= t for p in pos),
                "false_hits": sum(n >= t for n in neg),
            }
        )
    return out


async def _seed_questions() -> list[str]:
    import clickhouse_connect

    c = clickhouse_connect.get_client(
        host=os.environ["LOOM_CH_HOST"],
        port=int(os.environ.get("LOOM_CH_PORT", "8443")),
        username=os.environ["LOOM_CH_USER"],
        password=os.environ["LOOM_CH_PASSWORD"],
        database=os.environ.get("LOOM_CH_DATABASE", "loom"),
        secure=os.environ.get("LOOM_CH_SECURE", "true").lower() != "false",
    )
    sql = "SELECT question FROM loom.code_answers FINAL WHERE tombstoned_at IS NULL"
    rows = c.query(sql).result_rows
    return [str(r[0]) for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--model", default="")
    ap.add_argument("--pairs", default=str(HERE / "pairs.json"))
    ap.add_argument("--out", default="")
    ap.add_argument("--seed-from-ch", action="store_true")
    args = ap.parse_args()
    _load_env(args.env_file)
    if args.model:
        os.environ["LOOM_EMBEDDING_MODEL"] = args.model
    from server.embedding.embedder import get_embedder

    if args.seed_from_ch:
        for q in await _seed_questions():
            print("seed:", q)
    raw = Path(args.pairs).read_bytes()
    data = json.loads(raw)
    emb = get_embedder()
    from server.code.app.answers_cache import guard_passes

    data.setdefault("different_identifier", [])
    kinds = ("paraphrase", "different", "different_identifier", "unlabelled_cached")
    texts = sorted({q for k in kinds for p in data[k] for q in p})
    vecs = dict(zip(texts, await asyncio.gather(*(emb.embed(t) for t in texts)), strict=True))

    def sims(kind: str) -> list[float]:
        return [_cos(vecs[a], vecs[b]) for a, b in data[kind]]

    pos, neg = sims("paraphrase"), sims("different")
    near = sims("different_identifier")
    all_neg_pairs = data["different"] + data["different_identifier"]
    guard_pos = [
        (sv, guard_passes(a, b)) for sv, (a, b) in zip(pos, data["paraphrase"], strict=True)
    ]
    guard_neg = [
        (sv, guard_passes(a, b)) for sv, (a, b) in zip(neg + near, all_neg_pairs, strict=True)
    ]
    unguarded_neg = sorted(
        ((sv, p) for (sv, ok), p in zip(guard_neg, all_neg_pairs, strict=True) if ok), reverse=True
    )[:5]
    top_neg = sorted(zip(neg, data["different"], strict=True), reverse=True)[:5]
    low_pos = sorted(zip(pos, data["paraphrase"], strict=True))[:5]
    summary = {
        "model": emb.model_name,
        "dim": emb.dim,
        # the input this summary was measured on; a pairs edit makes it stale
        "pairs_sha256": hashlib.sha256(raw).hexdigest(),
        "paraphrase": _dist(pos),
        "different": _dist(neg),
        "chosen": choose(pos, neg),
        "sweep": _sweep(pos, neg),
        "different_identifier": _dist(near) if near else {},
        "chosen_all_different": choose(pos, neg + near) if near else {},
        "chosen_with_guard": choose_guarded(guard_pos, guard_neg),
        "sweep_with_guard": [
            {
                "threshold": i / 100.0,
                "paraphrase_hits": sum(1 for sv, ok in guard_pos if ok and sv >= i / 100.0),
                "false_hits": sum(1 for sv, ok in guard_neg if ok and sv >= i / 100.0),
            }
            for i in range(40, 91, 5)
        ],
        "closest_unguarded_different": [[round(sv, 4), *p] for sv, p in unguarded_neg],
        "closest_different_identifier": [
            [round(sv, 4), *p]
            for sv, p in sorted(zip(near, data["different_identifier"], strict=True), reverse=True)[
                :5
            ]
        ],
        "closest_different": [[round(s, 4), *p] for s, p in top_neg],
        "farthest_paraphrase": [[round(s, 4), *p] for s, p in low_pos],
        "unlabelled_cached": [
            [round(_cos(vecs[a], vecs[b]), 4), a, b] for a, b in data["unlabelled_cached"]
        ],
    }
    text = json.dumps(summary, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
