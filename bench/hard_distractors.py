"""Hard distractors: does the ranking, and the answer, hold when the corpus
carries near-copies of the gold that differ in exactly the fact that matters?

A citation contract can be scored on a set built as 1 gold passage + 19
*designed* distractors, with a cite score of +1 for citing the gold and −1
for every other citation. Loom's ask and recall benches score against corpora whose
noise is whatever the namespace happened to hold; a ranking that clears that bar can
still fold under a distractor that shares every token with the gold but one number.

This bench manufactures that noise and measures the fold. For every question
in a `retrieval_recall.py`-shaped dataset it reads the gold memory, derives
`--per-gold` near-copies by deterministic mutation — a number moved, a date
shifted, a capitalised entity swapped for one from another gold, a polarity
word flipped — keeps only those whose normalised Levenshtein distance to the
gold is at most `--max-dist` (and at least `--min-dist`; never zero), writes them
under keys that share no substring with the gold, and re-measures:

  recall@k / MRR        gold rank before and after the distractors landed
  intrusion@k           share of the top-k the distractors took
  cite_score (--ask)    ±1 over the answer's [key] markers, before/after
  gold_cited (--ask)    did the answer cite the gold at all

Distractors are deleted at the end unless `--keep`. Nothing here touches the
server: it is a client of memory.set / memory.get / memory.search /
memory.ask / memory.delete, so the numbers are what an agent would see.

Run:
    LOOM_API_KEY=... python bench/hard_distractors.py \
        --dataset /path/to/retrieval_qa.json \
        --base-url http://localhost:7777 --per-gold 5 --top-k 10 --ask \
        --out /tmp/hard_distractors.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from bench.plane_ablation import load_questions
    from bench.retrieval_recall import score_one
except ImportError:  # run as a script from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bench.plane_ablation import load_questions
    from bench.retrieval_recall import score_one

KEY_PREFIX = "dstr_"
TAG = "hard_distractor"

_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w.])")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_CAP_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]{2,})\b")
_CITE_RE = re.compile(r"\[([^\[\]]{1,200})\]")
_POLARITY: tuple[tuple[str, str], ...] = (
    ("is not", "is"),
    ("is", "is not"),
    ("always", "never"),
    ("never", "always"),
    ("before", "after"),
    ("after", "before"),
    ("enabled", "disabled"),
    ("disabled", "enabled"),
    ("increase", "decrease"),
    ("decrease", "increase"),
    ("true", "false"),
    ("false", "true"),
    ("passed", "failed"),
    ("failed", "passed"),
    ("prefers", "avoids"),
    ("likes", "dislikes"),
    ("yes", "no"),
)


# ---- mutation --------------------------------------------------------------


def _shift_number(tok: str, rng: random.Random) -> str:
    raw = tok.replace(",", "")
    if "." in raw:
        whole, frac = raw.split(".", 1)
        n = int(whole or "0")
        step = max(1, abs(n) // 10) if n else 1
        n2 = n + rng.choice((-1, 1)) * rng.randint(1, step)
        return f"{n2}.{frac}"
    n = int(raw)
    if 1900 <= n <= 2100:  # a year: a small shift keeps it a year
        n2 = n + rng.choice((-3, -2, -1, 1, 2, 3))
    else:
        step = max(1, abs(n) // 5) if n else 1
        n2 = n + rng.choice((-1, 1)) * rng.randint(1, step)
        if n2 == n:
            n2 = n + 1
    return f"{n2:,}" if "," in tok else str(n2)


def mutate_numbers(text: str, rng: random.Random) -> str | None:
    """Move one number that is not part of an ISO date (dates have their own
    mutation and their zero-padding must survive)."""
    dates = [(m.start(), m.end()) for m in _DATE_RE.finditer(text)]
    hits = [m for m in _NUM_RE.finditer(text) if not any(s <= m.start() < e for s, e in dates)]
    if not hits:
        return None
    m = rng.choice(hits)
    tok = m.group(1)
    new = _shift_number(tok, rng)
    if tok.startswith("0") and len(tok) > 1 and new.isdigit():
        new = new.zfill(len(tok))
    return text[: m.start()] + new + text[m.end() :]


def mutate_dates(text: str, rng: random.Random) -> str | None:
    hits = list(_DATE_RE.finditer(text))
    if not hits:
        return None
    m = rng.choice(hits)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    d2 = d + rng.choice((-7, -3, -1, 1, 3, 7))
    if not 1 <= d2 <= 28:
        mo = mo + (1 if d2 > 28 else -1)
        d2 = ((d2 - 1) % 28) + 1
        if not 1 <= mo <= 12:
            y += 1 if mo > 12 else -1
            mo = ((mo - 1) % 12) + 1
    return text[: m.start()] + f"{y:04d}-{mo:02d}-{d2:02d}" + text[m.end() :]


def mutate_entities(text: str, pool: list[str], rng: random.Random) -> str | None:
    hits = [m for m in _CAP_RE.finditer(text)]
    if not hits or not pool:
        return None
    m = rng.choice(hits)
    choices = [p for p in pool if p != m.group(1) and p.lower() not in text.lower()]
    if not choices:
        return None
    return text[: m.start()] + rng.choice(choices) + text[m.end() :]


def mutate_polarity(text: str, rng: random.Random) -> str | None:
    lowered = text.lower()
    options = [(a, b) for a, b in _POLARITY if re.search(rf"\b{re.escape(a)}\b", lowered)]
    if not options:
        return None
    a, b = rng.choice(options)
    return re.sub(rf"\b{re.escape(a)}\b", b, text, count=1, flags=re.IGNORECASE)


def entity_pool(texts: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for t in texts:
        for m in _CAP_RE.finditer(t):
            seen.setdefault(m.group(1), None)
    return list(seen)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalised_distance(a: str, b: str) -> float:
    denom = max(len(a), len(b))
    return levenshtein(a, b) / denom if denom else 0.0


def make_distractors(
    gold: str,
    *,
    per_gold: int,
    pool: list[str],
    seed: int = 0,
    min_dist: float = 0.0,
    max_dist: float = 0.35,
    max_chars: int = 600,
) -> list[str]:
    """Up to `per_gold` near-copies of `gold`, each a different sentence that
    keeps most of the gold's bytes. Deterministic for (gold, pool, seed)."""
    gold = gold[:max_chars]
    rng = random.Random(f"{seed}:{gold}")
    out: list[str] = []
    seen = {gold}
    kinds = ("numbers", "dates", "entities", "polarity")
    for attempt in range(per_gold * 6):
        kind = kinds[attempt % len(kinds)]
        base = out[-1] if (attempt >= len(kinds) and rng.random() < 0.3 and out) else gold
        if kind == "numbers":
            cand = mutate_numbers(base, rng)
        elif kind == "dates":
            cand = mutate_dates(base, rng)
        elif kind == "entities":
            cand = mutate_entities(base, pool, rng)
        else:
            cand = mutate_polarity(base, rng)
        if not cand or cand in seen:
            continue
        d = normalised_distance(gold, cand)
        if d <= 0.0 or d < min_dist or d > max_dist:
            continue
        seen.add(cand)
        out.append(cand)
        if len(out) >= per_gold:
            break
    return out


