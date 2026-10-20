"""Session outcomes and contrast-distilled reflections.

Two passes, both offline, both default-off, both bounded per pass:

  1. LABEL. Every recent session summary that carries no outcome yet is read
     by a cheap model and labelled positive / negative / unclear with a
     confidence and a one-line reason, into `loom.session_outcomes_by_user`
     (source="judged"). A client that closes a run with an outcome writes
     the same row itself (source="client") and wins under FINAL.

  2. CONTRAST. Every confidently negative session that has no reflection yet
     is paired with its nearest confidently positive neighbour in the same
     namespace (cosine over the summary embeddings, above a floor), and one
     model call is asked what the failed session missed that the successful
     one did — derived from the two texts, nothing invented. The lessons land
     as ONE `reflection` memory keyed `reflection:contrast:<session_id>`,
     with `supports` (the positive session) and `contradicts` (the negative
     one) so the lesson carries its witnesses. Reflections stay hidden from
     unfiltered recall; this pass fills the tier a client can also write.

Deliberately absent: no blanket credit to everything shown, no merge that resets
scores, no hard delete. The label is what closes the loop: a coding session has
no environment reward, so its outcome has to be read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.llm import customer
from server.memory.api.models_crud import MemorySetInput
from server.memory.domain.sensitivity import value_reads_high
from server.memory.store.session_outcomes import insert_outcomes
from server.observability.genai import llm_stage

OUTCOMES = ("unclear", "positive", "negative")
REFLECTION_KEY_PREFIX = "reflection:contrast:"

_JUDGE_SYSTEM = """You read a short digest of one agent session and decide how it ended.
Answer with JSON only:
{"outcome": "positive" | "negative" | "unclear", "confidence": 0.0-1.0, "reason": "<one sentence>"}
positive = the session accomplished what it set out to do;
negative = it failed, gave up, or left the goal unmet;
unclear = the digest does not say.
Judge only from the digest. Do not reward effort; reward the goal being met."""

_JUDGE_USER = """## Session digest
{summary}

## Key facts recorded
{facts}

Your JSON:"""

_CONTRAST_SYSTEM = """You are an analysis-driven agent focused on learning from experience.
You will be given a FAILED session digest with the reason it was judged failed,
and a SUCCESSFUL session digest on a similar task.
Your task is to say what the failed session missed and how the successful one
addressed or avoided it.
Requirements:
- Every lesson must be derived directly from contrasting the two digests.
  Do not speculate or introduce steps neither digest supports.
- Focus on concrete behavioural or strategic differences, stated so a future
  agent can act on them.
- Do not mention the sessions themselves; lessons must be generally applicable.
- Keep each lesson to one sentence. At most 4 lessons.
Answer with JSON only: {"lessons": ["...", "..."], "what_failed": "<one sentence>"}."""

_CONTRAST_USER = """## Failed session
### Why it was judged failed
{neg_reason}
### Digest
{neg_summary}

## Successful session on a similar task
### Digest
{pos_summary}

