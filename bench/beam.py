"""BEAM through loom: ten memory abilities on conversations of 128K–10M tokens, graded
with BEAM's own rubric judge.

Why this bench exists beside LongMemEval, MemBench and LoCoMo: it is the
only public set that probes contradiction resolution, event ordering,
instruction following, preference following and summarization in one
harness, and the only one whose haystack grows past 1M tokens — the scale
at which a relevance filter over retrieved memories may pay, and at which
loom's relevance gate would have to be re-measured before it could earn a
default. LongMemEval-S is ~115K tokens; BEAM's smallest
chats are that size and its largest are 90× larger.

Data: the released chats (github.com/mohammadtavakoli78/BEAM, `chats/
<scale>/<n>/{chat.json,probing_questions/probing_questions.json}`; the
same rows are on Hugging Face as `Mohammadta/BEAM`). Nothing is committed:
the corpus is third-party. `--fetch 100K:1,2` pulls chats from the GitHub
raw endpoint into `--data-dir`.

Per chat: a fresh namespace; every turn group (a user message and the
assistant reply, plus any follow-ups the user answered) fed IN ORDER through
memory.set_from_messages with the batch's time anchor as observation_date,
so knowledge updates supersede and temporal questions have dates. Then each
probing question, per ability: memory.search for the top-k memories, an
external reader that answers the user AS THE ASSISTANT from those memories
(the LIGHT shape: retrieved evidence + the question, no full transcript),
and BEAM's `unified_llm_judge_base_prompt` per rubric item, scored 1 / 0.5
/ 0 and averaged — BEAM's headline metric. Event ordering adds BEAM's
Kendall-τ_b × F1 over the reference order (alignment by the judge model
instead of an embedding threshold). `--answerer loom-ask` sends
the question to memory.ask instead, so server-side reader configurations
(depth, cascade, window cut) can be compared under one judge.

Evidence recall rides along where an item names `source_chat_ids`: the
share of those turn ids whose extracted memories appear in the top-k.

    python bench/beam.py --fetch 100K:1,2,3 --data-dir /tmp/beam
    python bench/beam.py --data-dir /tmp/beam --scale 100K --limit 3 \\
        --abilities knowledge_update,event_ordering,contradiction_resolution \\
        --base-url http://localhost:7777 --token $TOK --out bench/beam.json
    python bench/beam.py --data-dir /tmp/beam --dry
"""

# ruff: noqa: E501 — BEAM's judge prompt is reproduced verbatim (line lengths included)
# so a loom number is comparable with BEAM's published table.
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

