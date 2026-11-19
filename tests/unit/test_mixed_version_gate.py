"""The mixed-version gate skips cleanly where it cannot run."""

from __future__ import annotations

import pytest

from scripts.gates import mixed_version


def test_without_docker_the_gate_skips_with_exit_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.main([]) == 0
    assert "skipped" in capsys.readouterr().out


def test_with_require_the_gate_fails_where_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.main(["--require"]) == 1
    assert "cannot run" in capsys.readouterr().out


def test_without_npm_no_bundle_is_built(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    monkeypatch.setattr(mixed_version.shutil, "which", lambda _name: None)
    assert mixed_version.build_frontend(tmp_path) is False


def test_the_next_tree_carries_one_migration_more(tmp_path) -> None:
    migrations = tmp_path / "schemas" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "029_instances.sql").write_text("SELECT 1;\n")
    (migrations / "030_shared_replica_state.sql").write_text("SELECT 1;\n")
    assert mixed_version.add_noop_migration(tmp_path) == "031_mixed_version_gate.sql"
    assert (migrations / "031_mixed_version_gate.sql").read_text().endswith("SELECT 1;\n")


@pytest.mark.parametrize("conflict", [False, True])
def test_an_isolated_container_name_is_removed_only_after_a_successful_start(
    monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    import subprocess
    from types import SimpleNamespace

    calls = []
    monkeypatch.setenv("LOOM_CH_CONTAINER", "test-mixed-isolated")
    monkeypatch.setattr(mixed_version, "_free_port", lambda: 18123)
    monkeypatch.setattr(
        mixed_version.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=200)
    )

    def run(cmd, **kwargs):
        calls.append(cmd)
        if conflict and cmd[:2] == ["docker", "run"]:
            raise subprocess.CalledProcessError(125, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mixed_version.subprocess, "run", run)
    if conflict:
        with pytest.raises(subprocess.CalledProcessError):
            with mixed_version.clickhouse(False):
                pytest.fail("a failed container start must not yield a store")
        assert len(calls) == 1
    else:
        with mixed_version.clickhouse(False) as port:
            assert port == 18123
        assert calls[-1] == ["docker", "rm", "-f", "test-mixed-isolated"]
    assert calls[0][calls[0].index("--name") + 1] == "test-mixed-isolated"


def test_latency_summary_keeps_refusals_and_small_sample_percentiles() -> None:
    from bench.scale_load import latency_observations

    samples = [
        {"elapsed_ms": 2.0, "error": ""},
        {"elapsed_ms": 9.0, "error": ""},
        {"elapsed_ms": 3.0, "error": "503:PROJECTION_BEHIND"},
    ]
    out = latency_observations(samples)
    assert out["attempted"] == 3 and out["n"] == 2
    assert out["p50_ms"] == 2 and out["p99_ms"] == 9
    assert out["errors"] == {"503:PROJECTION_BEHIND": 1}
    assert out["samples"] == samples


@pytest.mark.parametrize("ms", [float("nan"), float("inf"), -1])
def test_latency_summary_refuses_invalid_timings(ms) -> None:
    from bench.scale_load import latency_observations

    with pytest.raises(AssertionError):
        latency_observations([{"elapsed_ms": ms}])


def _latency_written():
    return {
        "memory_key": "k",
        "version": 2,
        "memory_id": "id",
        "status": "active",
        "noop": False,
        "conflict_verdict": "clear",
    }


@pytest.mark.parametrize(
    "delta",
    [
        {"noop": True},
        {"version": 0},
        {"duplicate_of": "other"},
        {"merged_into_key": "other"},
        {"conflict_verdict": ""},
    ],
)
def test_latency_fixture_never_treats_every_200_as_a_commit(delta) -> None:
    from bench.scale_load import committed_latency_rows

    with pytest.raises(AssertionError):
        committed_latency_rows(
            "memory.set", {**_latency_written(), **delta}, [{"memory_key": "k", "value": "v"}]
        )


def test_latency_bulk_checks_complete_outcomes_and_point_endstate() -> None:
    from bench.scale_load import assert_latency_current, committed_latency_rows

    result = {"written": [_latency_written()]}
    expected = committed_latency_rows(
        "memory.bulk_set", result, [{"memory_key": "k", "value": "v"}]
    )
    assert_latency_current({"found": True, "status": "active", **expected["k"]}, expected["k"])
    with pytest.raises(AssertionError):
        committed_latency_rows(
            "memory.bulk_set", {**result, "unchecked": 1}, [{"memory_key": "k", "value": "v"}]
        )
    with pytest.raises(AssertionError):
        assert_latency_current(
            {"found": True, "status": "active", **expected["k"], "value": "old"}, expected["k"]
        )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"memory_key": "k", "version": 1, "status": "active"}],
        [{"memory_key": "k", "version": 2, "status": "rejected"}],
        [{"memory_key": "k", "version": 2, "status": "active"}] * 2,
    ],
)
def test_latency_first_search_cannot_poll_away_a_missing_stale_or_rejected_row(rows) -> None:
    from bench.scale_load import assert_latency_visible

    with pytest.raises(AssertionError):
        assert_latency_visible({"results": rows}, "k", {"version": 2})


def test_latency_first_search_and_real_worker_identity_positive_controls() -> None:
    from bench.scale_load import assert_latency_visible, assert_latency_worker

    assert_latency_visible(
        {"results": [{"memory_key": "k", "version": 2, "status": "active"}]}, "k", {"version": 2}
    )
    assert_latency_worker(102, 100, 100, 100)
    for args in ((100, 100, 100, 100), (102, 100, 999, 100), (102, 100, 100, 999)):
        with pytest.raises(AssertionError):
            assert_latency_worker(*args)


@pytest.fixture
def latency_seed_api(monkeypatch, tmp_path):
    from types import SimpleNamespace

    seeded = {}

    def call(token, op, body):
        assert token == "fixture-token" and op == "memory.bulk_set"
        rows = seeded.setdefault(body["org"], {})
        written = []
        for entry in body["entries"]:
            assert entry["memory_key"] not in rows
            rows[entry["memory_key"]] = dict(entry)
            written.append({**_latency_written(), "memory_key": entry["memory_key"]})
        return {"written": written}

    monkeypatch.setattr(mixed_version, "mint", lambda *_args: "fixture-token")
    monkeypatch.setattr(mixed_version.time, "sleep", lambda _seconds: None)
    return SimpleNamespace(tree=tmp_path, call=call, seeded=seeded)