Your JSON:"""


@dataclass(frozen=True)
class DistillConfig:
    lookback_days: int = 14
    max_judged_per_pass: int = 40
    max_contrasts_per_pass: int = 10
    min_confidence: float = 0.7
    min_pair_similarity: float = 0.55
    model: str = ""


@dataclass
class DistillResult:
    sessions_scanned: int = 0
    labelled: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    contrast_candidates: int = 0
    reflections_written: int = 0
    unpaired: int = 0
    failures: list[str] = field(default_factory=list)


# --------------------------------------------------------------- prompts / parse


def judge_prompt(summary: str, key_facts: list[str]) -> tuple[str, str]:
    facts = (
        # A digest an earlier build wrote may still hold a credential among its key facts.
        "\n".join(
            f"- {str(f).strip()}"
            for f in (key_facts or [])
            if str(f).strip() and not value_reads_high(f)
        )
        or "- (none)"
    )
    return _JUDGE_SYSTEM, _JUDGE_USER.format(
        summary=str(summary or "").strip()[:4000], facts=facts[:2000]
    )


def parse_judgement(text: str) -> tuple[str, float, str]:
    """(outcome, confidence, reason) from the model's JSON; anything the model
    got wrong reads as unclear at 0 confidence — never as a label."""
    obj = _loads(text)
    outcome = str(obj.get("outcome") or "").strip().lower()
    if outcome not in OUTCOMES:
        outcome = "unclear"
    try:
        confidence = max(0.0, min(1.0, float(obj.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    if outcome == "unclear":
        confidence = 0.0
    reason = str(obj.get("reason") or "").strip()[:500]
    return outcome, confidence, reason


def contrast_prompt(neg_summary: str, neg_reason: str, pos_summary: str) -> tuple[str, str]:
    return _CONTRAST_SYSTEM, _CONTRAST_USER.format(
        neg_reason=str(neg_reason or "").strip()[:500] or "(no reason recorded)",
        neg_summary=str(neg_summary or "").strip()[:4000],
        pos_summary=str(pos_summary or "").strip()[:4000],
    )


def parse_lessons(text: str) -> tuple[list[str], str]:
    """(lessons, what_failed). Lessons are de-duplicated, trimmed to one line
    each, at most 4; an empty list means nothing was learned and no
    reflection is written."""
    obj = _loads(text)
    raw = obj.get("lessons")
    if isinstance(raw, str):
        raw = [ln for ln in re.split(r"\n+", raw)]
    lessons: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        s = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", str(item)).strip()
        s = " ".join(s.split())[:400]
        # Identity for the de-dup ignores case and closing punctuation: the
        # model restating a lesson with a period is not a second lesson.
        ident = re.sub(r"[\s.!;:]+$", "", s.lower())
        if s and ident and ident not in seen:
            seen.add(ident)
            lessons.append(s)
        if len(lessons) >= 4:
            break
    what_failed = " ".join(str(obj.get("what_failed") or "").split())[:500]
    return lessons, what_failed


def _loads(text: str) -> dict[str, Any]:
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", s, flags=re.S)
        if not m:
            return {}
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}


def build_outcome_row(
    *,
    org: str,
    namespace: str,
    session_id: str,
    outcome: str,
    confidence: float,
    reason: str,
    source: str,
    agent: str = "-",
    user_id: str = "-",
    run_id: str = "-",
    now: datetime | None = None,
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if source not in ("client", "judged"):
        raise ValueError(f"source must be client|judged, got {source!r}")
    ts = (now or datetime.now(UTC)).replace(tzinfo=None, microsecond=0)
    return {
        "org": org,
        "namespace": namespace,
        "agent": agent or "-",
        "user_id": user_id or "-",
        "session_id": session_id,
        "run_id": run_id or "-",
        "outcome": outcome,
        "confidence": float(max(0.0, min(1.0, confidence))),
        "reason": str(reason or "")[:500],
        "source": source,
        "judged_at": ts,
        "version": int(ts.replace(tzinfo=UTC).timestamp() * 1_000_000_000),
    }


async def record_outcome(ch: Any, **fields: Any) -> None:
    """Write one session's outcome label (`build_outcome_row`'s fields)."""
    await insert_outcomes([build_outcome_row(**fields)], ch=ch)


# ------------------------------------------------------------------- pass 1

_UNLABELLED_SQL = """
    SELECT s.org AS org, s.namespace AS namespace, s.agent AS agent,
           s.user_id AS user_id, s.session_id AS session_id,
           s.summary_text AS summary_text, s.key_facts AS key_facts
    FROM loom.session_summaries AS s FINAL
    LEFT ANTI JOIN (
        SELECT org, namespace, agent, user_id, session_id FROM loom.session_outcomes_by_user FINAL
    ) AS o ON o.org = s.org AND o.namespace = s.namespace AND o.agent = s.agent
          AND o.user_id = s.user_id AND o.session_id = s.session_id
    WHERE s.tombstoned_at IS NULL
      AND s.created_at > now() - INTERVAL {days:UInt16} DAY
      AND length(s.summary_text) > 0
    ORDER BY s.created_at DESC
    LIMIT {lim:UInt32}
