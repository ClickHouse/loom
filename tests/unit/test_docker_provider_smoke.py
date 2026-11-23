"""The smoke fixture is deterministic and isolates recovery from paid providers."""

import io
import json
import threading
import urllib.error
import urllib.request

import pytest

import scripts.gates.docker_provider_data as data
import scripts.gates.docker_provider_server as fixture
import scripts.gates.docker_provider_smoke as smoke
from scripts.gates.docker_quickstart_smoke import provider_fixture


@pytest.fixture
def provider():
    server = fixture.ProviderServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(base, path, payload, key="docker-smoke-embedding-key"):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def test_provider_returns_exact_repeatable_model_dimensions(provider):
    for model, dimensions in fixture.DIMENSIONS.items():
        payload = {"model": model, "input": ["same text", "other text"]}
        result = post(provider, "/v1/embeddings", payload)
        assert result == post(provider, "/v1/embeddings", payload)
        vectors = [entry["embedding"] for entry in result["data"]]
        assert [entry["index"] for entry in result["data"]] == [0, 1]
        assert all(len(vector) == dimensions for vector in vectors)
        assert vectors[0] != vectors[1]


def test_injected_failure_recovers_without_changing_model_or_data(provider):
    payload = {"model": "docker-smoke-16", "input": ["context"]}
    expected = post(provider, "/v1/embeddings", payload)
    post(provider, "/control", {"failed_model": "docker-smoke-16"})
    assert post(
        provider,
        "/v1/embeddings",
        {"model": "docker-smoke-16", "input": ["Loom provider connection check."]},
    )["data"]
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/embeddings", payload)
    assert error.value.code == 400
    post(provider, "/control", {"failed_model": ""})
    assert post(provider, "/v1/embeddings", payload, key="docker-smoke-rotated-key") == expected


def test_llm_transport_fixture_still_checks_credentials(provider):
    result = post(provider, "/v1/responses", {}, key="docker-smoke-llm-key")
    assert result["output"][0]["content"][0]["text"] == "OK"
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/responses", {}, key="wrong")
    assert error.value.code == 401


def test_fixture_rejects_unknown_credentials(provider):
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/embeddings", {"model": "docker-smoke-8", "input": ["text"]}, "wrong")
    assert error.value.code == 401


def test_fixture_is_not_exposed_outside_the_generated_network(tmp_path):
    configuration = json.loads(provider_fixture("loom:unit-smoke", tmp_path).read_text())
    services = configuration["services"]
    assert services["provider-smoke"]["image"] == "loom:unit-smoke"
    assert "ports" not in services["provider-smoke"]
    assert "environment" not in services["provider-smoke"]
    assert services["loom"]["depends_on"]["provider-smoke"]["condition"] == "service_healthy"


def test_key_rotation_omission_preserves_saved_credential_and_current_version(monkeypatch):
    calls = []

    def request(base, path, payload=None):
        calls.append((path, payload))
        return {"version": "18446744073709551615"} if payload is None else {"state": "active"}

    monkeypatch.setattr(smoke, "request", request)
    smoke.configure("http://localhost", "docker-smoke-8", 8)
    payload = calls[-1][1]
    assert payload["expected_version"] == "18446744073709551615"
    assert payload["keys"] == {}


def test_failed_status_is_an_error_unless_explicitly_waiting_for_failure(monkeypatch):
    failed = {"pending": {"state": "failed", "error_code": "rebuild_rebuild_failed"}}
    monkeypatch.setattr(smoke, "request", lambda *_: failed)
    with pytest.raises(AssertionError, match="Provider rebuild failed"):
        smoke.wait_status("http://localhost", lambda _: False)
    assert (
        smoke.wait_status("http://localhost", lambda row: row["pending"]["state"] == "failed")
        == failed
    )


