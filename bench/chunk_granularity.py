"""Which raw-chunk granularity finds the evidence?

The *unit* a conversation memory bank is built from decides retrieval:
turn-level units are fragmentary, session-level units drown the query in
unrelated topics, summaries lose the details a question needs, and topically
coherent segments can beat all three. Loom's raw-chunk plane cuts each
ingested session into fixed 800-character windows — about 3.4
turns on LoCoMo — with no topic model.

This bench asks that question of loom's chunk plane without a judge:
for one LoCoMo conversation it re-chunks every session per arm, imports the
chunks through `memory.import_evidence` (the server embeds), searches every
question, and scores the hits by the sessions they came from
against the question's gold evidence sessions.

    python bench/chunk_granularity.py --base-url http://localhost:7803 \\
        --questions bench/datasets/locomo10.json --sample-id conv-26 \\
        --arms turns:1,turns:3,turns:5,turns:10,chars:400,chars:800,chars:1600,session,topic

Arms: `turns:N` (N turns per chunk), `chars:C` (loom's own splitter at C
characters), `session` (one chunk per session), `topic` (zero-shot LLM
segmentation with a fixed topic-segmentation prompt, `--seg-model`). Metrics per arm:
session recall@k, hit@k, MRR of the first gold-session hit, DCG@k, mean
retrieved characters, chunk count, and a per-category breakdown. Two phases
(`--phase import` then `--phase search`, or both) so the server can be
restarted between them: a namespace this process just wrote is read with
sequential consistency for a while, ~3 s per search.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.bench_namespaces import erase_bench_namespaces, register_bench_namespace
from bench.locomo_qa import (
    _chat,
    evidence_sessions_cited,
    iter_sessions,
    turn_to_message,
)
from server.memory.app import chunk_projection as chunk_projection
from server.memory.domain import chunk_shape

_SESSION_NO_RE = re.compile(r"session_(\d+)$")


@dataclass
class Chunk:
    session_no: int
    chunk_index: int
    text: str
    turn_start: int  # 0-based turn indices inside the session, inclusive
    turn_end: int


@dataclass
class ArmResult:
    arm: str
    namespace: str
    chunks: int = 0
    chars_per_chunk: float = 0.0
    turns_per_chunk: float = 0.0
    import_seconds: float = 0.0
    n: int = 0
    hit_at_k: int = 0
    session_recall_sum: float = 0.0
    rr_sum: float = 0.0
    dcg_sum: float = 0.0
    retrieved_chars_sum: int = 0
    turn_hit_at_k: int = 0
    turn_recall_sum: float = 0.0
    turn_rr_sum: float = 0.0
    turn_dcg_sum: float = 0.0
    recall_b3k_sum: float = 0.0
    recall_b6k_sum: float = 0.0
    precision_sum: float = 0.0
    by_category: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    failures: int = 0

    def summary(self) -> dict[str, Any]:
        n = max(1, self.n)
        return {
            "arm": self.arm,
            "namespace": self.namespace,
            "chunks": self.chunks,
            "chars_per_chunk": round(self.chars_per_chunk, 1),
            "turns_per_chunk": round(self.turns_per_chunk, 2),
            "import_seconds": round(self.import_seconds, 1),
            "questions": self.n,
            "hit_at_k": round(self.hit_at_k / n, 3),
            "session_recall_at_k": round(self.session_recall_sum / n, 3),
            "mrr": round(self.rr_sum / n, 3),
            "dcg_at_k": round(self.dcg_sum / n, 3),
            "retrieved_chars_mean": round(self.retrieved_chars_sum / n),
            "turn_hit_at_k": round(self.turn_hit_at_k / n, 3),
            "turn_recall_at_k": round(self.turn_recall_sum / n, 3),
            "turn_mrr": round(self.turn_rr_sum / n, 3),
            "turn_dcg_at_k": round(self.turn_dcg_sum / n, 3),
            "turn_recall_at_3k_chars": round(self.recall_b3k_sum / n, 3),
            "turn_recall_at_6k_chars": round(self.recall_b6k_sum / n, 3),
            "turn_precision": round(self.precision_sum / n, 3),
            "failures": self.failures,
            "by_category_turn_recall": {
                c: round(mean(v), 3) for c, v in sorted(self.by_category.items())
            },
        }


# ------------------------------------------------------------------ rendering


def render_turn(turn: dict[str, Any]) -> str:
    """The line loom's chunk writer embeds for a LoCoMo turn: `[user] Speaker: text`."""
    m = turn_to_message(turn)
    return f"[{m['role']}] {m['content']}"


