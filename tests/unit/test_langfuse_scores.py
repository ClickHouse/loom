"""Retrieval-quality scores: gating, shape, and the hot-path guarantees.

`server/insights/app/admin_routes.py` reads scores to gate `prompts.evaluate`. These tests
pin the writer that produces them — and, more importantly, the things that make
it safe to call from inside a search.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import langfuse_scores as scores


@pytest.fixture(autouse=True)
def _reset_module_state() -> Any:
    scores._q.reset()
    scores._dropped_full = 0
    scores._dropped_breaker_open = 0
    yield
    scores._q.reset()


def _configure(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    defaults = {
        "langfuse_url": "https://us.cloud.langfuse.com",
        "langfuse_admin_token": "pk-lf-a:sk-lf-b",
        "langfuse_sample_rate": 1.0,
    }
    defaults.update(kw)
    for k, v in defaults.items():
        monkeypatch.setattr(scores.settings, k, v, raising=False)


def _fake_trace(monkeypatch: pytest.MonkeyPatch, hex_id: str | None) -> None:
    monkeypatch.setattr(scores, "_current_trace_id", lambda: hex_id)


def test_off_without_langfuse_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Langfuse URL: no span processor, so no scores either (a score
    references a trace by id)."""
    _configure(monkeypatch, langfuse_url="")
    assert scores.enabled() is False
    _fake_trace(monkeypatch, "a" * 32)
    assert (
        scores.record_search_scores(
            confidence=0.9,
            top_similarity=0.8,
            abstained=False,
            abstain_code="",
            namespace="ns",
        )
        == 0
    )


def test_off_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, langfuse_admin_token="pk-lf-only-no-colon")
    assert scores.enabled() is False


def test_no_scores_without_an_active_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A score with no trace id would be rejected, or worse, orphaned."""
    _configure(monkeypatch)
    _fake_trace(monkeypatch, None)
    assert (
        scores.record_search_scores(
            confidence=0.5,
            top_similarity=0.5,
            abstained=False,
            abstain_code="",
            namespace="ns",
        )
        == 0
    )


def test_sampled_out_traces_get_no_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scores must agree with the span processor's sampling verdict.

    Otherwise a downsampled deployment accumulates scores whose traces were never
    forwarded — the dashboard shows quality data for traces you cannot open.
    """
    _configure(monkeypatch, langfuse_sample_rate=0.0)
    _fake_trace(monkeypatch, "f" * 32)
    assert (
        scores.record_search_scores(
            confidence=0.5,
            top_similarity=0.5,
            abstained=False,
            abstain_code="",
            namespace="ns",
        )
        == 0
    )


def test_sampling_gate_matches_the_span_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same trace id, same verdict, in both modules.

    Two independent implementations of "is this trace sampled" would drift, and
    the drift is invisible: you would get scores for unsampled traces and no
    scores for sampled ones, in proportion to the disagreement.
    """
    from server.observability import otel_langfuse

    monkeypatch.setattr(otel_langfuse.settings, "langfuse_sample_rate", 0.37, raising=False)
    monkeypatch.setattr(otel_langfuse.settings, "langfuse_span_denylist", "", raising=False)
    _configure(monkeypatch, langfuse_sample_rate=0.37)
    span_filter = otel_langfuse._LangfuseSpanFilter()

    class _Ctx:
        def __init__(self, tid: int) -> None:
            self.trace_id = tid

    class _Span:
        name = "memory.search"
        parent = "p"

        def __init__(self, tid: int) -> None:
            self._c = _Ctx(tid)

        def get_span_context(self) -> Any:
            return self._c

    for i in range(500):
        tid = (i * 0x9E3779B97F4A7C15) & ((1 << 128) - 1)
        assert span_filter.keep(_Span(tid)) == scores._trace_is_sampled(format(tid, "032x")), (
            f"score and span sampling disagree on trace {tid:032x}"
        )


def test_abstained_is_written_even_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BOOLEAN that only appears when true has no denominator.

    "What fraction of searches abstain" is the actual question, and it needs the
    negative cases recorded.
    """
    _configure(monkeypatch)
    _fake_trace(monkeypatch, "1" * 32)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(scores, "_enqueue", lambda b: sent.append(b))

    scores.record_search_scores(
        confidence=0.91,
        top_similarity=0.77,
        abstained=False,
        abstain_code="",
        namespace="clickgap-public",
    )
    by_name = {s["name"]: s for s in sent}
    assert set(by_name) == {"retrieval_confidence", "top_similarity", "abstained"}
    assert by_name["abstained"]["value"] == 0
    assert by_name["abstained"]["dataType"] == "BOOLEAN"
    assert by_name["retrieval_confidence"]["value"] == pytest.approx(0.91)
    assert all(s["traceId"] == "1" * 32 for s in sent)