@pytest.mark.asyncio
async def test_data_check_requires_no_retained_rebuild_copies(monkeypatch):
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.query_one.side_effect = [
        {
            "embedding_model": "model",
            "embedding_dim": 8,
            "memory_key_embedding_dim": 8,
            "width": 8,
            "verified": 1,
            "tags": ["provider-smoke"],
            "content_excerpt": "Provider rebuild preserves metadata",
        },
        {
            "embedding_model": "model",
            "embedding_dim": 8,
            "width": 8,
            "content": "def greet(): return 42",
            "start_line": 1,
        },
        {"n": 1},
    ]
    monkeypatch.setattr(data, "get_client", lambda: client)
    with pytest.raises(AssertionError):
        await data.verify("model", 8)


def test_crash_keeps_the_supervisor_and_verifies_retirement_before_reset(monkeypatch, tmp_path):
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"services": {"loom": {}}}))
    compose = ["docker", "compose", "-f", str(override)]
    events = []
    environment = {
        "source": "environment",
        "embedding_model": "environment",
        "embedding_dimensions": 8,
        "llm_model": "fake",
    }
    active = {
        "source": "dashboard",
        "pending": None,
        "embedding_model": "docker-smoke-16",
        "embedding_dimensions": 16,
    }
    statuses = iter(
        [
            {
                **active,
                "embedding_model": "docker-smoke-8",
                "configured_keys": {"compatible": True},
            },
            {
                "embedding_model": "docker-smoke-8",
                "pending": {"state": "failed", "error_code": "rebuild_rebuild_failed"},
            },
            active,
            {**environment, "pending": None},
        ]
    )
    model_reads = iter([environment, active])

    def request(_base, path, _payload=None):
        if path == "/v1/admin/models":
            return next(model_reads)
        raise urllib.error.HTTPError("http://fixture", 503, "closed", {}, None)

    def configure(*_args):
        raise urllib.error.HTTPError(
            "http://fixture", 422, "width", {}, io.BytesIO(b"unsupported_dimensions")
        )

    applications = iter(["queued", "active", "queued"])

    def dashboard(_base, action, *_args):
        events.append(action)
        return {"state": next(applications) if action == "apply" else "queued"}

    def check(_compose, *args):
        events.append(args[0])
        return (
            [{"participant_id": "first"}, {"participant_id": "second"}]
            if args[0] == "workers"
            else None
        )

    def status(_base, predicate):
        value = next(statuses)
        assert predicate(value)
        return value

    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(smoke, "configure", configure)
    monkeypatch.setattr(smoke, "dashboard_action", dashboard)
    monkeypatch.setattr(smoke, "data_check", check)
    monkeypatch.setattr(smoke, "wait_status", status)
    monkeypatch.setattr(smoke, "wait_admission", lambda *_: None)
    monkeypatch.setattr(smoke, "control", lambda *_: None)
    monkeypatch.setattr(smoke, "run", lambda *args: events.append(tuple(args)))
    smoke.smoke_providers("http://fixture", compose)
    assert not any(isinstance(event, tuple) and "SIGKILL" in event for event in events)
    assert events.index("retry") < events.index("crash-worker") < events.index("workers")
    recreate = [
        i
        for i, event in enumerate(events)
        if isinstance(event, tuple) and "--force-recreate" in event
    ]
    assert len(recreate) == 2
    assert events.index("workers") < recreate[-1] < events.index("retired") < events.index("reset")