try:
    from bench._gate import measurement_fields
    from bench._http import apost_json
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/beam.py` puts bench/ itself on sys.path
    from _gate import measurement_fields  # type: ignore[no-redef]
    from _http import apost_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

ABILITIES = (
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
)

_RAW = "https://raw.githubusercontent.com/mohammadtavakoli78/BEAM/main/chats"
_OPENAI_CHAT_URL = os.environ.get("OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions")
_DEFAULT_READER = os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini"
_DEFAULT_JUDGE = os.environ.get("BENCH_JUDGE_MODEL") or "gpt-4o-2024-08-06"

# BEAM's generator stamps every user turn with its plan coordinates.
_MARKER_RE = re.compile(r"\s*->->\s*[\d,\s]+$")

# BEAM's judge prompt, verbatim (src/prompts.py::unified_llm_judge_base_prompt),
# so a loom number is comparable with BEAM's published table.
UNIFIED_JUDGE_PROMPT = """
You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- RUBRIC CRITERION (what to check): <rubric_item>
- RESPONSE TO EVALUATE: <llm_response>

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate. 

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT
A compliant response must be **on-topic** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., "$68,000", "68k", "68,000 USD", or "sixty-eight thousand dollars"). Treat them as equal when numerically equivalent
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., "itemized list", "no citations", "one sentence").
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.
  
- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.
  
- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)
   
3. **Check Compliance**: 
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"
}

NOTE: ONLY output the json object, without any explanation before or after that
"""

READER_PROMPT = (
    "You are the assistant in a long-running conversation with this user. "
    "Below are the memories retrieved from that conversation, each with the "
    "date it was recorded. Continue the conversation: answer the user's "
    "latest message the way the assistant who had lived through all of it "
    "would — follow any standing instructions or stated preferences the "
    "memories record, use the most recent value when a fact was updated, "
    "point out when the memories contradict each other, order events by "
    "their dates, and if the memories genuinely do not contain what is "
    "asked, say so plainly instead of guessing.\n\n"
    "Today's date: {date}\n\nMEMORIES:\n{memories}\n\nUSER: {question}\n\nASSISTANT:"
)

ORDER_ALIGN_PROMPT = (
    "Below is a numbered REFERENCE list of events and a RESPONSE that "
    "describes an order of events. For each reference item that the "
    "response mentions (by meaning, not wording), note the position at "
    "which the response mentions it. Then count how many DISTINCT events the "
    "response lists that match none of the reference items.\n"
    'Reply with JSON only: {{"order": [<reference numbers in the order the '
    'response mentions them>], "extra": <count of unmatched events>}}.\n\n'
    "REFERENCE:\n{reference}\n\nRESPONSE:\n{response}"
)


# ---------------------------------------------------------------------------
# data


@dataclass
class Session:
    sid: str  # BEAM turn id of the first user message
    date: str  # ISO date or ""
    messages: list[dict[str, str]]
    turn_ids: list[int] = field(default_factory=list)


@dataclass
class Question:
    chat: str
    ability: str
    idx: int
    question: str
    rubric: list[str]
    reference: str  # ideal answer / response / summary, for the record
    source_turn_ids: list[int]
    difficulty: str = ""


@dataclass
class Chat:
    name: str
    scale: str
    sessions: list[Session]
    questions: list[Question]

    @property
    def chars(self) -> int:
        return sum(len(m["content"]) for s in self.sessions for m in s.messages)


def parse_anchor(anchor: str) -> str:
    """BEAM anchors read 'March-15-2024'; loom wants ISO. '' when unparseable."""
    a = (anchor or "").strip()
    if not a:
        return ""
    for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(a, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def strip_marker(text: str) -> str:
    return _MARKER_RE.sub("", text or "").strip()


def sessions_of(chat: list[dict[str, Any]]) -> list[Session]:
    """One session per turn GROUP (user, assistant, optional follow-ups). The
    batch's time anchor dates every group in it; a user turn's own anchor
    wins when present. Order is the conversation's order."""
    out: list[Session] = []
    for batch in chat:
        batch_date = parse_anchor(str(batch.get("time_anchor") or ""))
        for group in batch.get("turns") or []:
            msgs: list[dict[str, str]] = []
            ids: list[int] = []
            date = batch_date
            for t in group:
                role = str(t.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                content = strip_marker(str(t.get("content") or ""))
                if not content:
                    continue
                if role == "user" and t.get("time_anchor"):
                    date = parse_anchor(str(t["time_anchor"])) or date
                msgs.append({"role": role, "content": content})
                if isinstance(t.get("id"), int):
                    ids.append(int(t["id"]))
            if not msgs:
                continue
            sid = str(ids[0]) if ids else f"g{len(out)}"
            out.append(Session(sid=sid, date=date, messages=msgs, turn_ids=ids))
    return out


def _turn_ids(raw: Any) -> list[int]:
    """`source_chat_ids` is a list, or a dict of lists (knowledge_update's
    original_info / updated_info, temporal_reasoning's first/second event)."""
    if isinstance(raw, dict):
        vals: list[Any] = []
        for v in raw.values():
            vals.extend(v if isinstance(v, list) else [v])
    elif isinstance(raw, list):
        vals = raw
    else:
        vals = []
    out: list[int] = []
    for v in vals:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def questions_of(chat_name: str, pq: dict[str, Any], abilities: tuple[str, ...]) -> list[Question]:
    out: list[Question] = []
    for ability in abilities:
        for i, q in enumerate(pq.get(ability) or []):
            rubric = [str(r) for r in (q.get("rubric") or []) if str(r).strip()]
            if not rubric:
                continue
            reference = str(
                q.get("answer")
                or q.get("ideal_answer")
                or q.get("ideal_response")
                or q.get("ideal_summary")
                or q.get("expected_compliance")
                or ""
            )
            out.append(
                Question(
                    chat=chat_name,
                    ability=ability,
                    idx=i,
                    question=str(q.get("question") or ""),
                    rubric=rubric,
                    reference=reference,
                    source_turn_ids=_turn_ids(q.get("source_chat_ids")),
                    difficulty=str(q.get("difficulty") or ""),
                )
            )
    return out


def load_chats(data_dir: Path, scale: str, limit: int, abilities: tuple[str, ...]) -> list[Chat]:
    root = data_dir / scale
    if not root.is_dir():
        return []
    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir() and (d / "chat.json").exists()),
        key=lambda d: (len(d.name), d.name),
    )
    out: list[Chat] = []
    for d in dirs[: limit if limit > 0 else None]:
        chat = json.loads((d / "chat.json").read_text())
        pq_path = d / "probing_questions" / "probing_questions.json"
        pq = json.loads(pq_path.read_text()) if pq_path.exists() else {}
        name = f"{scale}/{d.name}"
        out.append(
            Chat(
                name=name,
                scale=scale,
                sessions=sessions_of(chat),
                questions=questions_of(name, pq, abilities),
            )
        )
    return out


async def fetch_chats(client: httpx.AsyncClient, data_dir: Path, spec: str) -> int:
    """`100K:1,2,3` → chats/100K/{1,2,3}/{chat.json, probing_questions.json}
    from the BEAM repository's raw endpoint. Existing files are kept."""
    n = 0
    for part in spec.split(";"):
        if ":" not in part:
            continue
        scale, ids = part.split(":", 1)
        for cid in (x.strip() for x in ids.split(",") if x.strip()):
            dest = data_dir / scale / cid
            (dest / "probing_questions").mkdir(parents=True, exist_ok=True)
            for rel in ("chat.json", "probing_questions/probing_questions.json"):
                target = dest / rel
                if target.exists():
                    continue
                r = await client.get(f"{_RAW}/{scale}/{cid}/{rel}", timeout=120.0)
                r.raise_for_status()
                target.write_bytes(r.content)
                n += 1
    return n


# ---------------------------------------------------------------------------
# scoring


def kendall_tau_b(x: list[int], y: list[int]) -> float:
    """Kendall's τ_b between two rankings of the same items (ties allowed).
    Pure Python; matches scipy.stats.kendalltau(variant='b') on untied and
    tied inputs. 0.0 when either ranking is constant."""
    n = len(x)
    if n < 2 or len(y) != n:
        return 0.0
    conc = disc = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                conc += 1
            else:
                disc += 1
    n0 = n * (n - 1) / 2
    denom = math.sqrt((n0 - ties_x) * (n0 - ties_y))
    return (conc - disc) / denom if denom else 0.0


def event_ordering_score(n_reference: int, order: list[int], extra: int) -> dict[str, float]:
    """BEAM's event-ordering metric: F1 of reference items mentioned (extras
    are false positives) × normalised τ_b between the reference order and the
    response's order, with unmentioned items tied at the end (their
    `tie_rank`). `order` holds 1-based reference numbers as the response
    mentioned them."""
    seen: list[int] = []
    for o in order:
        if 1 <= o <= n_reference and o not in seen:
            seen.append(o)
    tp = len(seen)
    fp = max(0, int(extra))
    fn = n_reference - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    tie_rank = n_reference + 1
    ref_ranks = list(range(1, n_reference + 1))
    sys_rank = {item: i + 1 for i, item in enumerate(seen)}
    sys_ranks = [sys_rank.get(item, tie_rank) for item in ref_ranks]
    tau = (
        kendall_tau_b(ref_ranks, sys_ranks)
        if tp >= 2
        else (1.0 if tp == 1 and n_reference == 1 else 0.0)
    )
    tau_norm = (tau + 1) / 2 if tp >= 2 else (1.0 if tp == 1 and n_reference == 1 else 0.0)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tau_norm": tau_norm,
        "final_score": tau_norm * f1,
    }


def parse_json_object(text: str) -> dict[str, Any] | None:
    """The judge is asked for JSON only; tolerate fences and prose around it."""
    raw = (text or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def rubric_score(verdicts: list[dict[str, Any] | None]) -> float:
    """Mean of per-item scores clamped to {0, 0.5, 1}; an unparseable verdict
    scores 0 (BEAM repairs JSON; we count it against, never drop it)."""
    if not verdicts:
        return 0.0
    total = 0.0
    for v in verdicts:
        try:
            s = float((v or {}).get("score", 0.0))
        except (TypeError, ValueError):
            s = 0.0
        total += min(1.0, max(0.0, round(s * 2) / 2))
    return total / len(verdicts)


def evidence_recall(
    source_ids: list[int], hit_keys: list[str], key_to_turns: dict[str, set[int]]
) -> float:
    if not source_ids:
        return -1.0  # not measured for this item
    got: set[int] = set()
    for k in hit_keys:
        got |= key_to_turns.get(k, set())
    return len(set(source_ids) & got) / len(set(source_ids))


# ---------------------------------------------------------------------------
# network


async def _post(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], token: str, retries: int = 3
) -> dict[str, Any]:
    out: dict[str, Any] = await apost_json(
        client, path, body, token=token, timeout_s=300.0, attempts=retries
    )
    return out


async def _chat(
    client: httpx.AsyncClient,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    max_tokens: int = 900,
    usage: dict[str, int] | None = None,
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.0,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = await client.post(
        _OPENAI_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=120.0,
    )
    r.raise_for_status()
    payload = r.json()
    if usage is not None and isinstance(payload.get("usage"), dict):
        usage["tin"] += int(payload["usage"].get("prompt_tokens") or 0)
        usage["tout"] += int(payload["usage"].get("completion_tokens") or 0)
        usage["calls"] += 1
    return str(payload["choices"][0]["message"]["content"] or "").strip()


def render_memories(hits: list[dict[str, Any]]) -> str:
    def _date(h: dict[str, Any]) -> str:
        d = str(h.get("valid_at") or h.get("temporal_anchor") or "")
        return "" if d.startswith("1970-01-01") else d[:10]

    ordered = sorted(hits, key=lambda h: _date(h) or "9999")
    return (
        "\n".join(
            f"- [{_date(h) or 'undated'}] {(h.get('content_excerpt') or '').strip()}"
            for h in ordered
        )
        or "(nothing retrieved)"
    )


@dataclass
class QResult:
    chat: str
    ability: str
    idx: int
    difficulty: str
    question: str
    answer: str
    rubric_score: float
    rubric_n: int
    event_ordering: dict[str, float] | None
    evidence_recall: float
    hits: int
    gate_mode: str = ""
    gate_dropped: int = 0
    answer_seconds: float = 0.0


async def run_chat(
    client: httpx.AsyncClient,
    a: argparse.Namespace,
    chat: Chat,
    api_key: str,
    sem: asyncio.Semaphore,
) -> tuple[list[QResult], dict[str, Any]]:
    ns = f"beam-{uuid.uuid4().hex[:10]}"
    remember_bench_namespace(a.org, ns)
    await register_bench_namespace(
        lambda p, b: _post(client, a.base_url + p, b, a.token),
        org=a.org,
        namespace=ns,
        description=f"BEAM {chat.name}",
        keep=a.keep_namespaces,
    )
    ident = {"org": a.org, "namespace": ns, "agent": "beam", "user_id": "-"}
    key_to_turns: dict[str, set[int]] = {}
    build = {"tin": 0, "tout": 0, "calls": 0, "written": 0, "seconds": 0.0}
    results: list[QResult] = []
    async with sem:
        t0 = time.perf_counter()
        for s in chat.sessions:
            body: dict[str, Any] = {
                **ident,
                "messages": s.messages[:400],
                "session_id": s.sid,
                "max_tokens": 4096,
            }
            if s.date:
                body["observation_date"] = s.date
            resp = await _post(client, a.base_url + "/v1/memory.set_from_messages", body, a.token)
            build["calls"] += 1
            build["tin"] += int(resp.get("tokens_in") or 0)
            build["tout"] += int(resp.get("tokens_out") or 0)
            written = resp.get("written") or []
            build["written"] += len(written)
            for w in written:
                k = str(w.get("memory_key") or "")
                if k:
                    key_to_turns.setdefault(k, set()).update(s.turn_ids)
        build["seconds"] = round(time.perf_counter() - t0, 1)

        last_date = next((s.date for s in reversed(chat.sessions) if s.date), "")
        for q in chat.questions:
            usage = {"tin": 0, "tout": 0, "calls": 0}
            t1 = time.perf_counter()
            search_body: dict[str, Any] = {
                **measurement_fields("search", credit=a.credit),
                **ident,
                "agent": "*",
                "query": q.question,
                "top_k": a.top_k,
                "search_mode": a.search_mode,
                "namespace_scope": "this",
            }
            if a.rerank and a.rerank != "none":
                search_body["rerank"] = a.rerank
            if last_date:
                search_body["observation_date"] = last_date
            gate_mode, gate_dropped = "", 0
            if a.answerer == "loom-ask":
                ask_body: dict[str, Any] = {
                    **measurement_fields("ask", credit=a.credit),
                    **ident,
                    "agent": "*",
                    "query": q.question,
                    "top_k": a.top_k,
                    "search_mode": a.search_mode,
                    "namespace_scope": "this",
                    "allow_synthesis": True,
                }
                if a.ask_json:
                    ask_body.update(json.loads(a.ask_json))
                ask = await _post(client, a.base_url + "/v1/memory.ask", ask_body, a.token)
                answer = (
                    str(ask.get("answer") or "").strip()
                    or "The stored memories do not contain this."
                )
                hit_keys = [str(k) for k in (ask.get("memory_keys") or [])]
                n_hits = len(ask.get("results") or [])
                gate_mode = str(ask.get("gate_mode") or "")
                gate_dropped = int(ask.get("gate_dropped") or 0)
            else:
                sr = await _post(client, a.base_url + "/v1/memory.search", search_body, a.token)
                hits = sr.get("results") or []
                n_hits = len(hits)
                hit_keys = [str(h.get("memory_key") or "") for h in hits]
                prompt = READER_PROMPT.format(
                    date=last_date or "unknown", memories=render_memories(hits), question=q.question
                )
                answer = await _chat(
                    client,
                    a.reader_model,
                    api_key,
                    [{"role": "user", "content": prompt}],
                    usage=usage,
                )
            verdicts: list[dict[str, Any] | None] = []
            for item in q.rubric:
                text = await _chat(
                    client,
                    a.judge_model,
                    api_key,
                    [
                        {
                            "role": "user",
                            "content": UNIFIED_JUDGE_PROMPT.replace("<rubric_item>", item).replace(
                                "<llm_response>", answer
                            ),
                        }
                    ],
                    json_mode=True,
                    max_tokens=300,
                    usage=usage,
                )
                verdicts.append(parse_json_object(text))
            eo: dict[str, float] | None = None
            if q.ability == "event_ordering" and q.rubric:
                ref = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(q.rubric))
                text = await _chat(
                    client,
                    a.judge_model,
                    api_key,
                    [
                        {
                            "role": "user",
                            "content": ORDER_ALIGN_PROMPT.format(reference=ref, response=answer),
                        }
                    ],
                    json_mode=True,
                    max_tokens=200,
                    usage=usage,
                )
                obj = parse_json_object(text) or {}
                order = [int(x) for x in (obj.get("order") or []) if str(x).lstrip("-").isdigit()]
                try:
                    extra = int(obj.get("extra") or 0)
                except (TypeError, ValueError):
                    extra = 0
                eo = event_ordering_score(len(q.rubric), order, extra)
            results.append(
                QResult(
                    chat=chat.name,
                    ability=q.ability,
                    idx=q.idx,
                    difficulty=q.difficulty,
                    question=q.question,
                    answer=answer,
                    rubric_score=rubric_score(verdicts),
                    rubric_n=len(verdicts),
                    event_ordering=eo,
                    evidence_recall=evidence_recall(q.source_turn_ids, hit_keys, key_to_turns),
                    hits=n_hits,
                    gate_mode=gate_mode,
                    gate_dropped=gate_dropped,
                    answer_seconds=round(time.perf_counter() - t1, 2),
                )
            )
            mark = (
                "✓"
                if results[-1].rubric_score >= 0.75
                else ("~" if results[-1].rubric_score >= 0.5 else "✗")
            )
            print(
                f"  {mark} {chat.name} {q.ability}#{q.idx} score={results[-1].rubric_score:.2f}"
                + (f" eo={eo['final_score']:.2f}" if eo else ""),
                flush=True,
            )
    return results, {"namespace": ns, "chars": chat.chars, **build}


def summarize(results: list[QResult]) -> dict[str, Any]:
    by: dict[str, list[QResult]] = {}
    for r in results:
        by.setdefault(r.ability, []).append(r)
    out: dict[str, Any] = {"questions": len(results), "abilities": {}}
    for ability in ABILITIES:
        rs = by.get(ability)
        if not rs:
            continue
        row: dict[str, Any] = {
            "n": len(rs),
            "rubric_mean": round(sum(r.rubric_score for r in rs) / len(rs), 3),
            "full_marks": sum(1 for r in rs if r.rubric_score >= 0.999),
        }
        measured = [r.evidence_recall for r in rs if r.evidence_recall >= 0]
        if measured:
            row["evidence_recall"] = round(sum(measured) / len(measured), 3)
        eos = [r.event_ordering for r in rs if r.event_ordering]
        if eos:
            row["event_ordering_final"] = round(sum(e["final_score"] for e in eos) / len(eos), 3)
            row["event_ordering_tau"] = round(sum(e["tau_norm"] for e in eos) / len(eos), 3)
        out["abilities"][ability] = row
    if results:
        out["rubric_mean"] = round(sum(r.rubric_score for r in results) / len(results), 3)
    return out


def print_summary(s: dict[str, Any]) -> None:
    print()
    print(
        f"{'ability':<26} {'n':>3} {'rubric':>7} {'full':>5} {'ev-recall':>10} {'event-order':>12}"
    )
    for ability, row in s.get("abilities", {}).items():
        er = f"{row['evidence_recall']:.2f}" if "evidence_recall" in row else "—"
        eo = f"{row['event_ordering_final']:.2f}" if "event_ordering_final" in row else "—"
        print(
            f"{ability:<26} {row['n']:>3} {row['rubric_mean']:>7.3f} {row['full_marks']:>5} {er:>10} {eo:>12}"
        )
    if "rubric_mean" in s:
        print(f"\nOVERALL rubric mean: {s['rubric_mean']:.3f} over {s['questions']} questions")


async def main_async(a: argparse.Namespace) -> int:
    data_dir = Path(a.data_dir)
    abilities = (
        tuple(x.strip() for x in a.abilities.split(",") if x.strip()) if a.abilities else ABILITIES
    )
    unknown = [x for x in abilities if x not in ABILITIES]
    if unknown:
        print(f"unknown abilities: {unknown}; known: {', '.join(ABILITIES)}", file=sys.stderr)
        return 2
    async with httpx.AsyncClient() as client:
        if a.fetch:
            n = await fetch_chats(client, data_dir, a.fetch)
            print(f"fetched {n} files into {data_dir}")
        chats = load_chats(data_dir, a.scale, a.limit, abilities)
        if not chats:
            print(
                f"no chats under {data_dir / a.scale} — use --fetch 100K:1,2 or clone the BEAM repo",
                file=sys.stderr,
            )
            return 2
        for c in chats:
            print(
                f"{c.name}: {len(c.sessions)} sessions, {c.chars:,} chars, {len(c.questions)} questions"
            )
        if a.dry:
            return 0
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("OPENAI_API_KEY is required for the reader and judge", file=sys.stderr)
            return 2
        if not a.base_url:
            print("--base-url is required (or --dry)", file=sys.stderr)
            return 2
        sem = asyncio.Semaphore(max(1, a.concurrency))

        async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            return await _post(client, a.base_url + path, body, a.token)

        async with erase_on_exit(post, org=a.org, keep=a.keep_namespaces):
            outs = await asyncio.gather(*(run_chat(client, a, c, api_key, sem) for c in chats))
    results = [r for rs, _ in outs for r in rs]
    builds = [b for _, b in outs]
    summary = summarize(results)
    summary["build"] = {
        "chats": len(builds),
        "chars": sum(b["chars"] for b in builds),
        "memories_written": sum(b["written"] for b in builds),
        "tokens_in": sum(b["tin"] for b in builds),
        "calls": sum(b["calls"] for b in builds),
        "seconds": round(sum(b["seconds"] for b in builds), 1),
    }
    summary["config"] = {
        "scale": a.scale,
        "top_k": a.top_k,
        "search_mode": a.search_mode,
        "rerank": a.rerank,
        "answerer": a.answerer,
        "ask_json": a.ask_json,
        "reader_model": a.reader_model,
        "judge_model": a.judge_model,
        "credit": a.credit,
    }
    print_summary(summary)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(
            json.dumps({"summary": summary, "results": [r.__dict__ for r in results]}, indent=1)
        )
        print(f"wrote {a.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--data-dir",
        required=True,
        help="directory holding <scale>/<n>/chat.json (a BEAM checkout's chats/)",
    )
    ap.add_argument(
        "--fetch", default="", help="pull chats first, e.g. '100K:1,2,3' or '100K:1;500K:2'"
    )
    ap.add_argument("--scale", default="100K", help="100K | 500K | 1M | 10M")
    ap.add_argument("--limit", type=int, default=0, help="chats to run (0 = all present)")
    ap.add_argument("--abilities", default="", help="comma list; default all ten")
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument("--search-mode", default="rrf")
    ap.add_argument("--rerank", default="builtin:openai")
    ap.add_argument("--answerer", choices=["reader", "loom-ask"], default="reader")
    ap.add_argument(
        "--ask-json",
        default="",
        help='memory.ask overrides for --answerer loom-ask, e.g. \'{"depth": "raw"}\'',
    )
    ap.add_argument("--reader-model", default=_DEFAULT_READER)
    ap.add_argument("--judge-model", default=_DEFAULT_JUDGE)
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase this run's beam-* namespaces when it ends",
    )
    ap.add_argument(
        "--credit",
        action="store_true",
        help="send the server's crediting defaults (search auto_touch, ask reinforce) "
        "instead of the evaluation-read fields",
    )
    ap.add_argument(
        "--dry", action="store_true", help="load and count; no network to loom or OpenAI"
    )
    a = ap.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
