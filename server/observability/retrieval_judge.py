"""Independent LLM judging of a sample of real searches.

Every other quality score loom writes to Langfuse is loom grading itself:
`retrieval_confidence` is its own calibrated confidence, `top_similarity` its own
cosine. That is circular in a way that can hide the exact failure it exists to
catch — confidence is calibrated against the *current* embedder's distance
distribution, so a degraded embedder drags the measurement and the yardstick down
together and the panel stays flat.

This adds the missing axis: a judge that sees only the query and the returned
memory text, and rates whether they actually answer it. It shares no state with
the retrieval path — not the embedder, not the thresholds, not the confidence
calibration — so the two scores can disagree, which is the entire point.

Constraints, all of which shape the design:

* **Off by default.** `retrieval_judge_sample_rate` is 0.0. This spends real
  money per judged search, so it must be switched on deliberately, not inherited
  by a deployment that upgraded.
* **Never on the request path.** Judging happens on a background task after the
  response has been sent, on a bounded queue.
* **Its own cost is attributed.** The judge is itself an LLM call; it binds a cost
  accumulator and emits a worker cost event under `worker.retrieval_judge`, so
  the observability spend is visible as spend rather than quietly inflating the
  namespace whose search triggered it.
* **Never judges its own judging.** The judge call is not sampled, so there is no
  recursion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from server.core.llm_free import reads_llm_free
from server.observability.worker_queue import LoopBoundQueue
from server.settings import settings

log = logging.getLogger(__name__)

SCORE_RELEVANCE = "retrieval_relevance"
SCORE_JUDGE_VERDICT = "judge_verdict"

# Names how `relevance` is DERIVED. Bump when the scheme changes, not when the
# prompt's wording is tweaked. Stamped on every metric point so a scheme change
# cannot silently average against another scheme's points.
JUDGE_SCHEME = "derived-on-target-weighted"

# Small: this is a money-spending queue, and a deep backlog would judge searches
# whose context has long since scrolled past.
_MAX_QUEUE = 200

_judged = 0
_dropped = 0
_dropped_shutdown = 0

_JUDGE_SYSTEM = """\
You grade a memory-retrieval system. You are given a user's QUERY and the \
memory excerpts the system RETURNED.

The QUERY is often a TOPIC or keyword phrase, not a question — "grace hash \
join bucket spilling", "secret masking in EXPLAIN output". Read it as "find me \
what is known about this", and judge whether someone looking for that topic \
got what they were looking for. Do NOT require that the excerpts form a \
complete answer to a question the user did not ask.

Ignore style, length, formatting and duplication.

Do not score. Report two observable facts and let the caller do the arithmetic:

Return strict JSON, no prose:
{"on_target": <true|false>, "useful": [<1-based excerpt numbers>], \
"reason": "<max 20 words>"}

  on_target — would someone searching for this QUERY consider that they found
              what they were looking for? True when an excerpt is about the
              thing asked about, even if it does not resolve every detail.
              False when the excerpts are about something else, or only
              share generic vocabulary with the query.
  useful    — every excerpt about the thing asked about. Empty if none are.