def _process_fixture(monkeypatch):
    from multiprocessing import spawn
    from types import SimpleNamespace

    parent = {
        "pid": 1,
        "parent": 0,
        "start": "10",
        "namespace": 1,
        "executable": "/python",
        "command": ["python", *data.SUPERVISOR],
    }
    processes = {
        pid: {
            "pid": pid,
            "parent": 1,
            "start": str(pid * 10),
            "namespace": 1,
            "executable": "/python",
            "command": [
                str(value, "utf-8") if isinstance(value, bytes) else value
                for value in spawn.get_command_line(tracker_fd=4, pipe_handle=8)
            ],
        }
        for pid in (8, 9)
    }
    rows = [
        {
            "participant_id": str(pid),
            "children": [],
            "version": 5,
            "state": "running",
            "host_proof": f"h|b|c|1|{pid}|{pid * 10}|||cell-retirement-v1",
        }
        for pid in (8, 9)
    ]
    monkeypatch.setattr(data.provider_process, "host_proof", lambda: "h|b|c|1|99|990||")
    monkeypatch.setattr(data, "_process", lambda pid: processes[pid])
    monkeypatch.setattr(data, "Path", lambda _path: SimpleNamespace(read_text=lambda: "8 9"))
    return parent, processes, rows


def test_worker_selection_joins_kernel_children_without_treating_other_participants_as_workers(
    monkeypatch,
):
    parent, processes, rows = _process_fixture(monkeypatch)
    processes[44] = {
        **processes[8],
        "pid": 44,
        "parent": 0,
        "start": "440",
        "command": ["python", "-", "seed"],
    }
    mixed = [
        {
            **rows[0],
            "participant_id": "native",
            "host_proof": "h|b|c|1|44|440|||cell-retirement-v1",
        },
        {
            **rows[0],
            "participant_id": "reused-pid",
            "host_proof": "h|b|c|1|8|79|||cell-retirement-v1",
        },
        {**rows[0], "participant_id": "gone", "host_proof": "h|b|c|1|66|660|||cell-retirement-v1"},
        *rows,
    ]
    assert data._workers(mixed, parent) == [
        {k: row[k] for k in ("participant_id", "host_proof", "version")} for row in rows
    ]
    assert [row["participant_id"] for row in mixed] == ["native", "reused-pid", "gone", "8", "9"]


def test_worker_command_uses_exact_inherited_interpreter_flags(monkeypatch):
    from multiprocessing import spawn, util

    parent, processes, rows = _process_fixture(monkeypatch)
    monkeypatch.setattr(util, "_args_from_interpreter_flags", lambda: ["-B"])
    command = [
        str(value, "utf-8") if isinstance(value, bytes) else value
        for value in spawn.get_command_line(tracker_fd=4, pipe_handle=8)
    ]
    processes[8] = {**processes[8], "command": command}
    assert data._worker_process(rows[0]["host_proof"], parent) == processes[8]
    for flags in (["-E"], ["-B", "-X", "unknown"]):
        processes[8] = {**processes[8], "command": [command[0], *flags, *command[-3:]]}
        with pytest.raises(AssertionError, match="interpreter flags"):
            data._worker_process(rows[0]["host_proof"], parent)


def test_kernel_workers_never_ignore_missing_or_mismatched_current_proofs(monkeypatch):
    parent, processes, rows = _process_fixture(monkeypatch)
    for proof in (
        None,
        "h|b|c|1|8|79|||cell-retirement-v1",
        "h|b|c|2|8|80|||cell-retirement-v1",
        "h|b|c|1|8|80|||unknown",
    ):
        changed = rows[1:] if proof is None else [{**rows[0], "host_proof": proof}, rows[1]]
        with pytest.raises(AssertionError, match="exact registrations|certificate invalid"):
            data._workers(changed, parent)
    for field, value in (("namespace", 2), ("executable", "/other-python")):
        original = processes[8]
        processes[8] = {**original, field: value}
        with pytest.raises(AssertionError, match=f"{field} mismatch"):
            data._workers(rows, parent)
        processes[8] = original


def test_worker_selection_requires_registered_supervised_processes(monkeypatch):
    parent, processes, rows = _process_fixture(monkeypatch)
    foreign = {**rows[0], "host_proof": "h|b|c|2|8|80|||cell-retirement-v1"}
    assert data._workers([*rows, foreign], parent) == [
        {k: row[k] for k in ("participant_id", "host_proof", "version")} for row in rows
    ]
    processes[8] = {**processes[8], "parent": 2}
    with pytest.raises(AssertionError):
        data._workers(rows, parent)
    processes[8] = {
        **processes[8],
        "parent": 1,
        "command": ["python", "-c", "from multiprocessing.resource_tracker import main; main(4)"],
    }
    with pytest.raises(AssertionError):
        data._workers(rows, parent)


