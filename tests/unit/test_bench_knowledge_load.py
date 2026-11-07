"""bench/knowledge_load.py: the declared service levels it checks against, and how it
scores a run. No server: the load itself runs against one (its docstring)."""

from __future__ import annotations

from typing import Any

import pytest

from bench import knowledge_load as load
from server.settings.catalog import OP_BUDGETS_P99_MS

HEAD = "\n## Service levels\n\n| Measure | p95 ms | p99 ms |\n| --- | --- | --- |\n"


def test_each_declared_op_level_is_its_latency_budget() -> None:
    """An op's p99 level is its budget, so the load and the server's own
    budget.violated count against one number."""
    levels = load.service_levels(load.DOC.read_text())
    ops = {m for m in levels if m.startswith("knowledge.")}
    assert ops == {"knowledge.search", "knowledge.query", "knowledge.ingest"}
    for op in ops:
        p95, p99 = levels[op]
        assert p99 == OP_BUDGETS_P99_MS[op], op
        assert 0 < p95 <= p99, op
    assert load.INGEST_SMALL in levels
    # Every measured op the run reports is one the table can name.
    assert ops <= set(load.OPS)


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        ("| `knowledge.search` | 150 |\n", "not a"),
        ("| `knowledge.search` | 150 | 0.25 |\n", "not a"),
        ("| `knowledge.search` | 150 | 250 |\n| `knowledge.search` | 100 | 250 |\n", "repeated"),
        ("", "no rows"),
    ],
)
def test_a_levels_table_it_cannot_read_whole_is_an_error(rows: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        load.service_levels(HEAD + rows)


def test_a_level_is_read_with_its_thousands_and_up_to_the_next_heading() -> None:
    page = HEAD + "| `knowledge.query` | 1,000 | 1,500 |\n\n### Notes\n\n| a | b |\n"
    assert load.service_levels(page) == {"knowledge.query": (1000.0, 1500.0)}


def test_a_measure_past_either_level_or_never_measured_is_reported() -> None:
    def measured(p95: float, p99: float, ok: int = 10) -> dict[str, object]:
        return {"ok": ok, "p95_ms": p95, "p99_ms": p99}

    levels = {"a": (100.0, 200.0), "b": (100.0, 200.0), "c": (100.0, 200.0), "d": (1.0, 2.0)}
    report = {"a": measured(90, 190), "b": measured(120, 190), "c": measured(90, 210)}
    past = load.verdict(report, levels)  # type: ignore[arg-type]
    assert [p.split(":")[0] for p in past] == ["b", "c", "d"]
    assert past[2] == "d: nothing measured"
    assert load.verdict({"a": measured(90, 190, ok=0)}, {"a": (100.0, 200.0)}) == [  # type: ignore[dict-item]
        "a: nothing measured"
    ]


def test_a_tally_times_successes_and_counts_rejected_calls_apart() -> None:
    t = load.Tally()
    for ms in (10.0, 20.0, 30.0, 40.0):
        t.add(ms, 200)
    t.add(5.0, 201)  # an upload's answer
    for status in (429, 503, 503, 500, 0):
        t.add(1.0, status)
    s = t.summary()
    assert (s["ok"], s["p50_ms"], s["p99_ms"]) == (5, 20.0, 40.0)
    assert s["errors"] == {"429": 1, "503": 2, "500": 1, "0": 1} and s["rejected"] == 3


def test_a_measure_with_failed_calls_is_past_however_fast_the_rest() -> None:
    fast = {"ok": 10, "p95_ms": 1.0, "p99_ms": 1.0}
    levels = {"a": (100.0, 200.0)}
    assert load.verdict({"a": {**fast, "errors": {"429": 50}}}, levels) == []
    (past,) = load.verdict({"a": {**fast, "errors": {"500": 90, "429": 3}}}, levels)
    assert past == "a: 90 calls failed {'500': 90}"


def test_the_queue_depth_is_summed_by_sample_slot_over_the_tenants_that_read_one() -> None:
    series = [[(0, 2), (1, 3), (1, 4)], [(0, 5), (2, 1)], []]
    assert load.combined_depth(series) == {0: 7, 1: 4, 2: 1}


def test_an_answer_that_is_not_json_is_a_failed_call_tallied_not_a_crash() -> None:
    import asyncio

    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("knowledge.search"):
            return httpx.Response(502, text="<html>bad gateway</html>")
        return httpx.Response(200, text="not json")

    async def go() -> list[tuple[int, dict[str, object]]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(answer), base_url="http://x"
        ) as c:
            run = load.Run(c, "o", "ns", 0.0, load._fresh_tallies(), load.Counter())
            out = [await run.op("knowledge.search", {}), await run.op("knowledge.query", {})]
            assert run.tallies["knowledge.search"].errors == {"502": 1}
            # A 200 whose body is not JSON is no answer: an error, not a timed success.
            query = run.tallies["knowledge.query"]
            assert query.errors == {"0": 1} and not query.latencies_ms
            return out

    assert asyncio.run(go()) == [(502, {}), (0, {})]


def test_calls_start_at_their_rate_whatever_the_earlier_ones_take() -> None:
    import asyncio
    import time

    async def go() -> list[float]:
        started: list[float] = []

        async def slow(n: int) -> None:
            started.append(time.monotonic())
            await asyncio.sleep(0.5)  # far longer than the 0.05 s between starts

        run = load.Run(None, "o", "ns", time.monotonic() + 0.3, {}, load.Counter())  # type: ignore[arg-type]
        t0 = time.monotonic()
        await run.paced(20.0, slow)
        return [t - t0 for t in started]

    starts = asyncio.run(go())
    assert len(starts) >= 5  # open loop: a slow call does not hold back the next
    assert all(b - a < 0.2 for a, b in zip(starts, starts[1:], strict=False))


def test_a_jobs_time_is_its_servers_creation_to_its_end() -> None:
    status = {"created_at": "2026-01-02T03:04:05Z", "updated_at": "2026-01-02T03:04:17.250Z"}
    assert load._job_seconds(status, 99.0) == 12.25
    naive = {"created_at": "2026-01-02T03:04:05", "updated_at": "2026-01-02T03:05:05"}
    assert load._job_seconds(naive, 99.0) == 60.0
    # No timestamps (or unreadable ones): the time this process waited.
    assert load._job_seconds({"state": "ready"}, 3.5) == 3.5
    assert load._job_seconds({"created_at": "soon", "updated_at": "later"}, 3.5) == 3.5
    mixed = {"created_at": "2026-01-02T03:04:05Z", "updated_at": "2026-01-02T03:04:06"}
    assert load._job_seconds(mixed, 3.5) == 3.5  # one aware, one naive


def _main_against(answer: Any, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    import asyncio

    import httpx

    real = httpx.AsyncClient

    def mocked(*a: Any, **k: Any) -> httpx.AsyncClient:
        return real(*a, transport=httpx.MockTransport(answer), **k)

    monkeypatch.setattr(load.httpx, "AsyncClient", mocked)
    monkeypatch.setattr(load, "CANARY_S", 0.3)
    monkeypatch.setattr(load, "POLL_S", 0.05)
    args = load._args(["--base-url", "http://x", "--duration", "1", *argv])
    return asyncio.run(load.main(args))


def _server(job_state: str, refuse_org: str = "") -> Any:
    """A server whose canary job reads `job_state`, refusing `refuse_org` with 403."""
    import json

    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if refuse_org and request.content and json.loads(request.content).get("org") == refuse_org:
            return httpx.Response(403, json={"error": "no"})
        if path.endswith("knowledge.inspect"):
            settings = {"worker_enabled": True, "extractor_configured": True}
            return httpx.Response(200, json={"ingestions": settings})
        if path.endswith("knowledge.upload"):
            return httpx.Response(201, json={"upload_id": "u"})
        if path.endswith("knowledge.ingest"):
            return httpx.Response(200, json={"ingestion_id": "j"})
        times = {"created_at": "2026-01-02T03:04:05Z", "updated_at": "2026-01-02T03:04:06Z"}
        return httpx.Response(200, json={"state": job_state, **times})

    return answer


@pytest.mark.parametrize("state", ["pending", "failed", "cancelled"])
def test_a_canary_that_does_not_end_ready_exits_2(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    assert _main_against(_server(state), monkeypatch) == 2


def test_a_server_that_does_not_answer_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _main_against(refused, monkeypatch) == 2


def test_a_tenant_org_the_token_cannot_reach_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _server("ready", refuse_org="bench-1")
    assert _main_against(answer, monkeypatch, "--tenants", "2") == 2


def test_a_token_that_cannot_read_the_tenant_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    assert _main_against(lambda r: httpx.Response(403, json={"error": "no"}), monkeypatch) == 2


@pytest.mark.parametrize(
    "broken", ["upload not json", "upload without an id", "ingest without an id"]
)
def test_an_upload_or_ingest_answer_out_of_shape_is_not_queued_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """The canary's own upload or ingest answered out of shape: exit 2, no traceback."""
    import httpx

    ready = _server("ready")

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("knowledge.upload") and broken == "upload not json":
            return httpx.Response(201, text="<html>proxy</html>")
        if path.endswith("knowledge.upload") and broken == "upload without an id":
            return httpx.Response(201, json={})
        if path.endswith("knowledge.ingest") and broken == "ingest without an id":
            return httpx.Response(200, json={})
        return ready(request)

    assert _main_against(answer, monkeypatch) == 2


def test_an_answer_out_of_shape_mid_run_is_skipped_and_the_run_still_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dataset or inspect answer the harness cannot read ends no task: it is
    skipped (no dataset to query, no depth sample) and the run still reports."""
    import json

    import httpx

    ready = _server("ready")
    inspected: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("knowledge.dataset_get"):
            return httpx.Response(200, json={"datasets": [{"no": "id"}]})
        if path.endswith("knowledge.inspect"):
            inspected.append(1)
            if len(inspected) > 1:  # the probe reads well; the samples do not
                return httpx.Response(200, json={"ingestions": ["not", "totals"]})
        if path.endswith("knowledge.search"):
            return httpx.Response(200, json={"results": "x"})
        return ready(request)

    assert _main_against(answer, monkeypatch, "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["queue_depth"]["by_sample"] == []  # no sample from an unreadable answer
    assert out["measures"]["knowledge.search"]["ok"] > 0
    # Exit 1 for what the skipped dataset left: no query ran.
    assert "knowledge.query: nothing measured" in out["past_level"]