def test_abstain_reason_is_categorical_with_the_label_in_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CATEGORICAL puts the LABEL in `value`, as a string.

    Verified against the live API, not assumed: a `stringValue` field returns
    400 {"expected":"string","path":["value"]}. The failure mode is what makes
    this worth pinning — the POST runs on a background task, so a wrong shape
    just means the score never appears, with no error at the call site.
    """
    _configure(monkeypatch)
    _fake_trace(monkeypatch, "2" * 32)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(scores, "_enqueue", lambda b: sent.append(b))

    scores.record_search_scores(
        confidence=0.2,
        top_similarity=0.11,
        abstained=True,
        abstain_code="below_threshold",
        namespace="ns",
    )
    reason = next(s for s in sent if s["name"] == "abstain_reason")
    assert reason["dataType"] == "CATEGORICAL"
    assert reason["value"] == "below_threshold"
    assert "stringValue" not in reason


def test_missing_values_are_omitted_not_zeroed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No confidence is not confidence 0.

    A search over a corpus with no vector-plane hits has no `top_similarity`;
    writing 0.0 would report a perfectly bad search instead of no measurement,
    and would drag every aggregate down.
    """
    _configure(monkeypatch)
    _fake_trace(monkeypatch, "3" * 32)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(scores, "_enqueue", lambda b: sent.append(b))

    scores.record_search_scores(
        confidence=None,
        top_similarity=None,
        abstained=True,
        abstain_code="no_match",
        namespace="ns",
    )
    names = {s["name"] for s in sent}
    assert names == {"abstained", "abstain_reason"}


def test_enqueue_is_non_blocking_without_a_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`record_search_quality` is sync and runs inside the request.

    Called with no running loop (a test, a CLI, a thread), this must be a no-op
    rather than an error — and must never construct a coroutine nobody awaits.
    """
    _configure(monkeypatch)
    _fake_trace(monkeypatch, "4" * 32)
    n = scores.record_search_scores(
        confidence=0.5,
        top_similarity=0.5,
        abstained=False,
        abstain_code="",
        namespace="ns",
    )
    # Scores were built and enqueue attempted; without a loop nothing is queued.
    assert n == 3
    assert scores._q.queue is None


@pytest.mark.asyncio
async def test_queue_is_bounded_and_drops_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue that grows while Langfuse is slow is a memory leak.

    Oldest-first because during a backlog the newest scores describe the searches
    an operator is currently looking at.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(scores._q, "maxsize", 3)
    # Keep the worker from draining what we are trying to overflow.
    monkeypatch.setattr(scores._q, "_run", lambda: asyncio.sleep(3600))

    for i in range(10):
        scores._enqueue({"traceId": "x", "name": f"s{i}"})
    assert scores._q.queue is not None
    assert scores._q.queue.qsize() == 3
    assert scores._dropped_full == 7
    remaining = [scores._q.queue.get_nowait()["name"] for _ in range(3)]
    assert remaining == ["s7", "s8", "s9"], "should keep the NEWEST three"


@pytest.mark.asyncio
async def test_worker_survives_a_failing_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bad POST must not kill the worker.

    If it did, every later score in the process would be silently queued forever —
    the queue would fill, then start dropping, with no error after the first.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(scores, "_circuit_allow", lambda: True)
    monkeypatch.setattr(scores, "_circuit_record", lambda ok: None)

    calls: list[dict[str, Any]] = []

    def _boom(body: dict[str, Any]) -> None:
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError("langfuse 500")

    monkeypatch.setattr(scores, "_post_score", _boom)
    scores._enqueue({"traceId": "t", "name": "first"})
    scores._enqueue({"traceId": "t", "name": "second"})
    assert scores._q.queue is not None
    await asyncio.wait_for(scores._q.queue.join(), timeout=5.0)
    assert [c["name"] for c in calls] == ["first", "second"]
    assert scores._q.worker is not None and not scores._q.worker.done()


@pytest.mark.asyncio
async def test_open_breaker_drops_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Langfuse outage must not become a retry storm from the score path."""
    _configure(monkeypatch)
    monkeypatch.setattr(scores, "_circuit_allow", lambda: False)
    posted: list[Any] = []
    monkeypatch.setattr(scores, "_post_score", lambda b: posted.append(b))

    scores._enqueue({"traceId": "t", "name": "x"})
    scores._enqueue({"traceId": "t", "name": "y"})
    assert scores._q.queue is not None
    await asyncio.wait_for(scores._q.queue.join(), timeout=5.0)
    assert posted == []
    # ...and the drop is counted, not silent.
    assert scores.status()["dropped_breaker_open"] == 2
    assert scores.circuit_status()["dropped_while_open"] == 2


def test_breaker_opens_after_the_threshold_and_probes_after_the_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scores.settings, "langfuse_circuit_failure_threshold", 2)
    monkeypatch.setattr(scores.settings, "langfuse_circuit_cooldown_s", 30.0)
    monkeypatch.setattr(scores, "_circuit_state", "CLOSED")
    monkeypatch.setattr(scores, "_circuit_consecutive_failures", 0)
    now = [1000.0]
    monkeypatch.setattr(scores.time, "monotonic", lambda: now[0])
    scores._circuit_record(False)
    assert scores._circuit_allow() and scores.circuit_status()["state"] == "CLOSED"
    scores._circuit_record(False)
    assert scores.circuit_status()["state"] == "OPEN" and not scores._circuit_allow()
    now[0] += 31.0
    assert scores._circuit_allow() and scores.circuit_status()["state"] == "HALF_OPEN"
    scores._circuit_record(True)
    assert scores.circuit_status() == {
        "state": "CLOSED",
        "consecutive_failures": 0,
        "dropped_while_open": 0,
    }