def test_latency_both_complete_cohorts_exist_before_the_first_measured_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path, latency_seed_api
) -> None:
    from types import SimpleNamespace

    class FirstGroupReached(RuntimeError):
        pass

    stopped = []

    class MeasuredGroup:
        def __init__(self, name, *args, **kwargs):
            self.name = name

        def start(self):
            expected = {
                f"fixture-{arm}-{role}": count
                for arm in ("base", "head")
                for role, count in (("main", 2048), ("light", 16))
            }
            assert {org: len(rows) for org, rows in latency_seed_api.seeded.items()} == expected, (
                "both complete cohorts must exist before the first measured group starts"
            )
            for org, rows in latency_seed_api.seeded.items():
                prefix, count, width = (
                    ("seed", 2048, 4) if org.endswith("-main") else ("light", 16, 2)
                )
                assert set(rows) == {f"{prefix}/{i:0{width}}" for i in range(count)}
            raise FirstGroupReached()

        def stop(self):
            stopped.append(self.name)

    monkeypatch.setattr(mixed_version, "Server", MeasuredGroup)
    scenario = mixed_version.Scenario.__new__(mixed_version.Scenario)
    scenario.org = "fixture"
    scenario.builds = {"N-1": latency_seed_api, "N": SimpleNamespace(tree=tmp_path)}
    scenario.ch_port = 18123
    scenario.owned_logs = tmp_path
    scenario.owned_servers = []
    with pytest.raises(FirstGroupReached):
        scenario.latency_acceptance("base", "head")
    assert stopped == ["latency-base-0", "latency-base-1"]


def test_latency_partial_start_registers_and_stops_both_owned_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path, latency_seed_api
) -> None:
    from types import SimpleNamespace

    owned = []
    stopped = []

    class RefusedStart:
        def __init__(self, name, *args, **kwargs):
            self.name = name

        def start(self):
            assert len(owned) == 2
            raise RuntimeError("start refused")

        def stop(self):
            stopped.append(self.name)

    monkeypatch.setattr(mixed_version, "Server", RefusedStart)
    scenario = mixed_version.Scenario.__new__(mixed_version.Scenario)
    scenario.org = "fixture"
    scenario.builds = {"N-1": latency_seed_api, "N": SimpleNamespace(tree=tmp_path)}
    scenario.ch_port = 18123
    scenario.owned_logs = tmp_path
    scenario.owned_servers = owned
    with pytest.raises(RuntimeError, match="start refused"):
        scenario.latency_acceptance("base", "head")
    assert len(owned) == 2 and stopped == ["latency-base-0", "latency-base-1"]


