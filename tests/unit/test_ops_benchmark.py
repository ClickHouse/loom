"""scripts/ops/benchmark.py: nearest-rank percentiles, the summary, a health
check before any load, and concurrency that is not capped by the default
executor's size."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scripts.ops import benchmark as bench


def test_percentiles_are_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert bench.percentile(values, 0.50) == 50.0
    assert bench.percentile(values, 0.95) == 95.0
    assert bench.percentile(values, 0.99) == 99.0
    assert bench.percentile([4.0, 1.0, 3.0, 2.0], 0.50) == 2.0  # sorted first
    assert bench.percentile([7.0], 0.99) == 7.0
    assert bench.percentile([], 0.5) == 0.0


def test_the_summary_keeps_sub_millisecond_latencies_and_names_failures() -> None:
    calls = [bench.Call(200, 0.4, "bench"), bench.Call(200, 0.6, "bench")]
    calls += [bench.Call(429, 1.0, "bench"), bench.Call(0, 5.0, error="TimeoutError")]
    s = bench.summarize(calls, 2.0)
    assert s["p50_ms"] == 0.4 and s["max_ms"] == 0.6 and s["mean_ms"] == 0.5
    assert s["ok"] == 2 and s["rps"] == 1.0
    assert s["error_codes"] == {"429": 1, "TimeoutError": 1}
    assert s["traffic_class"] == {"-": 1, "bench": 3}


class _Server:
    """A stub Loom: /v1/health and one slow op, counting calls in flight."""

    def __init__(self, delay_s: float = 0.0, healthy: bool = True) -> None:
        self.in_flight = self.peak = 0
        self.bodies: list[dict[str, Any]] = []
        lock, outer = threading.Lock(), self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                body = json.dumps({"build_commit": "abc", "schema_version": "9"}).encode()
                self.send_response(200 if healthy else 503)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                n = int(self.headers["Content-Length"])
                with lock:
                    outer.bodies.append(json.loads(self.rfile.read(n)))
                    outer.in_flight += 1
                    outer.peak = max(outer.peak, outer.in_flight)
                time.sleep(delay_s)
                with lock:
                    outer.in_flight -= 1
                self.send_response(200)
                self.send_header("X-Loom-Traffic-Class", "bench")
                self.end_headers()
                self.wfile.write(b"{}")

        class Httpd(ThreadingHTTPServer):
            request_queue_size = 128  # the default 5 drops a burst of connects

        self.httpd = Httpd(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def test_concurrency_is_not_capped_by_the_default_executor(monkeypatch: Any) -> None:
    # Each call waits at a barrier for all 48 to be running at once: it opens
    # only when nothing caps the calls in flight (the default executor stops at
    # 32 threads). The clock stands still until it opens, so every worker makes
    # exactly one call however slowly the runner starts threads.
    concurrency = 48
    opened = threading.Event()
    # The barrier's action sets the flag before any waiter is released, so no
    # worker can read the stopped clock again and start a second round.
    barrier = threading.Barrier(concurrency, action=opened.set, timeout=30)

    def post(*_: Any) -> bench.Call:
        barrier.wait()
        return bench.Call(200, 1.0, "bench")

    clock = types.SimpleNamespace(monotonic=lambda: 1e9 if opened.is_set() else 0.0)
    monkeypatch.setattr(bench, "_post", post)
    monkeypatch.setattr(bench, "time", clock)
    out = asyncio.run(bench.run("http://stub", "memory.set", concurrency, 1.0, "o", "b", "t"))
    assert out["ok"] == concurrency and not barrier.broken


def test_a_failed_health_check_sends_no_load(capsys: Any) -> None:
    srv = _Server(healthy=False)
    try:
        rc = bench.main(["--base-url", srv.url, "--token", "t", "--duration", "0.1"])
    finally:
        srv.close()
    assert rc == 2 and srv.bodies == []
    assert "nothing sent" in capsys.readouterr().err


def test_the_json_result_carries_the_environment(capsys: Any) -> None:
    srv = _Server()
    try:
        rc = bench.main(
            ["--base-url", srv.url, "--token", "t", "--duration", "0.1", "--concurrency", "2"]
            + ["--namespace", "bench-y", "--json"]
        )
    finally:
        srv.close()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert rc == 0 and result["env"]["build_commit"] == "abc"
    assert result["env"]["namespace"] == "bench-y" and "WRITES rows into" in captured.err


@pytest.mark.parametrize("op", bench.OPS)
def test_every_op_builds_a_body_for_its_path(op: str) -> None:
    path, body = bench._make_body(op, "o", "n")
    assert path == f"/v1/{op}" and body["org"] == "o" and body["namespace"] == "n"