def chunks_by_turns(session_no: int, lines: list[str], n: int) -> list[Chunk]:
    out: list[Chunk] = []
    for i in range(0, len(lines), n):
        seg = lines[i : i + n]
        out.append(Chunk(session_no, len(out), "\n".join(seg), i, i + len(seg) - 1))
    return out


def chunks_by_chars(session_no: int, lines: list[str], size: int) -> list[Chunk]:
    """Loom's own splitter (`chunk_text_with_truncation`) at `size` characters;
    turn spans recovered from the 1-based line numbers it returns."""
    text = "\n".join(lines)
    old_size, old_overlap, old_cap = (
        chunk_shape.CHUNK_SIZE,
        chunk_shape.CHUNK_OVERLAP,
        chunk_projection._MAX_CHUNKS_PER_CALL,
    )
    # loom's overlap is 100 chars at 800; keep the same 1/8 ratio at other sizes
    (
        chunk_shape.CHUNK_SIZE,
        chunk_shape.CHUNK_OVERLAP,
        chunk_projection._MAX_CHUNKS_PER_CALL,
    ) = size, max(0, size // 8), 10_000
    try:
        pieces, _ = chunk_projection.chunk_text_with_truncation(text)
    finally:
        (
            chunk_shape.CHUNK_SIZE,
            chunk_shape.CHUNK_OVERLAP,
            chunk_projection._MAX_CHUNKS_PER_CALL,
        ) = old_size, old_overlap, old_cap
    return [
        Chunk(session_no, i, chunk, line_start - 1, line_end - 1)
        for i, (chunk, line_start, line_end) in enumerate(pieces)
    ]


def chunks_by_session(session_no: int, lines: list[str]) -> list[Chunk]:
    return [Chunk(session_no, 0, "\n".join(lines), 0, len(lines) - 1)]


def chunks_by_segments(
    session_no: int, lines: list[str], segments: list[tuple[int, int]]
) -> list[Chunk]:
    out: list[Chunk] = []
    for a, b in segments:
        a, b = max(0, a), min(len(lines) - 1, b)
        if b < a:
            continue
        out.append(Chunk(session_no, len(out), "\n".join(lines[a : b + 1]), a, b))
    return out


# ---------------------------------------------------- topic segmentation

_SEG_PROMPT = """# Instruction
## Context
- **Goal**: Your task is to segment a multi-turn conversation between two speakers into
topically coherent units based on semantics. Successive turns on the same topic should be
grouped into the same segmentation unit, and new segmentation units should be created when the
topic shifts.
- **Data**: The input data is a series of turns separated by "\\n\\n". Each turn starts with
"[Turn (Turn Number)]: ".
### Output Format
- Output the segmentation results in **JSONL (JSON Lines)** format. Each dictionary represents a
segment, consisting of one or more turns on the same topic, with keys:
- **segment_id**: The index of this segment, starting from 0.
- **start_turn_number**: The number of the **first** turn in this segment.
- **end_turn_number**: The number of the **last** turn in this segment.
- **num_turns**: end_turn_number - start_turn_number + 1.
Example:
<segmentation>
{"segment_id": 0, "start_turn_number": 0, "end_turn_number": 5, "num_turns": 6}
{"segment_id": 1, "start_turn_number": 6, "end_turn_number": 8, "num_turns": 3}
</segmentation>
# Data
{text}
# Question
## Please generate the segmentation result from the input data that meets the following
requirements:
- **No Missing Turns**: the turn numbers cover all turns without omission.
- **No Overlapping Turns**: successive segments have no overlap.
- **Accurate Counting**: the sum of num_turns equals the total number of turns.
- Provide your segmentation result between the tags: <segmentation></segmentation>.
# Output
Now, provide the segmentation result based on the instructions above."""


def parse_segments(reply: str, n_turns: int) -> list[tuple[int, int]] | None:
    """(start, end) turn spans from the model's JSONL; None when they do not
    tile [0, n_turns) exactly — the caller falls back to a fixed arm."""
    body = reply
    m = re.search(r"<segmentation>(.*?)</segmentation>", reply, re.S)
    if m:
        body = m.group(1)
    spans: list[tuple[int, int]] = []
    for line in body.splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            spans.append((int(d["start_turn_number"]), int(d["end_turn_number"])))
        except (KeyError, TypeError, ValueError):
            continue
    spans.sort()
    expect = 0
    for a, b in spans:
        if a != expect or b < a:
            return None
        expect = b + 1
    return spans if expect == n_turns and spans else None


async def segment_session(
    client: httpx.AsyncClient, model: str, api_key: str, lines: list[str], usage: dict[str, int]
) -> list[tuple[int, int]] | None:
    text = "\n\n".join(f"[Turn {i}]: {ln}" for i, ln in enumerate(lines))
    try:
        reply = await _chat(
            client,
            model,
            _SEG_PROMPT.replace("{text}", text),
            api_key,
            max_tokens=1500,
            usage=usage,
        )
    except Exception as e:  # noqa: BLE001 — one session's failure falls back, not the run
        print(f"  segmentation call failed: {e}", file=sys.stderr)
        return None
    return parse_segments(reply, len(lines))


# ------------------------------------------------------------------ loom client


class Loom:
    def __init__(self, base_url: str, token: str) -> None:
        self._c = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=120.0,
        )

    async def post(self, op: str, body: dict[str, Any], *, retries: int = 5) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self._c.post(f"/v1/{op}", json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"{op} → HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except (RuntimeError, httpx.HTTPError) as e:
                last = e
                await asyncio.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"{op} failed after {retries} attempts: {last}")

    async def close(self) -> None:
        await self._c.aclose()

    async def bench_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """`post` as the `post(path, body)` bench_namespaces takes."""
        return await self.post(path.removeprefix("/v1/"), body)