def test_latency_transport_retains_lag_refusal_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=503,
            text="projection behind",
            json=lambda: {"detail": {"code": "PROJECTION_BEHIND"}},
            elapsed=timedelta(0),
            headers={"X-Request-Id": "fixture-refusal"},
        )

    class Client:
        def __init__(self, *, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def post(self, url, *, extensions, **kwargs):
            return post(url, timeout=self.timeout, **kwargs)

    monkeypatch.setattr(mixed_version.httpx, "Client", Client)
    monkeypatch.setattr(mixed_version, "_free_port", lambda: 18080)
    monkeypatch.setattr(mixed_version.time, "time_ns", lambda: 0)
    monkeypatch.setattr(mixed_version.time, "perf_counter", lambda: 1.0)
    srv = mixed_version.Server("latency-test", tmp_path, 18123, tmp_path)
    observation = {}
    with pytest.raises(AssertionError, match="503"):
        srv.call("token", "memory.search", {}, observation=observation)
    assert len(calls) == 1
    assert calls[0][1]["headers"] == {"Authorization": "Bearer token"}
    assert calls[0][1]["timeout"] == 60
    original = {key: observation[key] for key in ("elapsed_ms", "status", "error")}
    assert original == {"elapsed_ms": 0.0, "status": 503, "error": "503:PROJECTION_BEHIND"}
    assert {key: value for key, value in observation.items() if key not in original} == {
        "started_unix_ns": 0,
        "started_monotonic_s": 1.0,
        "connection_events_ms": {},
        "client_setup_ms": 0.0,
        "finished_unix_ns": 0,
        "httpx_elapsed_ms": 0.0,
        "request_id": "fixture-refusal",
    }


@pytest.fixture
def latency_transport(monkeypatch, tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace

    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=10.0)
    body = {"results": [{"memory_key": "k", "value": "complete private body"}]}
    response = SimpleNamespace(
        status_code=200,
        text="complete private body",
        json=lambda: body,
        elapsed=timedelta(milliseconds=15),
        headers={
            "X-Request-Id": "from-response",
            "Server-Timing": 'app;dur=6, ch;dur=9;desc="concurrent statements"',
            "Authorization": "private header",
        },
    )
    state = SimpleNamespace(
        clock=clock, body=body, response=response, failure=None, clients=[], calls=[], closed=[]
    )

    class Client:
        def __init__(self, **options):
            state.clients.append((self, options))
            clock.advance(0.002)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            clock.advance(0.011)
            state.closed.append(self)

        def post(self, url, **kwargs):
            state.calls.append((url, kwargs))
            trace = kwargs["extensions"]["trace"]
            info = {"private": "private trace credentials/body/SQL/address"}
            trace("private.unrecognized", info)
            clock.advance(0.003)
            trace("connection.connect_tcp.started", info)
            clock.advance(0.005)
            trace("connection.connect_tcp.complete", info)
            trace("http11.send_request_body.complete", info)
            clock.advance(0.007)
            if state.failure is not None:
                raise state.failure
            trace("http11.receive_response_headers.complete", info)
            return state.response

    monkeypatch.setattr(mixed_version.httpx, "Client", Client)
    monkeypatch.setattr(mixed_version.time, "perf_counter", clock.monotonic)
    monkeypatch.setattr(mixed_version.time, "time_ns", lambda: round(clock.t * 1e9))
    monkeypatch.setattr(mixed_version, "_free_port", lambda: 18080)
    state.server = mixed_version.Server("latency-test", tmp_path, 18123, tmp_path)
    return state


def test_observed_calls_keep_cold_clients_complete_results_and_closed_timing(latency_transport):
    import json

    state = latency_transport
    request = {"query": "complete request"}
    samples = [{}, {}]
    for sample in samples:
        assert (
            state.server.call("fixture-token", "memory.search", request, observation=sample)
            == state.body
        )
        assert sample["elapsed_ms"] == pytest.approx(28)
        assert sample["client_setup_ms"] == pytest.approx(2)
        assert sample["httpx_elapsed_ms"] == 15
        assert sample["request_id"] == "from-response"
        assert (sample["status"], sample["error"]) == (200, "")
        assert (sample["server_app_ms"], sample["server_ch_ms"]) == (6, 9)
        assert sample["finished_unix_ns"] - sample["started_unix_ns"] == 28_000_000
        assert sample["connection_events_ms"] == pytest.approx(
            {
                "connection.connect_tcp.started": 5,
                "connection.connect_tcp.complete": 10,
                "http11.send_request_body.complete": 10,
                "http11.receive_response_headers.complete": 17,
            }
        )
        assert set(sample) == {
            "elapsed_ms",
            "client_setup_ms",
            "httpx_elapsed_ms",
            "request_id",
            "status",
            "error",
            "server_app_ms",
            "server_ch_ms",
            "started_unix_ns",
            "started_monotonic_s",
            "finished_unix_ns",
            "connection_events_ms",
        }
        assert "private" not in json.dumps(sample)
    assert len(state.clients) == len(state.closed) == len(state.calls) == 2
    assert state.clients[0][0] is not state.clients[1][0]
    assert state.closed == [client for client, _options in state.clients]
    assert [options for _client, options in state.clients] == [{"timeout": 60}] * 2
    for url, kwargs in state.calls:
        assert url == state.server.url + "/v1/memory.search"
        assert kwargs["json"] is request
        assert kwargs["headers"] == {"Authorization": "Bearer fixture-token"}
        assert set(kwargs) == {"json", "headers", "extensions"}


def test_observed_transport_exception_keeps_identity_and_includes_client_close(latency_transport):
    state = latency_transport
    state.failure = mixed_version.httpx.ReadError("fixture transport error")
    sample = {}
    with pytest.raises(mixed_version.httpx.ReadError) as caught:
        state.server.call("fixture-token", "memory.search", {}, observation=sample)
    assert caught.value is state.failure
    assert sample["elapsed_ms"] == pytest.approx(28)
    assert sample["error"] == "ReadError"
    assert len(state.calls) == len(state.clients) == len(state.closed) == 1
    assert state.closed[0] is state.clients[0][0]


@pytest.mark.parametrize("refused", [False, True], ids=["complete", "same-exception"])
def test_unobserved_call_keeps_the_original_post_path(monkeypatch, latency_transport, refused):
    state = latency_transport
    failure = mixed_version.httpx.ReadError("original transport error")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if refused:
            raise failure
        return state.response

    monkeypatch.setattr(mixed_version.httpx, "post", post)
    request = {"memory_key": "k"}
    if refused:
        with pytest.raises(mixed_version.httpx.ReadError) as caught:
            state.server.call("fixture-token", "memory.get", request)
        assert caught.value is failure
    else:
        assert state.server.call("fixture-token", "memory.get", request) == state.body
    assert state.clients == state.closed == []
    assert calls == [
        (
            state.server.url + "/v1/memory.get",
            {
                "json": request,
                "headers": {"Authorization": "Bearer fixture-token"},
                "timeout": 60,
            },
        )
    ]


@pytest.mark.parametrize("broken", [False, True], ids=["invalid-values", "unreadable-headers"])
def test_malformed_timing_metadata_cannot_replace_the_complete_response(latency_transport, broken):
    state = latency_transport
    state.response.headers = (
        None
        if broken
        else {
            "X-Request-Id": "invalid request id",
            "Server-Timing": "app;dur=invalid, ch;dur=-1, private;dur=10",
        }
    )
    sample = {}
    assert state.server.call("fixture-token", "memory.search", {}, observation=sample) == state.body
    assert (sample["status"], sample["error"]) == (200, "")
    assert sample["elapsed_ms"] == pytest.approx(28)
    assert not {"request_id", "server_app_ms", "server_ch_ms"} & sample.keys()
    assert sample.get("timing_error") == ("AttributeError" if broken else None)
    assert len(state.closed) == len(state.calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["none", "error", "cancel"])
async def test_stage_wrappers_delegate_exact_arguments_results_and_errors(asynchronous, failure):
    import asyncio

    from scripts.gates import _latency_observer as observer

    marker, answer = object(), object()
    error = (
        asyncio.CancelledError("owned cancellation")
        if failure == "cancel"
        else RuntimeError("private")
    )
    calls = []

    def target(*args, **kwargs):
        calls.append((args, kwargs))
        if failure != "none":
            raise error
        return answer

    async def async_target(*args, **kwargs):
        return target(*args, **kwargs)

    record = {"asgi_monotonic_s": observer.time.perf_counter()}
    token = observer._current.set(record)
    try:
        wrapped = observer._timed("auth", async_target if asynchronous else target)
        if failure != "none":
            with pytest.raises(type(error)) as caught:
                if asynchronous:
                    await wrapped(marker, key=marker)
                else:
                    wrapped(marker, key=marker)
            assert caught.value is error
        else:
            result = (
                await wrapped(marker, key=marker) if asynchronous else wrapped(marker, key=marker)
            )
            assert result is answer
        assert calls == [((marker,), {"key": marker})]
        assert record["auth_duration_ms"] >= 0
        assert record["auth_start_ms"] >= 0
        assert len(record) == 3
    finally:
        observer._current.reset(token)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_stage_asgi_delegates_body_errors_and_cancellation_without_leaking_context(cancelled):
    import asyncio

    from scripts.gates import _latency_observer as observer

    error = asyncio.CancelledError("owned cancellation") if cancelled else RuntimeError("private")
    original = {"type": "http.request", "body": b"private body", "more_body": False}
    receives, sends = [], []

    async def receive():
        receives.append(True)
        return original

    async def send(message):
        sends.append(message)

    async def app(scope, receive, send):
        assert await receive() is original
        raise error

    wrapped = observer._ObservedApp(app, {})
    with pytest.raises(type(error)) as caught:
        await wrapped({"type": "http"}, receive, send)
    assert caught.value is error
    assert receives == [True] and sends == []
    assert observer._current.get() is None


async def test_stage_asgi_metadata_is_bounded_private_and_does_not_change_response(monkeypatch):
    import json

    from scripts.gates import _latency_observer as observer
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=10)
    monkeypatch.setattr(observer.time, "perf_counter", clock.monotonic)
    monkeypatch.setattr(observer.time, "time_ns", lambda: 100)
    sources = {
        "main_sha256": "a" * 64,
        "http_sha256": "b" * 64,
        "request_tail_sha256": "c" * 64,
        "namespace_write_marks_sha256": "d" * 64,
    }
    request = {"type": "http.request", "body": b"private argument", "more_body": False}
    body = {"type": "http.response.body", "body": b"complete private response"}
    headers = [(b"set-cookie", b"private cookie")]
    messages = []

    async def receive():
        return request

    async def send(message):
        messages.append(message)

    async def handler(value):
        assert value is request
        clock.advance(0.003)
        return body

    async def app(scope, receive, send):
        assert await receive() is request
        result = await observer._timed("handler", handler)(request)
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send(result)

    await observer._ObservedApp(app, sources)({"type": "http"}, receive, send)
    assert messages[0]["status"] == 200
    assert messages[0]["headers"][:-1] == headers
    name, raw = messages[0]["headers"][-1]
    assert name == observer.HEADER.lower().encode() and len(raw) <= 4096
    metadata = observer.parse_metadata(raw.decode(), sources)
    assert metadata["handler_duration_ms"] == pytest.approx(3)
    assert metadata["body_delivered_ms"] == 0
    assert metadata["pid"] > 0
    assert "private" not in json.dumps(metadata)
    assert messages[1] is body and observer._current.get() is None