@pytest.mark.parametrize("fault", ["none", "pid-reused", "supervisor-changed"])
def test_pidfd_crash_refuses_changed_identity_before_signalling(monkeypatch, fault):
    parent, processes, rows = _process_fixture(monkeypatch)
    events = []
    observed = iter(
        [processes[8], {**processes[8], "start": "81"} if fault == "pid-reused" else processes[8]]
    )
    monkeypatch.setattr(data, "_process", lambda _pid: next(observed))
    monkeypatch.setattr(
        data,
        "_supervisor",
        lambda: {**parent, "start": "11"} if fault == "supervisor-changed" else parent,
    )
    monkeypatch.setattr(data.os, "pidfd_open", lambda pid: events.append(("open", pid)) or 73)
    monkeypatch.setattr(data.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(
        data.signal, "pidfd_send_signal", lambda fd, sig: events.append(("signal", fd, sig))
    )
    if fault == "none":
        data._kill_worker(rows[0], parent)
        assert events == [("open", 8), ("signal", 73, data.signal.SIGKILL), ("close", 73)]
    else:
        with pytest.raises(AssertionError):
            data._kill_worker(rows[0], parent)
        assert events == [("open", 8), ("close", 73)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["none", "drained", "old-version", "different-proof", "busy", "unknown"]
)
async def test_retirement_requires_exact_newer_certificate_and_known_zero_queries(
    monkeypatch, fault
):
    from unittest.mock import AsyncMock

    from server.insights.store import provider_participants

    _parent, _processes, rows = _process_fixture(monkeypatch)
    observed = {**rows[0], "state": "retired", "version": 6}
    if fault == "drained":
        observed["state"] = "drained"
    elif fault == "old-version":
        observed["version"] = 5
    elif fault == "different-proof":
        observed["host_proof"] = rows[1]["host_proof"]
    client = AsyncMock()
    client.query_all.return_value = [observed]
    count = 1 if fault == "busy" else None if fault == "unknown" else 0
    monkeypatch.setattr(provider_participants, "running_queries", AsyncMock(return_value=count))
    assert await data.retired(client, [rows[0]]) is (fault == "none")
    assert client.query_all.await_args.args[1] == {"ids": [rows[0]["participant_id"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,reason",
    [
        ("death_unknown", '"death": "unproved"'),
        ("missing_rows", '"retirement": "row_count"'),
        ("queries_unknown", '"retirement": "queries_unknown"'),
        ("replacement_mismatch", '"victim_absent": false'),
        ("none", None),
    ],
)
async def test_crash_observation_distinguishes_unproved_predicates(monkeypatch, fault, reason):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.insights.store import provider_participants

    parent, _processes, rows = _process_fixture(monkeypatch)
    monkeypatch.setattr(data, "_supervisor", lambda: parent)
    monkeypatch.setattr(data, "workers", AsyncMock(return_value=rows))
    signals = []
    monkeypatch.setattr(data, "_kill_worker", lambda victim, _: signals.append(victim))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _: fault != "death_unknown")
    victim = rows[0]
    replacement = {**victim, "participant_id": "replacement"}
    after = rows if fault == "replacement_mismatch" else [rows[1], replacement]
    monkeypatch.setattr(data, "_workers", lambda *_: after)
    client = AsyncMock()
    client.query_all.side_effect = [
        [rows[0]],
        [rows[1]],
        [] if fault == "missing_rows" else [{**victim, "state": "retired", "version": 6}],
        [victim],
    ]
    monkeypatch.setattr(
        provider_participants,
        "running_queries",
        AsyncMock(return_value=None if fault == "queries_unknown" else 0),
    )
    ticks = iter([0, 0, 241])
    monkeypatch.setattr(data, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(data.asyncio, "sleep", AsyncMock())
    if reason is None:
        await data.crash_worker(client)
    else:
        with pytest.raises(AssertionError) as failure:
            await data.crash_worker(client)
        assert reason in str(failure.value)
        assert victim["host_proof"] not in str(failure.value)
        assert victim["participant_id"] not in str(failure.value)
    assert signals == [victim]
    reads = client.query_all.await_args_list
    assert [call.args[1] for call in reads[:2]] == [
        {"ids": [row["participant_id"]]} for row in rows
    ]
    assert all(
        "SELECT participant_id, state, version, host_proof" in call.args[0] for call in reads[:2]
    )
    if fault == "missing_rows":
        assert '"reference_count": 1' in str(failure.value)
        assert '"reference_identity": true' in str(failure.value)
        assert "SELECT participant_id FROM" in reads[-1].args[0]
    else:
        assert all("SELECT participant_id FROM" not in call.args[0] for call in reads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["missing-first", "missing-second", "wrong-identity", "reference-error"]
)
async def test_crash_refuses_unreadable_captured_worker_before_signalling(monkeypatch, fault):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    parent, _processes, rows = _process_fixture(monkeypatch)
    monkeypatch.setattr(data, "_supervisor", lambda: parent)
    monkeypatch.setattr(data, "workers", AsyncMock(return_value=rows))
    signals = []
    monkeypatch.setattr(data, "_kill_worker", lambda victim, _: signals.append(victim))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _: True)
    client = AsyncMock()

    async def query(sql, parameters):
        selected = next(row for row in rows if [row["participant_id"]] == parameters["ids"])
        if sql.startswith("SELECT participant_id FROM"):
            if fault == "reference-error":
                raise RuntimeError("private diagnostic failure")
            return [selected]
        if fault == "missing-second" and selected == rows[0]:
            return [selected]
        return [rows[1]] if fault == "wrong-identity" else []

    client.query_all.side_effect = query
    ticks = iter([0, 0, 241])
    monkeypatch.setattr(data, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(data.asyncio, "sleep", AsyncMock())
    with pytest.raises(AssertionError, match="Captured worker readback failed") as failure:
        await data.crash_worker(client)
    assert signals == []
    message = str(failure.value)
    assert '"reader_identity": false' in message
    assert f'"reader_count": {int(fault == "wrong-identity")}' in message
    if fault == "reference-error":
        assert '"reference_read": "query_error"' in message
        assert "private diagnostic failure" not in message
    else:
        assert '"reference_count": 1' in message
        assert '"reference_identity": true' in message
    assert all(row["host_proof"] not in message for row in rows)
    assert [call.args[1] for call in client.query_all.await_args_list] == (
        [{"ids": [rows[0]["participant_id"]]}] if fault == "missing-second" else []
    ) + [{"ids": [rows[int(fault == "missing-second")]["participant_id"]]}] * 2


@pytest.mark.asyncio
async def test_retirement_reader_preserves_string_identity_from_native_uuid():
    from unittest.mock import AsyncMock
    from uuid import UUID

    participant_id = UUID("00112233-4455-6677-8899-aabbccddeeff")
    row = {
        "participant_id": participant_id,
        "state": "retired",
        "version": 6,
        "host_proof": "proof",
    }
    client = AsyncMock()
    client.query_all.return_value = [row]
    captured = [{**row, "participant_id": str(participant_id)}]
    assert await data._retirement_rows(client, captured) == captured
    assert row["participant_id"] is participant_id
    assert client.query_all.await_args.args == (
        "SELECT participant_id, state, version, host_proof "
        "FROM loom._provider_participants FINAL WHERE participant_id IN {ids:Array(UUID)} LIMIT 5",
        {"ids": [str(participant_id)]},
    )


@pytest.mark.parametrize(
    "fault", ["gone", "zombie", "dead", "live", "permission", "unreadable", "malformed", "one-live"]
)
def test_worker_census_omits_only_proved_exited_children(monkeypatch, fault):
    from types import SimpleNamespace

    parent, _processes, rows = _process_fixture(monkeypatch)
    process = data._process

    def observed_process(pid):
        if pid == 7:
            if fault == "permission":
                raise PermissionError("unreadable child executable")
            raise FileNotFoundError("disappeared child executable")
        return process(pid)

    def read(path):
        if path == "/proc/1/task/1/children":
            return "7 8" if fault == "one-live" else "7 8 9"
        assert path == "/proc/7/stat"
        if fault == "gone":
            raise FileNotFoundError("reaped child")
        if fault == "unreadable":
            raise PermissionError("unreadable child stat")
        if fault == "malformed":
            return "7 (incomplete child)"
        state = "X" if fault == "dead" else "R" if fault == "live" else "Z"
        return f"7 (exited child) {state} 1"

    monkeypatch.setattr(data, "_process", observed_process)
    monkeypatch.setattr(data, "Path", lambda path: SimpleNamespace(read_text=lambda: read(path)))
    if fault in {"gone", "zombie", "dead"}:
        assert data._workers(rows, parent) == [
            {key: row[key] for key in ("participant_id", "host_proof", "version")} for row in rows
        ]
    else:
        with pytest.raises((OSError, IndexError, AssertionError)):
            data._workers(rows, parent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["spawn", "registration", "timeout", "duplicate", "certificate", "namespace"]
)
async def test_crash_waits_only_for_incomplete_replacement_census(monkeypatch, fault):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    parent, processes, rows = _process_fixture(monkeypatch)
    replacement = {
        **rows[0],
        "participant_id": "replacement",
        "host_proof": "h|b|c|1|10|100|||cell-retirement-v1",
    }
    processes[10] = {**processes[8], "pid": 10, "start": "100"}
    if fault == "certificate":
        replacement["host_proof"] = "h|b|c|1|10|100|||unknown"
    if fault == "namespace":
        processes[10]["namespace"] = 2
    stage = 0

    def children(_path):
        pending = fault in {"spawn", "timeout"} and stage == 0
        return SimpleNamespace(read_text=lambda: "9" if pending else "9 10")

    async def registered(_client):
        if fault == "registration" and stage == 0:
            return [rows[1]]
        if fault == "duplicate":
            return [rows[1], replacement, {**replacement, "participant_id": "ambiguous"}]
        return [rows[1], replacement]

    async def pause(_seconds):
        nonlocal stage
        stage += 1

    monkeypatch.setattr(data, "Path", children)
    monkeypatch.setattr(data, "_supervisor", lambda: parent)
    monkeypatch.setattr(data, "workers", AsyncMock(return_value=rows))
    monkeypatch.setattr(data, "_worker_rows", registered)
    monkeypatch.setattr(data, "_retirement_rows", AsyncMock(side_effect=[[rows[0]], [rows[1]]]))
    monkeypatch.setattr(data, "retired", AsyncMock(return_value=True))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _proof: True)
    signals = []
    monkeypatch.setattr(data, "_kill_worker", lambda victim, _parent: signals.append(victim))
    ticks = iter([0, 0, 241] if fault == "timeout" else [0, 0, 1, 241])
    monkeypatch.setattr(data, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(data.asyncio, "sleep", pause)
    if fault in {"spawn", "registration"}:
        await data.crash_worker(object())
        assert stage == 1
    else:
        pattern = {
            "timeout": "supervised replacement was not proved",
            "duplicate": "exact registrations: 2",
            "certificate": "certificate invalid",
            "namespace": "namespace mismatch",
        }[fault]
        with pytest.raises(AssertionError, match=pattern):
            await data.crash_worker(object())
        assert stage == int(fault == "timeout")
    assert signals == [rows[0]]