def session_id_for(namespace: str, session_no: int) -> str:
    return f"{namespace}:session_{session_no}"


async def import_arm(loom: Loom, org: str, namespace: str, agent: str, chunks: list[Chunk]) -> int:
    written = 0
    for i in range(0, len(chunks), 64):
        batch = chunks[i : i + 64]
        res = await loom.post(
            "memory.import_evidence",
            {
                "org": org,
                "namespace": namespace,
                "agent": agent,
                "user_id": "-",
                "source_trace": "bench:chunk_granularity",
                "chunks": [
                    {
                        "session_id": session_id_for(namespace, c.session_no),
                        "chunk_index": c.chunk_index,
                        "content": c.text,
                        "line_start": c.turn_start + 1,
                        "line_end": c.turn_end + 1,
                    }
                    for c in batch
                ],
            },
        )
        written += int(res.get("chunks_written") or 0)
    return written


_DIA_ID_RE = re.compile(r"^D(\d+):(\d+)$")


def gold_turns(q: dict[str, Any]) -> set[tuple[int, int]]:
    """(session_no, 0-based turn index) for every gold evidence id `D<s>:<n>`
    (LoCoMo numbers turns from 1 within a session)."""
    out: set[tuple[int, int]] = set()
    for ev in q.get("evidence") or []:
        m = _DIA_ID_RE.match(str(ev).strip())
        if m:
            out.add((int(m.group(1)), int(m.group(2)) - 1))
    return out