@pytest.mark.parametrize("change", ["unknown", "nonfinite", "source", "oversize"])
def test_stage_metadata_refuses_untrusted_fields_numbers_and_sources(change):
    import json

    from scripts.gates import _latency_observer as observer

    sources = {
        "main_sha256": "a" * 64,
        "http_sha256": "b" * 64,
        "request_tail_sha256": "c" * 64,
        "namespace_write_marks_sha256": "d" * 64,
    }
    record = {
        **sources,
        "pid": 123,
        "asgi_unix_ns": 1,
        "asgi_monotonic_s": 1,
        "response_start_ms": 1,
    }
    if change == "unknown":
        record["private_argument"] = "private"
    elif change == "nonfinite":
        record["auth_duration_ms"] = float("nan")
    elif change == "source":
        record["main_sha256"] = "c" * 64
    raw = json.dumps(record) if change != "oversize" else " " * 4097
    with pytest.raises(ValueError):
        observer.parse_metadata(raw, sources)


@pytest.mark.parametrize("wrong_origin", [False, True])
def test_stage_factory_checks_arm_origins_before_importing_server(
    monkeypatch, tmp_path, wrong_origin
):
    from types import SimpleNamespace

    from scripts.gates import _latency_observer as observer

    paths = {
        "server": "server/__init__.py",
        "server.transport.http": "server/transport/http.py",
        "server.main": "server/main.py",
        "server.core.request_tail": "server/core/request_tail.py",
        "server.core.namespace_write_marks": "server/core/namespace_write_marks.py",
    }
    for relative in paths.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# This arm's source.\n")
    http = SimpleNamespace(__file__=str(tmp_path / paths["server.transport.http"]))
    for attribute, _name in observer._STAGES:
        setattr(http, attribute, lambda *args, **kwargs: None)
    app = object()
    main = SimpleNamespace(__file__=str(tmp_path / paths["server.main"]), app=app)
    request_tail = SimpleNamespace(
        __file__=str(tmp_path / paths["server.core.request_tail"]),
        settle=lambda *args, **kwargs: None,
        _publication_barrier=lambda *args, **kwargs: None,
        _ordinary_step=lambda *args, **kwargs: None,
    )
    imports, specs = [], []

    def find_spec(name):
        specs.append(name)
        path = tmp_path / paths[name]
        if wrong_origin and name == "server":
            path = tmp_path / "wrong" / "__init__.py"
        return SimpleNamespace(origin=str(path))

    def import_module(name):
        assert specs == list(paths)
        imports.append(name)
        return {
            "server.transport.http": http,
            "server.main": main,
            "server.core.request_tail": request_tail,
            "server.core.namespace_write_marks": SimpleNamespace(
                __file__=str(tmp_path / paths["server.core.namespace_write_marks"]), KEY_WAIT_S=1.0
            ),
        }[name]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(observer, "find_spec", find_spec)
    monkeypatch.setattr(observer, "import_module", import_module)
    if wrong_origin:
        with pytest.raises(RuntimeError, match="resolved a server outside"):
            observer.create_app()
        assert imports == []
    else:
        wrapped = observer.create_app()
        assert wrapped.app is app and wrapped.sources == observer.source_hashes(tmp_path)
        assert imports == [
            "server.transport.http",
            "server.main",
            "server.core.request_tail",
            "server.core.namespace_write_marks",
        ]


@pytest.mark.parametrize("observed", [False, True])
def test_stage_profile_keeps_exact_arm_pythonpath_and_ordinary_launch(
    monkeypatch, tmp_path, observed
):
    from types import SimpleNamespace

    tree = tmp_path / "arm"
    for relative in (
        "server/main.py",
        "server/transport/http.py",
        "server/core/request_tail.py",
        "server/core/namespace_write_marks.py",
    ):
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# This arm's source.\n")
    calls = []
    monkeypatch.setattr(mixed_version, "_free_port", lambda: 18080)
    monkeypatch.setattr(
        mixed_version.subprocess,
        "Popen",
        lambda command, **kw: calls.append((command, kw)) or SimpleNamespace(poll=lambda: None),
    )
    monkeypatch.setattr(
        mixed_version.httpx, "get", lambda *a, **kw: SimpleNamespace(status_code=200)
    )
    server = mixed_version.Server(
        "latency-test", tree, 18123, tmp_path, web_workers=2, stage_observer=observed
    )
    server.start()
    command, options = calls[0]
    assert options["cwd"] == tree and options["env"]["PYTHONPATH"] == str(tree)
    assert command[:3] == [mixed_version.sys.executable, "-m", "uvicorn"]
    target = (
        [
            "_latency_observer:create_app",
            "--factory",
            "--app-dir",
            str(mixed_version.ROOT / "scripts" / "gates"),
        ]
        if observed
        else ["server.main:app"]
    )
    assert command[3:] == target + ["--host", "127.0.0.1", "--port", "18080", "--workers", "2"]
    options["stdout"].close()


async def test_stage_wrapper_delegates_non_http_scopes_without_observation():
    from scripts.gates import _latency_observer as observer

    scope, receive, send, answer = {"type": "lifespan"}, object(), object(), object()

    async def app(actual_scope, actual_receive, actual_send):
        assert (actual_scope, actual_receive, actual_send) == (scope, receive, send)
        assert observer._current.get() is None
        return answer

    assert await observer._ObservedApp(app, {})(scope, receive, send) is answer