def test_score_names_match_the_prompts_evaluate_gate() -> None:
    """The names are a contract with a consumer that already exists.

    `prompts.evaluate` reads the score list by `traceId` and `name=<rubric>`
    (`server/insights/app/admin_routes.py`). Renaming a score here does not fail loudly — it
    makes the auto-promote gate silently find nothing and decline to promote.
    """
    from pathlib import Path

    admin = Path(__file__).resolve().parents[2] / "server" / "insights" / "app" / "admin_routes.py"
    src = admin.read_text()
    assert '"name": rubric' in src, (
        "the prompts.evaluate gate no longer queries scores by name; the score "
        "name contract in langfuse_scores.py may be stale"
    )
    assert scores.SCORE_CONFIDENCE == "retrieval_confidence"
    assert scores.SCORE_TOP_SIMILARITY == "top_similarity"
    assert scores.SCORE_ABSTAINED == "abstained"
    assert scores.SCORE_ABSTAIN_REASON == "abstain_reason"


@pytest.mark.asyncio
async def test_queue_is_rebuilt_when_the_event_loop_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An asyncio.Queue binds to the loop that first awaits it.

    A queue built on a previous loop can never be drained on this one, so
    `await queue.get()` raises RuntimeError forever. That call sits INSIDE the
    worker's try/except: outside it, it would kill the worker despite the "must
    never die" contract, after which every score would be queued and never
    posted while status() still reported enabled.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(scores._q, "_run", lambda: asyncio.sleep(3600))

    scores._enqueue({"traceId": "t", "name": "first"})
    stale_queue = scores._q.queue
    assert stale_queue is not None

    # Simulate having been built on a different loop.
    scores._q._loop = object()  # type: ignore[assignment]
    scores._enqueue({"traceId": "t", "name": "second"})
    assert scores._q.queue is not stale_queue, (
        "a loop change must rebuild the queue, not reuse one bound to a dead loop"
    )


@pytest.mark.asyncio
async def test_worker_survives_an_unusable_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising `get()` must stand the worker down cleanly, not crash it.

    The worker resets the queue so the next enqueue rebuilds both, instead of
    spinning forever on a queue that can never yield.
    """
    _configure(monkeypatch)

    class _BrokenQueue:
        async def get(self) -> Any:
            raise RuntimeError("<Queue> is bound to a different event loop")

        def task_done(self) -> None:
            pass

    scores._q.queue = _BrokenQueue()  # type: ignore[assignment]
    await scores._q._run()  # must return, not raise
    assert scores._q.queue is None, "the unusable queue must be dropped"
    assert scores._q.worker is None


@pytest.mark.asyncio
async def test_drain_posts_the_backlog_then_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown posts what is queued instead of losing it with the loop, and
    leaves no worker task behind."""
    _configure(monkeypatch)
    monkeypatch.setattr(scores, "_circuit_allow", lambda: True)
    monkeypatch.setattr(scores, "_circuit_record", lambda ok: None)
    posted: list[str] = []
    monkeypatch.setattr(scores, "_post_score", lambda b: posted.append(b["name"]))
    scores._enqueue({"traceId": "t", "name": "a"})
    scores._enqueue({"traceId": "t", "name": "b"})
    worker = scores._q.worker
    await scores.drain(timeout_s=5.0)
    assert posted == ["a", "b"]
    assert worker is not None and worker.done()
    assert scores._q.queue is None and scores._q.worker is None


@pytest.mark.asyncio
async def test_drain_counts_what_it_could_not_post(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(scores._q, "_run", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(scores, "_dropped_shutdown", 0)
    scores._enqueue({"traceId": "t", "name": "a"})
    scores._enqueue({"traceId": "t", "name": "b"})
    await scores.drain(timeout_s=0.01)
    assert scores.status()["dropped_shutdown"] == 2
    assert scores._q.worker is None


@pytest.mark.asyncio
async def test_judge_stop_cancels_the_worker_and_counts_the_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge does not hold shutdown on paid LLM calls: its backlog is
    counted as dropped and its worker is gone."""
    from server.observability import retrieval_judge as judge

    monkeypatch.setattr(judge._q, "_run", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(judge, "_dropped_shutdown", 0)
    judge._q.reset()
    q = judge._q.ensure()
    assert q is not None
    q.put_nowait({"trace_id": "t"})
    worker = judge._q.worker
    await judge.stop()
    assert worker is not None and worker.cancelled()
    assert judge.status()["dropped_shutdown"] == 1
    assert judge._q.queue is None and judge._q.worker is None