def score_turn_hits(
    hit_spans: list[tuple[int, int, int]], gold: set[tuple[int, int]], k: int
) -> tuple[int, float, float, float]:
    """Turn-level (hit@k, recall@k, reciprocal rank, DCG@k): a hit is the
    (session, turn_start, turn_end) span of the retrieved chunk; it is
    relevant when it covers a gold turn. Recall counts gold turns covered by
    any of the first k chunks; DCG credits each relevant chunk at its rank."""
    top = hit_spans[:k]
    covered: set[tuple[int, int]] = set()
    rr = 0.0
    dcg = 0.0
    for rank, (s, a, b) in enumerate(top, start=1):
        here = {(gs, gt) for gs, gt in gold if gs == s and a <= gt <= b}
        if here:
            if rr == 0.0:
                rr = 1.0 / rank
            dcg += 1.0 / math.log2(rank + 1)
            covered |= here
    recall = len(covered) / len(gold) if gold else 0.0
    return int(bool(covered)), recall, rr, dcg


def recall_at_budget(
    hit_spans: list[tuple[int, int, int]],
    hit_chars: list[int],
    gold: set[tuple[int, int]],
    budget_chars: int,
) -> float:
    """Gold turns covered by the hits that fit, in rank order, inside a
    character budget — units are compared at a fixed context budget (4k
    tokens on LoCoMo), so a unit that covers more turns only by being
    bigger does not win here for free."""
    if not gold:
        return 0.0
    covered: set[tuple[int, int]] = set()
    spent = 0
    for (s, a, b), n in zip(hit_spans, hit_chars, strict=False):
        if spent + n > budget_chars and spent > 0:
            break
        spent += n
        covered |= {(gs, gt) for gs, gt in gold if gs == s and a <= gt <= b}
    return len(covered) / len(gold)


def turn_precision(hit_spans: list[tuple[int, int, int]], gold: set[tuple[int, int]]) -> float:
    """Gold turns covered per turn retrieved (0 when nothing was retrieved)."""
    turns = sum(max(0, b - a + 1) for _s, a, b in hit_spans if a >= 0)
    if not turns:
        return 0.0
    covered = {(gs, gt) for s, a, b in hit_spans for gs, gt in gold if gs == s and a <= gt <= b}
    return len(covered) / turns


def expand_neighbors(
    hit_spans: list[tuple[int, int, int]],
    hit_chars: list[int],
    spans: dict[tuple[int, int], tuple[int, int]],
    chunk_chars: dict[str, int],
    n: int,
) -> tuple[list[tuple[int, int, int]], list[int]]:
    """Read-time neighbour expansion, simulated: each retrieved chunk brings
    its `n` neighbours on either side (same session, adjacent chunk_index)
    into the context right after it, once each. Recall gains and the
    characters spent are what a server-side expansion would deliver."""
    by_span = {(s, a, b): (s, i) for (s, i), (a, b) in spans.items()}
    out_spans: list[tuple[int, int, int]] = []
    out_chars: list[int] = []
    seen: set[tuple[int, int]] = set()
    for (s, a, b), c in zip(hit_spans, hit_chars, strict=False):
        key = by_span.get((s, a, b))
        if key is None:
            out_spans.append((s, a, b))
            out_chars.append(c)
            continue
        _s, idx = key
        for j in [idx, *(idx + d for d in range(1, n + 1)), *(idx - d for d in range(1, n + 1))]:
            if (s, j) in seen or (s, j) not in spans:
                continue
            seen.add((s, j))
            na, nb = spans[(s, j)]
            out_spans.append((s, na, nb))
            out_chars.append(int(chunk_chars.get(f"{s}:{j}", 0)))
    return out_spans, out_chars