async def test_stage_encoding_failure_keeps_original_response(monkeypatch):
    from scripts.gates import _latency_observer as observer

    start = {"type": "http.response.start", "status": 200, "headers": []}
    body = {"type": "http.response.body", "body": b"complete response"}
    sent = []

    async def app(scope, receive, send):
        await send(start)
        await send(body)

    async def send(message):
        sent.append(message)

    def refused(*args, **kwargs):
        raise ValueError("encoding unavailable")

    monkeypatch.setattr(observer.json, "dumps", refused)
    await observer._ObservedApp(app, {})({"type": "http"}, None, send)
    assert sent[0] is start and sent[1] is body
    assert observer._current.get() is None


@pytest.mark.parametrize("malformed", [False, True])
def test_stage_client_metadata_does_not_change_closed_timing_or_complete_result(
    latency_transport, malformed
):
    import json

    from scripts.gates import _latency_observer as observer

    state = latency_transport
    sources = {
        "main_sha256": "a" * 64,
        "http_sha256": "b" * 64,
        "request_tail_sha256": "c" * 64,
        "namespace_write_marks_sha256": "d" * 64,
    }
    state.server.stage_observer = True
    state.server.stage_sources = sources
    state.response.headers[observer.HEADER] = (
        "invalid"
        if malformed
        else json.dumps(
            {
                **sources,
                "pid": 123,
                "asgi_unix_ns": 1,
                "asgi_monotonic_s": 1,
                "response_start_ms": 1,
            }
        )
    )
    sample = {}
    assert state.server.call("fixture-token", "memory.search", {}, observation=sample) == state.body
    assert sample["elapsed_ms"] == pytest.approx(28)
    assert sample["request_id"] == "from-response"
    assert (sample["status"], sample["error"]) == (200, "")
    assert len(state.calls) == len(state.clients) == len(state.closed) == 1
    if malformed:
        assert "server_stages" not in sample and sample["timing_error"] == "JSONDecodeError"
    else:
        assert sample["server_stages"]["pid"] == 123


@pytest.mark.parametrize(
    "mode", ["complete", "overflow", "parse-failure", "output-limit", "backend-failure"]
)
def test_latency_statement_costs_keep_original_witnesses_and_bound_diagnostics(monkeypatch, mode):
    import json

    scenario = mixed_version.Scenario.__new__(mixed_version.Scenario)
    scenario.ch_port = 18123
    request_id = "00000000-0000-0000-0000-000000000001"
    samples = [{"request_id": request_id, "error": "503:PROJECTION_BEHIND"}]
    requests = [{"request_id": request_id, "latency_ms": 20}]
    native = [{"request_id": request_id, "statements": 1}]
    cost = [1000, 2000, 1, 18446744073709551615, 1, 2, 3, 4, 2, 0, 30, 4000000000, 1000000, 1]
    row = {
        "request_id": request_id,
        "total_statements": 65 if mode == "overflow" else 1,
        "overflow": int(mode == "overflow"),
        "statements": [cost] * (64 if mode == "overflow" else 1),
    }
    calls = []

    def sql(port, query, params=None, settings=None):
        assert port == 18123
        calls.append((query, params, settings))
        if query == "SYSTEM FLUSH LOGS":
            return ""
        if "FROM loom.agent_events" in query:
            return json.dumps(requests[0])
        if "groupArray(64)" not in query:
            return json.dumps(native[0])
        if mode == "backend-failure":
            raise RuntimeError("private backend detail")
        if mode == "parse-failure":
            return "{"
        if mode == "output-limit":
            return " " * 2_097_153
        return json.dumps(row)

    monkeypatch.setattr(mixed_version, "sql", sql)
    out = scenario.latency_timing_evidence(samples, [("main", {"org": "fixture"})])
    assert out["requests"] == requests and out["native"] == native
    assert samples == [{"request_id": request_id, "error": "503:PROJECTION_BEHIND"}]
    bounds = {
        "max_execution_time": 5,
        "max_rows_to_read": 1_000_000,
        "max_memory_usage": 64_000_000,
        "max_result_rows": 121,
        "result_overflow_mode": "throw",
    }
    assert len(calls) == 4 and all(call[2] == bounds for call in calls[:3])
    query, params, limits = calls[-1]
    assert params == calls[1][1] == calls[2][1]
    assert limits == {
        **bounds,
        "max_result_bytes": 2_097_152,
        "output_format_json_quote_64bit_integers": 0,
    }
    assert "request_id IN {ids:Array(String)}" in query
    assert "GROUP BY request_id ORDER BY request_id LIMIT 121" in query
    assert "normalized_query_hash" in query and "count() AS total_statements" in query
    assert not any(name in query for name in ("SELECT *", "query,", "address", "exception,"))
    if mode in ("complete", "overflow"):
        assert out["statement_costs"] == [row]
        assert out.get("statement_cost_error") == (
            "statement_limit" if mode == "overflow" else None
        )
    else:
        assert "statement_costs" not in out
        if mode == "output-limit":
            assert out["statement_cost_error"] == "output_limit"
        else:
            assert out["error"] == (
                "JSONDecodeError" if mode == "parse-failure" else "RuntimeError"
            )
        assert "private backend detail" not in json.dumps(out)


@pytest.fixture
def tail_observer_arm(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts.gates import _latency_observer as observer
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=10)
    monkeypatch.setattr(observer.time, "perf_counter", clock.monotonic)
    monkeypatch.setattr(observer.time, "time_ns", lambda: 100)
    paths = {
        "server": "server/__init__.py",
        "server.transport.http": "server/transport/http.py",
        "server.main": "server/main.py",
        "server.core.request_tail": "server/core/request_tail.py",
        "server.core.namespace_write_marks": "server/core/namespace_write_marks.py",
    }
    for relative in paths.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# This arm's source.\n")
    http = SimpleNamespace(__file__=str(tmp_path / paths["server.transport.http"]))
    for attribute, _name in observer._STAGES:
        setattr(http, attribute, lambda *args, **kwargs: None)
    main = SimpleNamespace(__file__=str(tmp_path / paths["server.main"]), app=None)
    tail = SimpleNamespace(
        __file__=str(tmp_path / paths["server.core.request_tail"]),
        _ordinary_step=lambda *args, **kwargs: None,
    )
    modules = {
        "server.transport.http": http,
        "server.main": main,
        "server.core.request_tail": tail,
        "server.core.namespace_write_marks": SimpleNamespace(
            __file__=str(tmp_path / paths["server.core.namespace_write_marks"]), KEY_WAIT_S=1.0
        ),
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        observer, "find_spec", lambda name: SimpleNamespace(origin=str(tmp_path / paths[name]))
    )
    monkeypatch.setattr(observer, "import_module", lambda name: modules[name])

    def install(app, settle, publication):
        main.app, tail.settle, tail._publication_barrier = app, settle, publication
        return observer.create_app()

    return SimpleNamespace(
        observer=observer, clock=clock, http=http, tail=tail, install=install, tree=tmp_path
    )