"""


async def label_sessions(ch: Any, llm: Any, cfg: DistillConfig, result: DistillResult) -> None:
    rows = await ch.query_all(
        _UNLABELLED_SQL,
        params={"days": int(cfg.lookback_days), "lim": int(cfg.max_judged_per_pass)},
    )
    result.sessions_scanned = len(rows)
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        if await customer.switched_off(str(r["org"])):
            continue  # judged once the org's model calls are switched back on
        try:
            system, user = judge_prompt(
                str(r.get("summary_text") or ""), list(r.get("key_facts") or [])
            )
            with llm_stage("session_judge"), customer.for_org(str(r["org"])):
                res = await llm.complete(
                    system, user, max_tokens=160, response_format={"type": "json_object"}
                )
            outcome, confidence, reason = parse_judgement(getattr(res, "text", "") or "")
        except Exception as e:  # noqa: BLE001 — one bad judgement never stops the pass
            result.failures.append(f"judge {r.get('session_id')}: {type(e).__name__}: {e}")
            continue
        out_rows.append(
            build_outcome_row(
                org=str(r["org"]),
                namespace=str(r["namespace"]),
                agent=str(r.get("agent") or "-"),
                user_id=str(r.get("user_id") or "-"),
                session_id=str(r["session_id"]),
                outcome=outcome,
                confidence=confidence,
                reason=reason,
                source="judged",
            )
        )
        result.by_outcome[outcome] = result.by_outcome.get(outcome, 0) + 1
    if out_rows:
        await insert_outcomes(out_rows, ch=ch)
        result.labelled = len(out_rows)


# ------------------------------------------------------------------- pass 2

_NEGATIVE_SQL = """
    SELECT o.org AS org, o.namespace AS namespace, o.agent AS agent,
           o.session_id AS session_id, o.reason AS reason, o.confidence AS confidence,
           s.summary_text AS summary_text, s.summary_embedding AS summary_embedding,
           s.embedding_model AS embedding_model, s.user_id AS user_id
    FROM loom.session_outcomes_by_user AS o FINAL
    INNER JOIN (
        SELECT org, namespace, agent, session_id, summary_text, summary_embedding,
               embedding_model, user_id
        FROM loom.session_summaries FINAL
        WHERE tombstoned_at IS NULL AND embedding_dim > 0
    ) AS s ON s.org = o.org AND s.namespace = o.namespace AND s.agent = o.agent
          AND s.user_id = o.user_id AND s.session_id = o.session_id
    LEFT ANTI JOIN (
        SELECT org, namespace, agent, user_id,
               substring(memory_key, {plen:UInt16} + 1) AS session_id
        FROM loom.memory FINAL
        WHERE memory_type = 'reflection' AND startsWith(memory_key, {prefix:String})
          AND tombstoned_at IS NULL
    ) AS r ON r.org = o.org AND r.namespace = o.namespace AND r.agent = s.agent
          AND r.user_id = s.user_id AND r.session_id = o.session_id
    WHERE o.outcome = 'negative' AND o.confidence >= {minc:Float32}
      AND o.judged_at > now() - INTERVAL {days:UInt16} DAY
    ORDER BY o.judged_at DESC
    LIMIT {lim:UInt32}
"""

_NEAREST_POSITIVE_SQL = """
    SELECT s.session_id AS session_id, s.summary_text AS summary_text,
           1 - cosineDistance(s.summary_embedding, {q:Array(Float32)}) AS similarity
    FROM loom.session_summaries AS s FINAL
    INNER JOIN (
        SELECT org, namespace, agent, user_id, session_id FROM loom.session_outcomes_by_user FINAL
        WHERE outcome = 'positive' AND confidence >= {minc:Float32}
    ) AS o ON o.org = s.org AND o.namespace = s.namespace AND o.agent = s.agent
          AND o.user_id = s.user_id AND o.session_id = s.session_id
    WHERE s.org = {org:String} AND s.namespace = {ns:String}
      AND s.user_id = {uid:String}
      AND s.tombstoned_at IS NULL AND s.embedding_dim > 0
      AND s.embedding_model = {model:String} AND s.session_id != {sid:String}
    ORDER BY similarity DESC
    LIMIT 1
"""


async def write_contrast_reflection(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    neg_session: str,
    pos_session: str,
    lessons: list[str],
    what_failed: str,
    confidence: float,
    pair_similarity: float,
) -> str:
    """One `reflection` memory per contrasted failed session, through the
    normal memory.set path (blacklist, embedding, provenance) under the
    session's identity. Returns the memory_key."""
    from server.memory.app.write import handle_memory_set

    key = f"{REFLECTION_KEY_PREFIX}{neg_session}"
    value: dict[str, Any] = {
        "source": "contrast",
        "lessons": lessons,
        "what_failed": what_failed,
        "supports": [pos_session],
        "contradicts": [neg_session],
        "pair_similarity": round(float(pair_similarity), 4),
    }
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent=agent or "-",
            user_id=user_id or "-",
            session_id=neg_session,
            memory_key=key,
            memory_type="reflection",
            value=value,
            tags=[
                "reflection",
                "contrast",
                f"session:{neg_session}",
                f"session:{pos_session}",
                "status:failed",
            ],
            confidence=float(max(0.0, min(1.0, confidence))),
            source_trace=f"contrast:{neg_session}<{pos_session}",
        )
    )
    return key