def score_hits(hit_sessions: list[int], gold: set[int], k: int) -> tuple[int, float, float, float]:
    """(hit@k, session recall@k, reciprocal rank, DCG@k) over gold sessions."""
    top = hit_sessions[:k]
    hit = int(any(s in gold for s in top))
    recall = len(gold & set(top)) / len(gold) if gold else 0.0
    rr = 0.0
    dcg = 0.0
    seen: set[int] = set()
    for rank, s in enumerate(top, start=1):
        if s in gold:
            if rr == 0.0:
                rr = 1.0 / rank
            if s not in seen:  # a session counts once, at its first appearance
                dcg += 1.0 / math.log2(rank + 1)
                seen.add(s)
    return hit, recall, rr, dcg


# ------------------------------------------------------------------ main


def build_arm_chunks(
    arm: str,
    sessions: list[tuple[int, list[str]]],
    segments: dict[int, list[tuple[int, int]]] | None,
) -> list[Chunk]:
    out: list[Chunk] = []
    for session_no, lines in sessions:
        if arm.startswith("turns:"):
            out += chunks_by_turns(session_no, lines, int(arm.split(":")[1]))
        elif arm.startswith("chars:"):
            out += chunks_by_chars(session_no, lines, int(arm.split(":")[1]))
        elif arm == "session":
            out += chunks_by_session(session_no, lines)
        elif arm == "topic":
            spans = (segments or {}).get(session_no)
            if spans is None:
                out += chunks_by_turns(session_no, lines, 5)  # fallback, counted by the caller
            else:
                out += chunks_by_segments(session_no, lines, spans)
        else:
            raise SystemExit(f"unknown arm {arm!r}")
    return out