async def test_tail_stage_header_locates_post_finish_work_without_changing_delegation(
    tail_observer_arm,
):
    arm = tail_observer_arm
    marker, answer = object(), object()
    calls, sent = [], []

    async def settle(value, *, selected):
        calls.append(("settle", value, selected))
        arm.clock.advance(0.109384679)
        return answer

    async def publication(value, *, selected):
        calls.append(("publication", value, selected))
        arm.clock.advance(0.007)
        return answer

    async def finished():
        arm.clock.advance(0.020)
        return answer

    async def app(scope, receive, send):
        assert await arm.observer._timed("finish", finished)() is answer
        assert await arm.tail.settle(marker, selected=marker) is answer
        assert await arm.tail._publication_barrier(marker, selected=marker) is answer
        await send({"type": "http.response.start", "status": 200, "headers": []})
        return answer

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, settle, publication)
    assert await wrapped({"type": "http"}, None, send) is answer
    raw = sent[0]["headers"][-1][1]
    metadata = arm.observer.parse_metadata(raw.decode(), wrapped.sources)
    assert metadata["tail_settle_start_ms"] == pytest.approx(20)
    assert metadata["tail_settle_duration_ms"] == pytest.approx(109.384679)
    assert metadata["tail_publication_start_ms"] == pytest.approx(129.384679)
    assert metadata["tail_publication_duration_ms"] == pytest.approx(7)
    finish_end = metadata["finish_start_ms"] + metadata["finish_duration_ms"]
    assert metadata["response_start_ms"] - finish_end == pytest.approx(116.384679)
    assert calls == [("settle", marker, marker), ("publication", marker, marker)]
    assert metadata["request_tail_sha256"] == wrapped.sources["request_tail_sha256"]
    assert len(raw) <= 4096 and arm.observer._current.get() is None
    # Outside an observed request, these same wrappers only delegate.
    assert await arm.tail.settle(marker, selected=answer) is answer
    assert await arm.tail._publication_barrier(answer, selected=marker) is answer
    assert calls[2:] == [("settle", marker, answer), ("publication", answer, marker)]
    assert arm.observer._current.get() is None and sent[0]["headers"][-1][1] == raw


async def test_tail_stage_last_outer_completion_replaces_earlier_and_nested_calls(
    tail_observer_arm,
):
    arm = tail_observer_arm
    calls, sent = [], []
    answer, marker = object(), object()

    async def settle(value, *, branch):
        calls.append((value, branch))
        if branch == "early":
            arm.clock.advance(0.002)
        elif branch == "outer":
            arm.clock.advance(0.001)
            assert await arm.tail.settle(value, branch="inner") is answer
            arm.clock.advance(0.004)
        else:
            arm.clock.advance(0.003)
        return answer

    async def publication(value):
        assert value is marker
        arm.clock.advance(0.005)
        return answer

    async def app(scope, receive, send):
        assert await arm.tail.settle(marker, branch="early") is answer
        assert await arm.tail.settle(marker, branch="outer") is answer
        assert await arm.tail._publication_barrier(marker) is answer
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, settle, publication)
    await wrapped({"type": "http"}, None, send)
    metadata = arm.observer.parse_metadata(sent[0]["headers"][-1][1].decode(), wrapped.sources)
    assert calls == [(marker, "early"), (marker, "outer"), (marker, "inner")]
    assert metadata["tail_settle_start_ms"] == pytest.approx(2)
    assert metadata["tail_settle_duration_ms"] == pytest.approx(8)
    assert metadata["tail_publication_start_ms"] == pytest.approx(10)
    assert metadata["tail_publication_duration_ms"] == pytest.approx(5)
    assert metadata["response_start_ms"] == pytest.approx(15)


async def test_tail_stage_sent_header_excludes_later_body_checks_and_final_cleanup(
    tail_observer_arm,
):
    arm = tail_observer_arm
    sent, records, calls = [], [], []
    body = {"type": "http.response.body", "body": b"unchanged response"}

    async def settle(duration):
        calls.append(("settle", duration))
        arm.clock.advance(duration)

    async def publication(duration):
        calls.append(("publication", duration))
        arm.clock.advance(duration)

    async def app(scope, receive, send):
        await arm.tail.settle(0.005)
        await arm.tail._publication_barrier(0.003)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await arm.tail._publication_barrier(0.011)
        await send(body)
        await arm.tail.settle(0.017)
        records.append(dict(arm.observer._current.get()))

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, settle, publication)
    await wrapped({"type": "http"}, None, send)
    raw = sent[0]["headers"][-1][1]
    metadata = arm.observer.parse_metadata(raw.decode(), wrapped.sources)
    assert metadata["tail_settle_duration_ms"] == pytest.approx(5)
    assert metadata["tail_publication_duration_ms"] == pytest.approx(3)
    assert metadata["response_start_ms"] == pytest.approx(8)
    assert records[0]["tail_settle_start_ms"] == pytest.approx(19)
    assert records[0]["tail_settle_duration_ms"] == pytest.approx(17)
    assert records[0]["tail_publication_start_ms"] == pytest.approx(8)
    assert records[0]["tail_publication_duration_ms"] == pytest.approx(11)
    assert sent[1] is body and sent[0]["headers"][-1][1] == raw
    assert calls == [
        ("settle", 0.005),
        ("publication", 0.003),
        ("publication", 0.011),
        ("settle", 0.017),
    ]
    assert arm.observer._current.get() is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_tail_stage_failure_keeps_exact_error_and_no_response(tail_observer_arm, cancelled):
    import asyncio

    arm = tail_observer_arm
    error = asyncio.CancelledError("owned cancellation") if cancelled else RuntimeError("private")
    marker = object()
    calls, records, sent = [], [], []

    async def settle(value, *, selected):
        calls.append((value, selected))
        records.append(arm.observer._current.get())
        arm.clock.advance(0.003)
        raise error

    async def publication(value):
        pytest.fail("publication follows successful settle only")

    async def app(scope, receive, send):
        await arm.tail.settle(marker, selected=marker)
        await arm.tail._publication_barrier(marker)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, settle, publication)
    with pytest.raises(type(error)) as caught:
        await wrapped({"type": "http"}, None, send)
    assert caught.value is error and calls == [(marker, marker)] and sent == []
    assert records[0]["tail_settle_start_ms"] == pytest.approx(0)
    assert records[0]["tail_settle_duration_ms"] == pytest.approx(3)
    assert "tail_publication_duration_ms" not in records[0]
    assert arm.observer._current.get() is None