def distractor_key(gold_key: str, i: int) -> str:
    """A key that shares no substring with the gold key: recall scoring matches
    gold loosely (`gold in key`), so the gold text must not appear in it."""
    return f"{KEY_PREFIX}{hashlib.sha256(gold_key.encode()).hexdigest()[:10]}_{i:02d}"


def value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k in ("text", "content", "value", "summary", "statement"):
            if isinstance(value.get(k), str):
                return value[k]
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---- scoring ----------------------------------------------------------------


def cited_keys(answer: str, results: list[dict[str, Any]]) -> list[str]:
    marked = {
        m.group(1).strip().strip("`").lstrip("#").strip() for m in _CITE_RE.finditer(answer or "")
    }
    out: list[str] = []
    for h in results:
        k = str(h.get("memory_key") or "")
        if k and k in marked and k not in out:
            out.append(k)
    return out


def cite_score(cited: list[str], gold_keys: list[str]) -> tuple[float, bool]:
    """RTG: +1 for the gold, −1 for every other citation; 0 when nothing cited."""
    gold_hit = any(any(g in c for g in gold_keys) for c in cited)
    return float((1 if gold_hit else 0) - (len(cited) - (1 if gold_hit else 0))), gold_hit


@dataclass
class QRow:
    query: str
    gold_keys: list[str]
    written: int = 0
    rank_before: int | None = None
    rank_after: int | None = None
    intrusion: float = 0.0  # share of top-k taken by distractors
    top_intruder_rank: int | None = None
    cite_before: float | None = None
    cite_after: float | None = None
    gold_cited_before: bool | None = None
    gold_cited_after: bool | None = None
    error: str = ""