"""
# Why facts instead of a score: a 0.0-1.0 scale with worked anchors collapses
# onto the anchors (the model reads them as an enumeration and picks one), so
# the metric has no variance and a quality-collapse alert reading it can never
# fire. `on_target` and `useful` are claims about the excerpts that a human can
# check, and the score below is computed from them, so its spread comes from the
# data rather than from the model's willingness to use a range.
#
# The question is "did they find what they were looking for", not "could someone
# ANSWER this": loom's queries are topics, so asking for an answer is ill-posed
# and the safe reply is false.


def _relevance_from(answered: bool, useful: int, total: int) -> float:
    """Derive relevance from the judge's two factual claims.

    `answered` carries the weight; `useful / total` only distinguishes a tight
    answer from one buried in filler. It must stay a modifier, because `total` is
    the caller's `top_k` — a query with exactly one matching memory cannot do
    better than 1/top_k however good retrieval is.
    """
    if total <= 0:
        return 0.0
    frac = max(0.0, min(1.0, useful / total))
    if answered:
        # Answerable is nearly all of the value; precision is a modifier.
        #
        # Weighted 0.80/0.20: in a corpus where each query has exactly one
        # matching memory, the other hits are the CALLER's top_k, not a
        # retrieval failure, and a heavier precision weight (0.60 + 0.40/top_k)
        # would cap a perfectly answered query well below the answered band.
        # Precision still earns the top 0.20, so a tight answer beats one
        # buried in filler.
        return round(0.80 + 0.20 * frac, 4)
    if useful == 0:
        return 0.0
    # Related, insufficient. Capped well below the answered band so the two
    # cannot overlap — any threshold in (0.50, 0.80) means exactly
    # "not answerable".
    return round(0.10 + 0.40 * frac, 4)


# Review payload caps. A reviewer needs to see what the judge saw; they do not
# need the whole corpus, and this text goes to a third party.
_REVIEW_QUERY_CHARS = 200
_REVIEW_EXCERPT_CHARS = 300
_REVIEW_EXCERPTS = 5


async def _review_payload(
    query: str, excerpts: list[str], org: str, namespace: str
) -> dict[str, Any]:
    """What the judge saw, for a human to check its score against — or {}.

    `memory.search` traces carry no captured input or output (loom
    deliberately keeps query text off spans by default), so without this a
    reviewer opening a queued trace in Langfuse would see a span tree and no
    query and no memories: nothing to annotate against. The judge is the
    only place that holds the query and the exact excerpts it graded, so it
    stamps a bounded copy onto its own score.

    Gated on the SAME per-namespace consent as generation content, because this
    is the same data by another route:

        "all"   -> query + excerpts (a reviewer can check the judgement)
        "query" -> query only (they can see what was asked, not what came back)
        "off"   -> nothing

    Fails closed with the consent module, so an unresolvable namespace stamps
    nothing rather than defaulting to disclosure.
    """
    try:
        from server.observability.capture_consent import resolve

        level = await resolve(org, namespace)
    except Exception:  # noqa: BLE001 — fail CLOSED: no consent answer means no review payload
        return {}
    if level == "off":
        return {}
    out: dict[str, Any] = {"review_query": query[:_REVIEW_QUERY_CHARS]}
    if level == "all":
        out["review_excerpts"] = [e[:_REVIEW_EXCERPT_CHARS] for e in excerpts[:_REVIEW_EXCERPTS]]
    return out


def enabled() -> bool:
    from server.observability import langfuse_scores

    return bool(settings.retrieval_judge_sample_rate > 0.0 and langfuse_scores.enabled())


def _sampled(trace_id: str) -> bool:
    """Deterministic on the trace id, like the span and score gates.

    Uses a DIFFERENT bit range than span sampling on purpose: keying both on the
    low 64 bits would make the judged set a strict subset of the exported set in a
    way that correlates the two samples. Judging is about measuring the whole
    population, so it gets its own independent slice.
    """
    rate = settings.retrieval_judge_sample_rate
    rate = 0.0 if rate < 0 else (1.0 if rate > 1 else float(rate))
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    try:
        high = (int(trace_id, 16) >> 64) & 0xFFFFFFFFFFFFFFFF
    except ValueError:
        return False
    return high < int(rate * (1 << 64))


def _excerpts(hits: list[Any], limit: int) -> list[str]:
    """Bounded excerpt text from search hits, newest field names tolerated."""
    out: list[str] = []
    for h in hits[:limit]:
        get = h.get if isinstance(h, dict) else lambda k, d=None, _h=h: getattr(_h, k, d)
        text = ""
        # Memory hits: value/content/excerpt. Code units (code.search): body,
        # else description/signature when the body was elided under budget.
        # Without these the judge would see every code.search as "no results".
        for field in (
            "value",
            "content",
            "content_excerpt",
            "excerpt",
            "text",
            "body",
            "description",
            "signature",
        ):
            v = get(field)
            if isinstance(v, str) and v.strip():
                text = v
                break
        # A code unit whose body was elided under budget still has an identity
        # (path, span, symbol). Lead with the location so it can
        # grade reachability even when the text is thin.
        path = get("path")
        if isinstance(path, str) and path:
            where = f"{path}:{get('start_line') or 0}-{get('end_line') or 0}"
            sym = str(get("symbol") or "")
            head = f"{where} {sym}".strip()
            text = f"{head}\n{text}" if text else head
        if text:
            out.append(text[:600])
    return out


def submit(
    *,
    trace_id: str,
    query: str,
    hits: list[Any],
    org: str,
    namespace: str,
) -> bool:
    """Queue one search for judging. Returns True if it was queued.

    Synchronous and non-blocking: the caller is inside a request.
    """
    global _dropped

    if not enabled() or not trace_id or not query or reads_llm_free():
        return False
    if not _sampled(trace_id):
        return False
    q = _q.ensure()
    if q is None:
        return False
    item = {
        "trace_id": trace_id,
        "query": query[:1000],
        # Only the top few: the judge is rating whether the ANSWER is reachable,
        # and paying for 50 excerpts to ask that would cost more than the search.
        "excerpts": _excerpts(hits, 5),
        "org": org,
        "namespace": namespace,
    }
    try:
        q.put_nowait(item)
        return True
    except asyncio.QueueFull:
        _dropped += 1
        if _dropped in (1, 10, 100) or _dropped % 500 == 0:
            log.warning("retrieval judge queue full; dropped %d searches", _dropped)
        return False


def _parse(text: str, total_excerpts: int) -> tuple[float, str, str] | None:
    """`(relevance, verdict, reason)` from the judge's reply, or None.

    `on_target` must be a real bool. A missing or non-boolean value is a parse
    failure rather than a default: a defaulted field lets every judgement
    collapse onto one value without anyone noticing.
    """
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        data = json.loads(text[start:end])
    except (ValueError, TypeError):
        return None
    answered = data.get("on_target")
    if not isinstance(answered, bool):
        return None
    raw_useful = data.get("useful")
    if not isinstance(raw_useful, list):
        return None
    # Count distinct in-range excerpt numbers only: a model that repeats an index
    # or invents excerpt 9 of 5 must not inflate the precision term.
    useful = len(
        {
            int(i)
            for i in raw_useful
            if isinstance(i, (int, float))
            and not isinstance(i, bool)
            and 1 <= int(i) <= total_excerpts
        }
    )
    relevance = _relevance_from(answered, useful, total_excerpts)
    if answered:
        verdict = "answers"
    elif useful > 0:
        verdict = "partial"
    else:
        verdict = "off_topic"
    return relevance, verdict, str(data.get("reason") or "")[:200]


async def _judge_one(item: dict[str, Any]) -> None:
    from server.llm.client import get_llm_client, get_llm_client_for_model
    from server.observability import cost_accumulator
    from server.observability import langfuse_scores as _scores
    from server.observability.genai import llm_stage, set_identity

    excerpts = item["excerpts"]
    if not excerpts:
        # An empty result set is a legitimate 0 — and needs no LLM call to know.
        _scores.post_score(
            trace_id=item["trace_id"],
            name=SCORE_RELEVANCE,
            value=0.0,
            data_type="NUMERIC",
            comment="no results returned",
            metadata={"scheme": JUDGE_SCHEME},
        )
        return

    user = (
        "QUERY:\n"
        + item["query"]
        + "\n\nRETURNED:\n"
        + "\n---\n".join(f"[{i}] {e}" for i, e in enumerate(excerpts, start=1))
    )

    model = settings.retrieval_judge_model.strip()
    client = get_llm_client_for_model(model) if model else get_llm_client()

    # The judge's own spend is attributed to a worker op rather than left on the
    # namespace whose search triggered it — observability cost is an operating
    # cost, not the tenant's.
    # The judge prompt IS the namespace's query and memory text, so it rides the
    # judged namespace's consent. With no explicit level `set_identity` falls back
    # to the operator default, which would override a namespace's opt-out. Fails
    # closed like the transports: unresolvable consent captures nothing.
    try:
        from server.observability import capture_consent as _consent

        capture_level = await _consent.resolve(item["org"], item["namespace"])
    except Exception:  # noqa: BLE001 — fail closed, not open
        capture_level = "off"
    set_identity(
        org=item["org"],
        namespace=item["namespace"],
        operation="worker.retrieval_judge",
        capture_level=capture_level,
    )
    started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        with llm_stage("classify"):
            out = await client.complete(
                _JUDGE_SYSTEM, user, max_tokens=120, response_format={"type": "json_object"}
            )
    finally:
        from server.workers.cost_event import emit_worker_cost_event_for

        emit_worker_cost_event_for(
            org=item["org"],
            namespace=item["namespace"],
            operation="worker.retrieval_judge",
            started=started,
        )

    parsed = _parse(getattr(out, "text", "") or "", len(excerpts))
    if parsed is None:
        log.warning("retrieval judge returned unparseable JSON; skipping score")
        return
    relevance, verdict, reason = parsed
    # Metric as well as score: the alert that pages on quality collapse must not
    # depend on Langfuse being reachable.
    try:
        from server.observability import metrics as _m

        _m.record_judged_relevance(
            relevance=relevance,
            verdict=verdict,
            namespace=item["namespace"],
            scheme=JUDGE_SCHEME,
        )
    except Exception:  # noqa: BLE001 — the metric is additive to the score
        pass
    # `scheme` on the score, not just on the metric: the human-review queue is
    # built from Langfuse scores, so without it a reviewer calibrates against
    # retired schemes.
    meta: dict[str, Any] = {"scheme": JUDGE_SCHEME}
    meta.update(await _review_payload(item["query"], excerpts, item["org"], item["namespace"]))
    _scores.post_score(
        trace_id=item["trace_id"],
        name=SCORE_RELEVANCE,
        value=relevance,
        data_type="NUMERIC",
        comment=reason,
        metadata=meta,
    )
    _scores.post_score(
        trace_id=item["trace_id"],
        name=SCORE_JUDGE_VERDICT,
        value=verdict,
        data_type="CATEGORICAL",
        comment=reason,
        metadata=meta,
    )


async def _judge_counted(item: dict[str, Any]) -> None:
    global _judged
    await _judge_one(item)
    _judged += 1


_q = LoopBoundQueue(name="retrieval-judge", maxsize=_MAX_QUEUE, handle=_judge_counted)


async def stop() -> None:
    """Shutdown: stop the worker without judging what is queued. A judgement
    is a sampled, paid LLM call; holding shutdown on up to `_MAX_QUEUE` of
    them buys nothing, so the backlog is counted in `dropped_shutdown`. Runs
    in the lifespan before the score queue drains and the HTTP clients close."""
    global _dropped_shutdown
    q = _q.queue
    await _q.stop_worker()
    if q is not None and q.qsize():
        _dropped_shutdown += q.qsize()
        log.warning("retrieval judge: %d queued judgements dropped at shutdown", q.qsize())
    _q.reset()


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "sample_rate": settings.retrieval_judge_sample_rate,
        "queued": _q.qsize(),
        "judged": _judged,
        "dropped_queue_full": _dropped,
        "dropped_shutdown": _dropped_shutdown,
        "worker_running": _q.running(),
    }


__all__ = [
    "SCORE_JUDGE_VERDICT",
    "SCORE_RELEVANCE",
    "enabled",
    "status",
    "stop",
    "submit",
]