def test_tail_stage_factory_refuses_wrong_tail_origin_before_wrapping(tail_observer_arm):
    from types import SimpleNamespace

    arm = tail_observer_arm
    observed = []

    async def settle(value):
        observed.append(value)

    async def publication(value):
        observed.append(value)

    async def app(scope, receive, send):
        pytest.fail("a wrong-origin app must not run")

    original_find = arm.observer.find_spec
    arm.observer.find_spec = lambda name: (
        SimpleNamespace(origin=str(arm.tree / "wrong" / "request_tail.py"))
        if name == "server.core.request_tail"
        else original_find(name)
    )
    try:
        with pytest.raises(RuntimeError, match="resolved a server outside"):
            arm.install(app, settle, publication)
        assert arm.tail.settle is settle and arm.tail._publication_barrier is publication
    finally:
        arm.observer.find_spec = original_find
    arm.tail.__file__ = str(arm.tree / "wrong" / "request_tail.py")
    with pytest.raises(RuntimeError, match="imported a server outside"):
        arm.install(app, settle, publication)
    assert arm.tail.settle is settle and arm.tail._publication_barrier is publication
    assert observed == []


@pytest.fixture
def key_step_observer_arm(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts.gates import _latency_observer as observer
    from server.core import request_tail
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=10)
    monkeypatch.setattr(observer.time, "perf_counter", clock.monotonic)
    monkeypatch.setattr(observer.time, "time_ns", lambda: 100)
    paths = {
        "server": "server/__init__.py",
        "server.transport.http": "server/transport/http.py",
        "server.main": "server/main.py",
        "server.core.request_tail": "server/core/request_tail.py",
        "server.core.namespace_write_marks": "server/core/namespace_write_marks.py",
    }
    for relative in paths.values():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# This arm's source.\n")
    http = SimpleNamespace(__file__=str(tmp_path / paths["server.transport.http"]))
    for attribute, _name in observer._STAGES:
        setattr(http, attribute, lambda *args, **kwargs: None)
    main = SimpleNamespace(__file__=str(tmp_path / paths["server.main"]), app=None)
    tail = SimpleNamespace(__file__=str(tmp_path / paths["server.core.request_tail"]))
    marks = SimpleNamespace(
        __file__=str(tmp_path / paths["server.core.namespace_write_marks"]), KEY_WAIT_S=1.0
    )
    modules = {
        "server.transport.http": http,
        "server.main": main,
        "server.core.request_tail": tail,
        "server.core.namespace_write_marks": marks,
    }
    monkeypatch.chdir(tmp_path)
    # Both the original and proposed factories resolve their own known modules.
    monkeypatch.setattr(
        observer, "find_spec", lambda name: SimpleNamespace(origin=str(tmp_path / paths[name]))
    )
    monkeypatch.setattr(observer, "import_module", lambda name: modules[name])

    async def noop(*args, **kwargs):
        return None

    def install(app, ordinary):
        main.app = app
        tail.settle = tail._publication_barrier = noop
        tail._ordinary_step = ordinary
        return observer.create_app()

    return SimpleNamespace(
        observer=observer,
        clock=clock,
        tail=tail,
        marks=marks,
        real_ordinary=request_tail._ordinary_step,
        install=install,
        tree=tmp_path,
    )


@pytest.mark.parametrize("completed", [False, True])
async def test_key_step_header_observes_real_wait_and_done_state(key_step_observer_arm, completed):
    import asyncio
    from functools import partial
    from types import SimpleNamespace

    arm = key_step_observer_arm
    calls, sent = [], []
    answer, tag = object(), "private-key-tag"
    tail = SimpleNamespace(has_required=False)

    async def child():
        arm.clock.advance(0.012)
        return answer

    task = asyncio.create_task(child(), name="loom-write-key")
    if completed:
        assert await task is answer
    step = partial(asyncio.wait, [task], timeout=1.0)

    async def ordinary(*args, **kwargs):
        calls.append((args, kwargs))
        assert await arm.real_ordinary(*args, **kwargs) is None
        arm.clock.advance(0.003)
        return answer

    async def app(scope, receive, send):
        assert await arm.tail._ordinary_step(tail, tag, step) is answer
        await send({"type": "http.response.start", "status": 200, "headers": []})
        return answer

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, ordinary)
    assert await wrapped({"type": "http"}, None, send) is answer
    assert calls == [((tail, tag, step), {})] and task.done() and not task.cancelled()
    raw = sent[0]["headers"][-1][1]
    metadata = arm.observer.parse_metadata(raw.decode(), wrapped.sources)
    # Old observer reaches this successful response, then lacks these fields.
    assert metadata["tail_key_step_duration_ms"] == pytest.approx(3 if completed else 15)
    assert metadata["tail_key_step_start_ms"] == pytest.approx(0)
    assert metadata["tail_key_done_at_entry"] == int(completed)
    assert metadata["tail_key_done_at_return"] == 1
    assert (
        metadata["namespace_write_marks_sha256"] == wrapped.sources["namespace_write_marks_sha256"]
    )
    assert b"private" not in raw and b"loom-write-key" not in raw and len(raw) <= 4096
    assert arm.observer._current.get() is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_key_step_observation_preserves_exact_error_and_pending_child(
    key_step_observer_arm, cancelled
):
    import asyncio
    from functools import partial
    from types import SimpleNamespace

    arm = key_step_observer_arm
    answer = object()
    gate = asyncio.Event()
    records, calls, sent = [], [], []
    error = asyncio.CancelledError("owned cancel") if cancelled else RuntimeError("private")

    async def child():
        await gate.wait()
        return answer

    task = asyncio.create_task(child(), name="loom-write-key")
    step = partial(asyncio.wait, [task], timeout=1.0)
    tail = SimpleNamespace(has_required=False)

    async def ordinary(*args, **kwargs):
        calls.append((args, kwargs))
        records.append(arm.observer._current.get())
        arm.clock.advance(0.004)
        raise error

    async def app(scope, receive, send):
        await arm.tail._ordinary_step(tail, "private", step)

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, ordinary)
    try:
        with pytest.raises(type(error)) as caught:
            await wrapped({"type": "http"}, None, send)
        assert caught.value is error and calls == [((tail, "private", step), {})]
        assert not task.done() and sent == [] and arm.observer._current.get() is None
        assert records[0]["tail_key_step_duration_ms"] == pytest.approx(4)
        assert records[0]["tail_key_done_at_entry"] == 0
        assert records[0]["tail_key_done_at_return"] == 0
    finally:
        gate.set()
        assert await task is answer