@dataclass
class Report:
    n: int
    k: int
    per_gold: int
    written: int
    recall_before: float
    recall_after: float
    mrr_before: float
    mrr_after: float
    intrusion_mean: float
    gold_outranked: int  # questions where a distractor sits above the gold after
    cite_before: float | None
    cite_after: float | None
    gold_cited_before: float | None
    gold_cited_after: float | None
    rows: list[QRow] = field(default_factory=list)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def summarise(rows: list[QRow], *, k: int, per_gold: int) -> Report:
    ok = [r for r in rows if not r.error]
    n = len(ok) or 1

    def recall(attr: str) -> float:
        return round(sum(1 for r in ok if getattr(r, attr) is not None) / n, 4)

    def mrr(attr: str) -> float:
        return round(sum(1.0 / getattr(r, attr) for r in ok if getattr(r, attr)) / n, 4)

    outranked = sum(
        1
        for r in ok
        if r.top_intruder_rank is not None
        and (r.rank_after is None or r.top_intruder_rank < r.rank_after)
    )
    cb = [r.cite_before for r in ok if r.cite_before is not None]
    ca = [r.cite_after for r in ok if r.cite_after is not None]
    gb = [1.0 if r.gold_cited_before else 0.0 for r in ok if r.gold_cited_before is not None]
    ga = [1.0 if r.gold_cited_after else 0.0 for r in ok if r.gold_cited_after is not None]
    return Report(
        n=len(ok),
        k=k,
        per_gold=per_gold,
        written=sum(r.written for r in ok),
        recall_before=recall("rank_before"),
        recall_after=recall("rank_after"),
        mrr_before=mrr("rank_before"),
        mrr_after=mrr("rank_after"),
        intrusion_mean=round(sum(r.intrusion for r in ok) / n, 4),
        gold_outranked=outranked,
        cite_before=_mean(cb),
        cite_after=_mean(ca),
        gold_cited_before=_mean(gb),
        gold_cited_after=_mean(ga),
        rows=rows,
    )


def render(rep: Report) -> str:
    lines = [
        f"hard distractors: {rep.n} questions, {rep.written} distractors written "
        f"({rep.per_gold} per gold), k={rep.k}",
        "",
        "| metric | before | after |",
        "|---|---|---|",
        f"| recall@{rep.k} | {rep.recall_before:.3f} | {rep.recall_after:.3f} |",
        f"| MRR | {rep.mrr_before:.3f} | {rep.mrr_after:.3f} |",
        f"| intrusion@{rep.k} (distractor share of top-k) | — | {rep.intrusion_mean:.3f} |",
        f"| gold outranked by a distractor | — | {rep.gold_outranked}/{rep.n} |",
    ]
    if rep.cite_before is not None or rep.cite_after is not None:
        fmt = lambda v: "—" if v is None else f"{v:+.3f}"  # noqa: E731
        share = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
        lines.append(f"| cite score (±1) | {fmt(rep.cite_before)} | {fmt(rep.cite_after)} |")
        lines.append(
            f"| gold cited | {share(rep.gold_cited_before)} | {share(rep.gold_cited_after)} |"
        )
    return "\n".join(lines)


# ---- client -------------------------------------------------------------------