async def distil_contrasts(
    ch: Any, llm: Any, cfg: DistillConfig, result: DistillResult, *, writer: Any = None
) -> None:
    writer = writer or write_contrast_reflection
    negatives = await ch.query_all(
        _NEGATIVE_SQL,
        params={
            "plen": len(REFLECTION_KEY_PREFIX),
            "prefix": REFLECTION_KEY_PREFIX,
            "minc": float(cfg.min_confidence),
            "days": int(cfg.lookback_days),
            "lim": int(cfg.max_contrasts_per_pass),
        },
    )
    result.contrast_candidates = len(negatives)
    for neg in negatives:
        if await customer.switched_off(str(neg["org"])):
            continue
        sid = str(neg.get("session_id") or "")
        try:
            # The pair's LLM call and the reflection's write (which embeds) are
            # both that org's.
            with customer.for_org(str(neg["org"])):
                written = await _contrast_pair(ch, llm, cfg, neg, writer)
        except Exception as e:  # noqa: BLE001 — one bad pair never stops the pass
            result.failures.append(f"contrast {sid}: {type(e).__name__}: {e}")
            continue
        if written:
            result.reflections_written += 1
        else:
            result.unpaired += 1


async def _contrast_pair(ch: Any, llm: Any, cfg: DistillConfig, neg: Any, writer: Any) -> bool:
    """One failed session against its nearest success: True when a reflection
    was written, False when there is no close pair or no lesson."""
    sid = str(neg.get("session_id") or "")
    vec = [float(x) for x in (neg.get("summary_embedding") or [])]
    pos = await ch.query_one(
        _NEAREST_POSITIVE_SQL,
        params={
            "q": vec,
            "minc": float(cfg.min_confidence),
            "org": str(neg["org"]),
            "ns": str(neg["namespace"]),
            "model": str(neg.get("embedding_model") or ""),
            "sid": sid,
            # the positive's summary is handed to the LLM and its lessons
            # land in this user's memory: never another user's session
            "uid": str(neg.get("user_id") or "-"),
        },
    )
    sim = float((pos or {}).get("similarity") or 0.0)
    if not pos or sim < float(cfg.min_pair_similarity):
        return False
    system, user = contrast_prompt(
        str(neg.get("summary_text") or ""),
        str(neg.get("reason") or ""),
        str(pos.get("summary_text") or ""),
    )
    with llm_stage("session_contrast"):
        res = await llm.complete(
            system, user, max_tokens=400, response_format={"type": "json_object"}
        )
    lessons, what_failed = parse_lessons(getattr(res, "text", "") or "")
    if not lessons:
        return False
    await writer(
        org=str(neg["org"]),
        namespace=str(neg["namespace"]),
        agent=str(neg.get("agent") or "-"),
        user_id=str(neg.get("user_id") or "-"),
        neg_session=sid,
        pos_session=str(pos.get("session_id") or ""),
        lessons=lessons,
        what_failed=what_failed or str(neg.get("reason") or ""),
        confidence=min(float(neg.get("confidence") or 0.0), sim),
        pair_similarity=sim,
    )
    return True


async def run_distillation(
    cfg: DistillConfig | None = None, *, ch: Any = None, llm: Any = None, writer: Any = None
) -> DistillResult:
    cfg = cfg or DistillConfig()
    result = DistillResult()
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
        # In-server the singleton is connected at lifespan; a one-off pass run
        # from a shell (docker exec …) starts cold.
        if getattr(ch, "_client", None) is None:
            await ch.connect()
    if llm is None:
        from server.llm.client import get_llm_client_for_model

        llm = get_llm_client_for_model(cfg.model or None)
    await label_sessions(ch, llm, cfg, result)
    await distil_contrasts(ch, llm, cfg, result, writer=writer)
    return result


__all__ = [
    "OUTCOMES",
    "REFLECTION_KEY_PREFIX",
    "DistillConfig",
    "DistillResult",
    "build_outcome_row",
    "contrast_prompt",
    "distil_contrasts",
    "judge_prompt",
    "label_sessions",
    "parse_judgement",
    "parse_lessons",
    "run_distillation",
    "write_contrast_reflection",
]