async def test_key_step_sequential_calls_and_later_cleanup_do_not_rewrite_header(
    key_step_observer_arm,
):
    import asyncio
    from functools import partial
    from types import SimpleNamespace

    arm = key_step_observer_arm
    tail = SimpleNamespace(has_required=False)
    calls, sent, records = [], [], []

    async def child():
        return None

    task = asyncio.create_task(child(), name="loom-write-key")
    await task
    step = partial(asyncio.wait, [task], timeout=1.0)

    async def ordinary(*args, **kwargs):
        calls.append((args, kwargs))
        await arm.real_ordinary(*args, **kwargs)
        arm.clock.advance(0.002 * len(calls))

    async def app(scope, receive, send):
        await arm.tail._ordinary_step(tail, "private", step)
        await arm.tail._ordinary_step(tail, "private", step)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await arm.tail._ordinary_step(tail, "private", step)
        records.append(dict(arm.observer._current.get()))

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, ordinary)
    await wrapped({"type": "http"}, None, send)
    raw = sent[0]["headers"][-1][1]
    metadata = arm.observer.parse_metadata(raw.decode(), wrapped.sources)
    assert metadata["tail_key_step_start_ms"] == pytest.approx(2)
    assert metadata["tail_key_step_duration_ms"] == pytest.approx(4)
    assert metadata["response_start_ms"] == pytest.approx(6)
    assert records[0]["tail_key_step_start_ms"] == pytest.approx(6)
    assert records[0]["tail_key_step_duration_ms"] == pytest.approx(6)
    assert sent[0]["headers"][-1][1] == raw and len(calls) == 3
    # Outside an observed request the same function only delegates.
    await arm.tail._ordinary_step(tail, "private", step)
    assert len(calls) == 4 and arm.observer._current.get() is None


@pytest.mark.parametrize("ineligible", ["name", "timeout", "keyword", "function"])
async def test_key_step_unrecognized_callbacks_keep_ordinary_behavior(
    key_step_observer_arm, ineligible
):
    import asyncio
    from functools import partial
    from types import SimpleNamespace

    arm = key_step_observer_arm
    tail = SimpleNamespace(has_required=False)
    calls, sent = [], []
    answer = object()

    async def child():
        return answer

    task = asyncio.create_task(child(), name="other" if ineligible == "name" else "loom-write-key")
    assert await task is answer
    step = partial(asyncio.wait, [task], timeout=0.5 if ineligible == "timeout" else 1.0)
    if ineligible == "keyword":
        step = partial(asyncio.wait, fs=[task], timeout=1.0)
    elif ineligible == "function":
        step = child

    async def ordinary(*args, **kwargs):
        calls.append((args, kwargs))
        await arm.real_ordinary(*args, **kwargs)
        return answer

    async def app(scope, receive, send):
        assert await arm.tail._ordinary_step(tail, "private", step) is answer
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, ordinary)
    await wrapped({"type": "http"}, None, send)
    metadata = arm.observer.parse_metadata(sent[0]["headers"][-1][1].decode(), wrapped.sources)
    assert calls == [((tail, "private", step), {})]
    assert not any(key.startswith("tail_key_") for key in metadata)
    assert task.done() and not task.cancelled()


@pytest.mark.parametrize("value", [True, False, 0.0, 1.0, 2, -1])
def test_key_step_metadata_done_state_requires_integer_zero_or_one(key_step_observer_arm, value):
    import json

    arm = key_step_observer_arm
    sources = arm.observer.source_hashes(arm.tree)
    record = {
        **sources,
        "pid": 1,
        "asgi_unix_ns": 1,
        "asgi_monotonic_s": 1,
        "response_start_ms": 1,
        "tail_key_done_at_entry": value,
    }
    with pytest.raises(ValueError):
        arm.observer.parse_metadata(json.dumps(record), sources)


async def test_key_step_bounded_wait_does_not_complete_or_cancel_pending_child(
    key_step_observer_arm, monkeypatch
):
    import asyncio
    from functools import partial
    from types import SimpleNamespace

    arm = key_step_observer_arm
    gate = asyncio.Event()
    calls, sent = [], []

    async def child():
        await gate.wait()

    task = asyncio.create_task(child(), name="loom-write-key")

    async def bounded_wait(tasks, **options):
        assert options == {"timeout": 1.0}
        timeout = options["timeout"]
        calls.append((tasks, timeout))
        assert tasks == [task] and timeout == 1.0 and not task.done()
        arm.clock.advance(0.008)
        return set(), {task}

    monkeypatch.setattr(asyncio, "wait", bounded_wait)
    step = partial(asyncio.wait, [task], timeout=1.0)
    tail = SimpleNamespace(has_required=False)

    async def app(scope, receive, send):
        assert await arm.tail._ordinary_step(tail, "private", step) is None
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    wrapped = arm.install(app, arm.real_ordinary)
    try:
        await wrapped({"type": "http"}, None, send)
        metadata = arm.observer.parse_metadata(sent[0]["headers"][-1][1].decode(), wrapped.sources)
        assert metadata["tail_key_step_duration_ms"] == pytest.approx(8)
        assert metadata["tail_key_done_at_entry"] == metadata["tail_key_done_at_return"] == 0
        assert calls == [([task], 1.0)] and not task.done() and not task.cancelled()
    finally:
        gate.set()
        await task


@pytest.mark.parametrize("before_import", [False, True])
def test_key_step_defining_source_origin_is_verified(
    key_step_observer_arm, monkeypatch, before_import
):
    from types import SimpleNamespace

    arm = key_step_observer_arm
    original = arm.observer.find_spec
    if before_import:
        monkeypatch.setattr(
            arm.observer,
            "find_spec",
            lambda name: (
                SimpleNamespace(origin=str(arm.tree / "wrong.py"))
                if name == "server.core.namespace_write_marks"
                else original(name)
            ),
        )
    else:
        arm.marks.__file__ = str(arm.tree / "wrong.py")
    with pytest.raises(RuntimeError, match="outside its arm"):
        arm.install(None, lambda *args, **kwargs: None)