class Client:
    def __init__(self, base_url: str, token: str) -> None:
        import httpx

        self.base = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        self.http = httpx.AsyncClient(timeout=120.0)

    async def op(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(4):
            try:
                r = await self.http.post(f"{self.base}/v1/{name}", json=body, headers=self.headers)
            except Exception:
                if attempt == 3:
                    raise
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 500 and attempt < 3:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        await self.http.aclose()


def _ident(a: argparse.Namespace, org: str, ns: str) -> dict[str, Any]:
    return {"org": org, "namespace": ns, "agent": a.agent}


async def run(a: argparse.Namespace) -> Report:
    qs, ds_org, ds_ns = load_questions(a.dataset)
    org, ns = a.org or ds_org, a.namespace or ds_ns
    if not org or not ns:
        raise SystemExit("dataset carries no org/namespace; pass --org/--namespace")
    if a.limit:
        qs = qs[: a.limit]
    token = a.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        raise SystemExit("--token or LOOM_API_KEY required")
    c = Client(a.base_url, token)
    run_tag = f"{TAG}_{int(time.time())}"
    rows: list[QRow] = []
    written_keys: list[str] = []
    try:
        search_body = lambda q: {  # noqa: E731
            **_ident(a, org, ns),
            "query": q,
            "top_k": a.top_k,
            "namespace_scope": "this",
            "search_mode": a.search_mode,
        }
        ask_body = lambda q: {  # noqa: E731
            **_ident(a, org, ns),
            "query": q,
            "top_k": a.top_k,
            "namespace_scope": "this",
            "search_mode": a.search_mode,
            "precedents": False,
        }

        async def measure(q: str, gold: list[str], row: QRow, when: str) -> list[str]:
            resp = await c.op("memory.search", search_body(q))
            sc = score_one(resp, gold)
            setattr(row, f"rank_{when}", sc.hit_rank)
            if a.ask:
                ans = await c.op("memory.ask", ask_body(q))
                if ans.get("source") in ("synthesized", "precedent"):
                    cited = cited_keys(str(ans.get("answer") or ""), list(ans.get("results") or []))
                    s, g = cite_score(cited, gold)
                    setattr(row, f"cite_{when}", s)
                    setattr(row, f"gold_cited_{when}", g)
            return sc.returned

        # 1) baseline + gold texts
        golds: dict[str, str] = {}
        for q in qs:
            row = QRow(query=q["query"], gold_keys=list(q["gold_keys"]))
            rows.append(row)
            try:
                await measure(row.query, row.gold_keys, row, "before")
                for gk in row.gold_keys:
                    if gk in golds:
                        continue
                    got = await c.op("memory.get", {**_ident(a, org, ns), "memory_key": gk})
                    if got.get("found"):
                        golds[gk] = value_text(got.get("value"))
            except Exception as e:  # noqa: BLE001 — a failed question is recorded, not fatal
                row.error = f"before: {e}"
        pool = entity_pool(list(golds.values()))

        # 2) write distractors
        for row in rows:
            if row.error:
                continue
            for gk in row.gold_keys:
                text = golds.get(gk)
                if not text:
                    continue
                for i, d in enumerate(
                    make_distractors(
                        text,
                        per_gold=a.per_gold,
                        pool=pool,
                        seed=a.seed,
                        min_dist=a.min_dist,
                        max_dist=a.max_dist,
                    )
                ):
                    key = distractor_key(gk, i)
                    try:
                        await c.op(
                            "memory.set",
                            {
                                **_ident(a, org, ns),
                                "memory_key": key,
                                "value": d,
                                "memory_type": "semantic",
                                "tags": [TAG, run_tag],
                                "source_trace": f"bench/hard_distractors.py gold={gk}",
                            },
                        )
                        written_keys.append(key)
                        row.written += 1
                    except Exception as e:  # noqa: BLE001
                        row.error = f"write: {e}"
                        break

        # 3) let the embeddings land, then re-measure
        if written_keys:
            deadline = time.time() + a.settle_seconds
            probe = rows[0].query if rows else ""
            while time.time() < deadline:
                resp = await c.op("memory.search", {**search_body(probe), "top_k": 50})
                hits = resp.get("results") or []
                if any(str(h.get("memory_key", "")).startswith(KEY_PREFIX) for h in hits):
                    break
                await asyncio.sleep(3.0)
        for row in rows:
            if row.error:
                continue
            try:
                returned = await measure(row.query, row.gold_keys, row, "after")
                top = returned[: a.top_k]
                intruders = [i for i, k in enumerate(top, start=1) if k.startswith(KEY_PREFIX)]
                row.intrusion = round(len(intruders) / a.top_k, 4)
                row.top_intruder_rank = intruders[0] if intruders else None
            except Exception as e:  # noqa: BLE001
                row.error = f"after: {e}"
    finally:
        if written_keys and not a.keep:
            for key in written_keys:
                try:
                    await c.op("memory.delete", {**_ident(a, org, ns), "memory_key": key})
                except Exception as e:  # noqa: BLE001
                    print(f"cleanup failed for {key}: {e}", file=sys.stderr)
        await c.close()
    return summarise(rows, k=a.top_k, per_gold=a.per_gold)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset", required=True)
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default="")
    p.add_argument("--org", default="")
    p.add_argument("--namespace", default="")
    p.add_argument("--agent", default="bench")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--search-mode", default="rrf")
    p.add_argument("--per-gold", type=int, default=5)
    p.add_argument(
        "--min-dist",
        type=float,
        default=0.0,
        help="normalised Levenshtein floor; 0 = any change (one digit) is near enough",
    )
    p.add_argument("--max-dist", type=float, default=0.35)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--settle-seconds", type=float, default=60.0)
    p.add_argument("--ask", action="store_true", help="also score memory.ask citations (±1)")
    p.add_argument("--keep", action="store_true", help="leave the distractors in the namespace")
    p.add_argument("--out", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--max-intrusion",
        type=float,
        default=None,
        help="exit 1 when the mean distractor share of the top-k exceeds this",
    )
    a = p.parse_args(argv)
    rep = asyncio.run(run(a))
    if a.out:
        Path(a.out).write_text(json.dumps(asdict(rep), indent=2))
    print(json.dumps(asdict(rep), indent=2) if a.json else render(rep))
    if a.max_intrusion is not None and rep.intrusion_mean > a.max_intrusion:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
