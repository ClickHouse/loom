"""Retrieval quality as Langfuse scores.

Loom computes every number a reviewer would want about a search — calibrated
confidence, best cosine similarity, whether it abstained and why — and
`record_search_quality` is called on both transports with all of it in hand.
This module writes them as Langfuse scores, which
`server/insights/app/admin_routes.py` reads to gate `prompts.evaluate`.

Design constraints that shape everything here:

* **Never on the request path.** A search must not wait on, or fail because of,
  a third-party HTTP call. Scores are enqueued synchronously into a bounded
  queue and posted by a background task.
* **Never unbounded.** A queue that grows when Langfuse is slow is a memory leak
  with extra steps. The queue drops oldest-first and counts what it dropped.
* **Consistent with span sampling.** A score whose trace was never forwarded is
  a dangling row that shows up in the API and in no UI. The same trace-id gate
  the span processor uses decides here too, so scores and spans agree.
* **Behind a breaker**, so a Langfuse outage degrades to counted drops rather
  than retry storms. `/v1/health?include=langfuse` reports it as `circuit`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from server.observability.worker_queue import LoopBoundQueue
from server.settings import settings

log = logging.getLogger(__name__)

# Score names. These are the contract with `prompts.evaluate`'s `rubric`
# parameter (`server/insights/app/admin_routes.py`), which reads the score list by
# `traceId` and `name=<rubric>` — so a rename here silently breaks
# the auto-promote gate rather than failing loudly.
SCORE_CONFIDENCE = "retrieval_confidence"
SCORE_TOP_SIMILARITY = "top_similarity"
SCORE_ABSTAINED = "abstained"
SCORE_ABSTAIN_REASON = "abstain_reason"

# Bounded, and small. At 1 score/search x 4 scores this holds ~8s of a heavy
# 250 rps burst; beyond that the oldest are dropped, because a stale quality
# score is worth less than a bounded process.
_MAX_QUEUE = 2000

_dropped_full = 0
_dropped_breaker_open = 0
_dropped_shutdown = 0
_posted = 0
_failed = 0


# The breaker: after `langfuse_circuit_failure_threshold` consecutive failed
# POSTs it opens and scores are dropped for `langfuse_circuit_cooldown_s`, then
# one probe is let through (HALF_OPEN) and its outcome closes or reopens it.
_circuit_state: str = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
_circuit_consecutive_failures = 0
_circuit_opened_at: float = 0.0


def _circuit_allow() -> bool:
    """True if a POST should proceed."""
    global _circuit_state
    if _circuit_state == "OPEN":
        cooldown = max(1.0, float(settings.langfuse_circuit_cooldown_s))
        if (time.monotonic() - _circuit_opened_at) <= cooldown:
            return False
        _circuit_state = "HALF_OPEN"
        log.info("langfuse breaker: cooldown elapsed, entering HALF_OPEN")
    return True


def _circuit_record(success: bool) -> None:
    global _circuit_state, _circuit_consecutive_failures, _circuit_opened_at
    if success:
        if _circuit_state != "CLOSED":
            log.info("langfuse breaker: closing (was %s)", _circuit_state)
            _circuit_state = "CLOSED"
        _circuit_consecutive_failures = 0
        return
    _circuit_consecutive_failures += 1
    threshold = max(1, int(settings.langfuse_circuit_failure_threshold))
    if _circuit_consecutive_failures >= threshold and _circuit_state != "OPEN":
        _circuit_state = "OPEN"
        _circuit_opened_at = time.monotonic()
        log.warning(
            "langfuse breaker: OPEN after %d consecutive failures",
            _circuit_consecutive_failures,
        )


def circuit_status() -> dict[str, Any]:
    """The breaker, for /v1/health and the `loom.langfuse.breaker` gauge."""
    return {
        "state": _circuit_state,
        "consecutive_failures": _circuit_consecutive_failures,
        "dropped_while_open": _dropped_breaker_open,
    }


def _auth_header() -> str | None:
    token = settings.langfuse_admin_token.strip()
    if ":" not in token or not settings.langfuse_url:
        return None
    return "Basic " + base64.b64encode(token.encode()).decode()


def enabled() -> bool:
    """Scores are written whenever Langfuse is configured, the same condition
    that attaches the OTLP span processor: a score references a trace by id."""
    return _auth_header() is not None


def _current_trace_id() -> str | None:
    """The active span's trace id as 32-char hex, which is Langfuse's traceId.

    Verified against the live project rather than assumed: a span ingested over
    OTLP came back from `GET /api/public/traces/<otel-hex>` under exactly this
    id. That is the whole reason scores can be attached with no bookkeeping.
    """
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not ctx or not ctx.trace_id or not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 — no readable span means no score, never a failed search
        return None


def _trace_is_sampled(trace_id_hex: str) -> bool:
    """Match the span processor's verdict for this trace.

    Same deterministic low-64-bit threshold, so a sampled-out trace gets no
    scores instead of scores nobody can open.
    """
    rate = settings.langfuse_sample_rate
    rate = 0.0 if rate < 0 else (1.0 if rate > 1 else float(rate))
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    try:
        return (int(trace_id_hex, 16) & 0xFFFFFFFFFFFFFFFF) < int(rate * (1 << 64))
    except ValueError:
        return False


def _post_score(body: dict[str, Any]) -> None:
    """One blocking POST. Called only from a worker thread."""
    auth = _auth_header()
    if auth is None:
        return
    url = settings.langfuse_url.rstrip("/") + "/api/public/scores"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=settings.langfuse_timeout_s) as r:
        r.read()


async def _post_one(body: dict[str, Any]) -> None:
    """One score, one POST, behind the breaker."""
    global _posted, _failed, _dropped_breaker_open
    if not _circuit_allow():
        _dropped_breaker_open += 1
        return
    try:
        await asyncio.to_thread(_post_score, body)
        _circuit_record(True)
        _posted += 1
    except Exception as e:  # noqa: BLE001 — a failed POST counts toward the breaker
        _circuit_record(False)
        _failed += 1
        if _failed <= 3 or _failed % 100 == 0:
            log.warning(
                "langfuse score POST failed (%s: %s); %d failed so far",
                type(e).__name__,
                e,
                _failed,
            )


_q = LoopBoundQueue(name="langfuse-scores", maxsize=_MAX_QUEUE, handle=_post_one)


def _enqueue(body: dict[str, Any]) -> None:
    global _dropped_full
    q = _q.ensure()
    if q is None:
        return
    try:
        q.put_nowait(body)
    except asyncio.QueueFull:
        # Drop the OLDEST, not this one: during a backlog the newest scores
        # describe the searches an operator is actually looking at.
        try:
            q.get_nowait()
            q.task_done()
            q.put_nowait(body)
        except Exception:  # noqa: BLE001 — enqueue never raises into the request
            pass
        _dropped_full += 1
        if _dropped_full in (1, 10, 100) or _dropped_full % 1000 == 0:
            log.warning("langfuse score queue full; dropped %d oldest scores", _dropped_full)


def record_search_scores(
    *,
    confidence: float | None,
    top_similarity: float | None,
    abstained: bool,
    abstain_code: str,
    namespace: str,
    session_id: str = "",
) -> int:
    """Enqueue the retrieval-quality scores for the active trace.

    Returns the number enqueued — 0 when Langfuse is off, no span is active, or
    the trace was sampled out. Never raises and never blocks: called from
    `record_search_quality`, which runs inside the request.
    """
    if not enabled():
        return 0
    trace_id = _current_trace_id()
    if not trace_id or not _trace_is_sampled(trace_id):
        return 0

    scores: list[dict[str, Any]] = []
    if confidence is not None:
        scores.append(
            {
                "name": SCORE_CONFIDENCE,
                "value": float(confidence),
                "dataType": "NUMERIC",
            }
        )
    if top_similarity is not None:
        scores.append(
            {
                "name": SCORE_TOP_SIMILARITY,
                "value": float(top_similarity),
                "dataType": "NUMERIC",
            }
        )
    # Always written, including on the success path: a BOOLEAN that only appears
    # when true has no denominator, so "what fraction of searches abstain" —
    # the actual question — would be unanswerable.
    scores.append(
        {
            "name": SCORE_ABSTAINED,
            "value": 1 if abstained else 0,
            "dataType": "BOOLEAN",
        }
    )
    if abstained and abstain_code:
        scores.append(
            {
                "name": SCORE_ABSTAIN_REASON,
                # CATEGORICAL puts the LABEL in `value`, as a string — not in a
                # separate `stringValue` field, which answers
                #   400 {"expected":"string","path":["value"]}
                # and, since the POST happens on a background task, the only
                # symptom would be a missing score.
                "value": abstain_code,
                "dataType": "CATEGORICAL",
            }
        )

    for s in scores:
        post_score(
            trace_id=trace_id,
            name=s["name"],
            value=s["value"],
            data_type=s["dataType"],
            comment=f"namespace={namespace}" if namespace else "",
            session_id=session_id,
        )
    return len(scores)


def record_ask_scores(
    *,
    source: str,
    coverage: str,
    retrieval_confidence: float | None,
    namespace: str,
) -> int:
    """Per-trace quality scores for code.ask — the answer surface.

    `code_ask_cache_hit` is written on EVERY ask (boolean needs its
    denominator, same reasoning as `abstained`); `code_ask_coverage` maps
    the synthesizer's self-grade to 1.0/0.5/0.0 and is written only when a
    grade exists (cached hits carry the original synthesis's grade era, not
    this request's). Without these, an answer-quality regression is
    invisible in Langfuse until the weekly bench."""
    if not enabled():
        return 0
    trace_id = _current_trace_id()
    if not trace_id or not _trace_is_sampled(trace_id):
        return 0
    n = 0
    comment = f"namespace={namespace}" if namespace else ""
    if post_score(
        trace_id=trace_id,
        name="code_ask_cache_hit",
        value=1 if source == "cached" else 0,
        data_type="BOOLEAN",
        comment=comment,
    ):
        n += 1
    grade_value = {"full": 1.0, "partial": 0.5, "none": 0.0}.get(coverage)
    if grade_value is not None and post_score(
        trace_id=trace_id,
        name="code_ask_coverage",
        value=grade_value,
        comment=comment,
    ):
        n += 1
    if (
        retrieval_confidence is not None
        and source != "cached"
        and post_score(
            trace_id=trace_id,
            name=SCORE_CONFIDENCE,
            value=float(retrieval_confidence),
            comment=comment,
        )
    ):
        n += 1
    return n


def post_score(
    *,
    trace_id: str,
    name: str,
    value: float | str,
    data_type: str = "NUMERIC",
    comment: str = "",
    session_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Enqueue one arbitrary score against a trace. Non-blocking.

    Exposed so other producers — the retrieval judge, future evaluators — reuse
    this queue, breaker and drop accounting instead of each growing its own POST
    path. `record_search_scores` stays as the per-search convenience on top.

    CATEGORICAL carries its label in `value` as a string; sending it in a separate
    `stringValue` field is a 400 and, because the POST is asynchronous, the score
    simply never appears.

    `metadata` is a free-form dict that round-trips intact (verified against the
    live API: posted `{"scheme": ...}` comes back byte-identical on
    `GET /api/public/v2/scores`). The judge uses it to stamp which scoring scheme
    produced the number, so a consumer can filter out retired schemes — without
    it, the human-review queue mixes scores from schemes that no longer exist and
    a reviewer calibrates against numbers loom would not produce today.

    One live-API quirk worth recording: a score posted against a trace id that
    does not exist returns 200 with an id and is then never queryable. It is
    silently dropped, so callers must pass a real trace id.
    """
    if not enabled() or not trace_id or not name:
        return False
    body: dict[str, Any] = {
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": data_type,
    }
    if comment:
        body["comment"] = comment[:500]
    if session_id:
        body["sessionId"] = session_id
    if metadata:
        body["metadata"] = metadata
    _enqueue(body)
    return True


async def drain(timeout_s: float = 5.0) -> None:
    """Shutdown: post what is queued for up to `timeout_s`, then stop the
    worker. Scores still queued are counted in `dropped_shutdown`. Runs in the
    lifespan after the retrieval judge stops (it posts through this queue)."""
    global _dropped_shutdown
    q = _q.queue
    if q is not None and _q.running():
        try:
            await asyncio.wait_for(q.join(), timeout=timeout_s)
        except TimeoutError:
            log.warning("langfuse score drain timed out with %d scores queued", q.qsize())
        except RuntimeError:
            pass  # a queue bound to a previous loop cannot be joined on this one
    await _q.stop_worker()
    if q is not None and q.qsize():
        _dropped_shutdown += q.qsize()
        log.warning("langfuse scores: %d queued scores dropped at shutdown", q.qsize())
    _q.reset()


def status() -> dict[str, Any]:
    """The score path's counters, served as `langfuse.scores` by
    `/v1/health?include=langfuse`. The retrieval judge posts through
    `post_score`, so its breaker-open drops are counted here too."""
    return {
        "enabled": enabled(),
        "queued": _q.qsize(),
        "posted": _posted,
        "failed": _failed,
        "dropped_queue_full": _dropped_full,
        "dropped_breaker_open": _dropped_breaker_open,
        "dropped_shutdown": _dropped_shutdown,
        "worker_running": _q.running(),
    }


__all__ = [
    "SCORE_ABSTAINED",
    "SCORE_ABSTAIN_REASON",
    "SCORE_CONFIDENCE",
    "SCORE_TOP_SIMILARITY",
    "drain",
    "enabled",
    "post_score",
    "record_search_scores",
    "status",
]