async def run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        print("LOOM_API_KEY required", file=sys.stderr)
        return 2
    data = json.loads(Path(args.questions).read_text())
    sample = next(s for s in data if s.get("sample_id") == args.sample_id)
    sessions: list[tuple[int, list[str]]] = []
    for key, _date, turns in iter_sessions(sample["conversation"]):
        m = _SESSION_NO_RE.search(key)
        if not m:
            continue
        sessions.append((int(m.group(1)), [render_turn(t) for t in turns]))
    qs = [q for q in sample["qa"] if evidence_sessions_cited(q)][: args.max_questions]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    state_path = Path(args.state)
    state: dict[str, Any] = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("namespaces", {})
    state.setdefault("chunk_stats", {})
    loom = Loom(args.base_url, token)
    usage = {"tin": 0, "tout": 0, "calls": 0}
    results: list[ArmResult] = []
    # Namespaces this run creates. An import-only phase keeps them for the later
    # --phase search run; otherwise they are erased when the run exits, after a
    # failure too. Namespaces read from --state were made by an earlier run and
    # are left alone.
    minted: list[str] = []
    keep = args.keep_namespaces or args.phase == "import"
    try:
        segments: dict[int, list[tuple[int, int]]] | None = None
        seg_fallbacks = 0
        if "topic" in arms and args.phase in ("import", "both"):
            api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                print("OPENAI_API_KEY required for the topic arm", file=sys.stderr)
                return 2
            segments = {}
            async with httpx.AsyncClient() as oc:
                for session_no, lines in sessions:
                    spans = await segment_session(oc, args.seg_model, api_key, lines, usage)
                    if spans is None:
                        seg_fallbacks += 1
                    else:
                        segments[session_no] = spans
            state["segments"] = {str(k): v for k, v in segments.items()}
            state["segmentation_usage"] = dict(usage)
            state["segmentation_fallback_sessions"] = seg_fallbacks
        elif "topic" in arms and state.get("segments"):
            segments = {int(k): [tuple(x) for x in v] for k, v in state["segments"].items()}

        for arm in arms:
            ns = state["namespaces"].get(arm)
            if args.phase in ("import", "both"):
                chunks = build_arm_chunks(arm, sessions, segments)
                if not ns:
                    ns = f"{args.namespace_prefix}-{arm.replace(':', '')}-{secrets.token_hex(3)}"
                    minted.append(ns)
                    await register_bench_namespace(
                        loom.bench_post,
                        org=args.org,
                        namespace=ns,
                        description=f"chunk granularity arm {arm}",
                        keep=keep,
                    )
                t0 = asyncio.get_event_loop().time()
                written = await import_arm(loom, args.org, ns, args.agent, chunks)
                dt = asyncio.get_event_loop().time() - t0
                state["namespaces"][arm] = ns
                state["chunk_stats"][arm] = {
                    "chunks": written,
                    "chars_per_chunk": mean(len(c.text) for c in chunks) if chunks else 0.0,
                    "turns_per_chunk": mean(c.turn_end - c.turn_start + 1 for c in chunks)
                    if chunks
                    else 0.0,
                    "import_seconds": dt,
                    "chunk_chars": {f"{c.session_no}:{c.chunk_index}": len(c.text) for c in chunks},
                }
                state_path.write_text(json.dumps(state, indent=1))
                print(f"  {arm}: {written} chunks → {ns} in {dt:.1f}s", file=sys.stderr)
            if args.phase not in ("search", "both"):
                continue
            if not ns:
                print(f"  {arm}: no namespace in state; run --phase import first", file=sys.stderr)
                continue
            st = state["chunk_stats"].get(arm, {})
            res = ArmResult(
                arm=arm,
                namespace=ns,
                chunks=int(st.get("chunks", 0)),
                chars_per_chunk=float(st.get("chars_per_chunk", 0.0)),
                turns_per_chunk=float(st.get("turns_per_chunk", 0.0)),
                import_seconds=float(st.get("import_seconds", 0.0)),
            )
            chunk_chars: dict[str, int] = st.get("chunk_chars", {})
            spans = {
                (c.session_no, c.chunk_index): (c.turn_start, c.turn_end)
                for c in build_arm_chunks(arm, sessions, segments)
            }
            for q in qs:
                gold = evidence_sessions_cited(q)
                gold_t = gold_turns(q)
                try:
                    resp = await loom.post(
                        "memory.search",
                        {
                            "org": args.org,
                            "namespace": ns,
                            "agent": args.agent,
                            "user_id": "-",
                            "query": str(q.get("question") or ""),
                            "top_k": args.top_k,
                            "search_mode": args.search_mode,
                            "namespace_scope": "this",
                            "auto_touch": False,
                        },
                    )
                except RuntimeError as e:
                    res.failures += 1
                    print(f"  {arm}: search failed: {e}", file=sys.stderr)
                    continue
                hits = resp.get("results") or []
                hit_sessions: list[int] = []
                hit_spans: list[tuple[int, int, int]] = []
                hit_chars: list[int] = []
                for h in hits:
                    m = _SESSION_NO_RE.search(str(h.get("session_id") or ""))
                    if m:
                        hit_sessions.append(int(m.group(1)))
                    key = str(h.get("memory_key") or "")
                    # chunk keys are `chunk:<session_id>:<index>`
                    km = re.search(r"session_(\d+):(\d+)$", key)
                    if km:
                        s_no, idx = int(km.group(1)), int(km.group(2))
                        n_chars = int(chunk_chars.get(f"{s_no}:{idx}", 0))
                        res.retrieved_chars_sum += n_chars
                        a, b = spans.get((s_no, idx), (-1, -1))
                        hit_spans.append((s_no, a, b))
                        hit_chars.append(n_chars)
                if args.expand_neighbors:
                    hit_spans, hit_chars = expand_neighbors(
                        hit_spans, hit_chars, spans, chunk_chars, args.expand_neighbors
                    )
                hit, recall, rr, dcg = score_hits(hit_sessions, gold, args.top_k)
                t_hit, t_recall, t_rr, t_dcg = score_turn_hits(
                    hit_spans, gold_t, args.top_k * (1 + 2 * args.expand_neighbors)
                )
                res.turn_hit_at_k += t_hit
                res.turn_recall_sum += t_recall
                res.turn_rr_sum += t_rr
                res.turn_dcg_sum += t_dcg
                res.recall_b3k_sum += recall_at_budget(hit_spans, hit_chars, gold_t, 3000)
                res.recall_b6k_sum += recall_at_budget(hit_spans, hit_chars, gold_t, 6000)
                res.precision_sum += turn_precision(hit_spans, gold_t)
                res.n += 1
                res.hit_at_k += hit
                res.session_recall_sum += recall
                res.rr_sum += rr
                res.dcg_sum += dcg
                res.by_category[str(q.get("category"))].append(t_recall)
            results.append(res)
            print(json.dumps(res.summary()), file=sys.stderr)
    finally:
        if minted and not keep:
            await erase_bench_namespaces(loom.bench_post, org=args.org, namespaces=minted)
            # The state file must not hand an erased namespace to a later search.
            state["namespaces"] = {
                a: n for a, n in state["namespaces"].items() if n not in set(minted)
            }
            state_path.write_text(json.dumps(state, indent=1))
        await loom.close()
    if args.phase in ("search", "both"):
        out = {
            "sample_id": args.sample_id,
            "top_k": args.top_k,
            "search_mode": args.search_mode,
            "questions": len(qs),
            "arms": [r.summary() for r in results],
            "segmentation_usage": state.get("segmentation_usage"),
            "segmentation_fallback_sessions": state.get("segmentation_fallback_sessions"),
        }
        Path(args.out).write_text(json.dumps(out, indent=1))
        print(
            f"{'arm':<11} {'chunks':>6} {'turns/ch':>8} {'chars/ch':>8} {'hit@k':>6} "
            f"{'sessR@k':>8} {'turnR@k':>8} {'tMRR':>6} {'tDCG':>6} {'R@3k':>6} {'R@6k':>6} "
            f"{'prec':>6} {'chars/q':>8}"
        )
        for r in results:
            s = r.summary()
            print(
                f"{s['arm']:<11} {s['chunks']:>6} {s['turns_per_chunk']:>8} "
                f"{s['chars_per_chunk']:>8} {s['hit_at_k']:>6} {s['session_recall_at_k']:>8} "
                f"{s['turn_recall_at_k']:>8} {s['turn_mrr']:>6} {s['turn_dcg_at_k']:>6} "
                f"{s['turn_recall_at_3k_chars']:>6} {s['turn_recall_at_6k_chars']:>6} "
                f"{s['turn_precision']:>6} "
                f"{s['retrieved_chars_mean']:>8}"
            )
        print(f"wrote {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=None)
    p.add_argument("--org", default="dev")
    p.add_argument("--agent", default="cg-bench")
    p.add_argument("--namespace-prefix", default="bench-cg")
    p.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase the arm namespaces this run creates (--phase import always keeps them)",
    )
    p.add_argument("--questions", default="bench/datasets/locomo10.json")
    p.add_argument("--sample-id", default="conv-26")
    p.add_argument("--max-questions", type=int, default=500)
    p.add_argument(
        "--arms",
        default="turns:1,turns:3,turns:5,turns:10,chars:400,chars:800,chars:1600,session,topic",
    )
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument(
        "--expand-neighbors",
        type=int,
        default=0,
        help="simulate read-time expansion: each hit brings N adjacent chunks per side",
    )
    p.add_argument("--search-mode", default="semantic")
    p.add_argument("--seg-model", default=os.environ.get("BENCH_SEG_MODEL", "gpt-4o-mini"))
    p.add_argument("--phase", choices=["import", "search", "both"], default="both")
    p.add_argument("--state", default="bench/chunk_granularity.state.json")
    p.add_argument("--out", default="bench/chunk_granularity.summary.json")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
