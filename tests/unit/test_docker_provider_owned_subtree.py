"""The native fixture captures and checks its exact controlled process subtree."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.gates.docker_provider_data as data
from server.insights.store import provider_participants
from server.workers.child import child_argv
from tests.unit.test_docker_provider_smoke import _process_fixture


def _fixture(monkeypatch, *, background=True, fault=None):
    parent, processes, roots = _process_fixture(monkeypatch)
    children = {1: "8 9", 8: "18" if background else "", 9: ""}
    descendant = {
        **processes[8],
        "pid": 18,
        "parent": 8,
        "start": "180",
        "command": child_argv(4, 8),
    }
    processes[18] = descendant
    child = {
        "participant_id": "background-participant",
        "version": 5,
        "state": "running",
        "host_proof": "h|b|c|1|18|180|||cell-retirement-v1",
    }
    registrations = [*roots, *([child] if background else [])]
    if fault == "root-reused":
        processes[8] = {**processes[8], "start": "81"}
    elif fault == "root-parent":
        processes[8] = {**processes[8], "parent": 2}
    elif fault == "child-reused":
        processes[18] = {**descendant, "start": "181"}
    elif fault == "child-parent":
        processes[18] = {**descendant, "parent": 9}
    elif fault == "child-namespace":
        processes[18] = {**descendant, "namespace": 2}
    elif fault == "child-command":
        processes[18] = {**descendant, "command": [*descendant["command"], "--unknown"]}
    elif fault == "two-background":
        processes[19] = {**descendant, "pid": 19, "start": "190"}
        children[8] = "18 19"
    elif fault == "missing-child":
        registrations = roots
    elif fault == "duplicate-child":
        registrations.append({**child, "participant_id": "ambiguous-background"})

    def read(path):
        parts = str(path).split("/")
        if len(parts) == 4 and parts[1] == "proc" and parts[3] == "stat":
            process = processes[int(parts[2])]
            fields = ["0"] * 20
            fields[0], fields[17], fields[19] = "S", "1", process["start"]
            return f"{process['pid']} (fixture worker) " + " ".join(fields)
        assert parts[1] == "proc" and parts[3] == "task" and parts[2] == parts[4]
        assert parts[5] == "children"
        return children[int(parts[2])]

    monkeypatch.setattr(data, "Path", lambda path: SimpleNamespace(read_text=lambda: read(path)))
    monkeypatch.setattr(data, "_supervisor", lambda: parent)

    async def query(sql, parameters=None):
        selected = registrations
        if parameters and "proofs" in parameters:
            selected = [r for r in selected if r["host_proof"] in parameters["proofs"]]
        if (
            fault == "child-changed-after-query"
            and parameters
            and child["host_proof"] in parameters.get("proofs", [])
        ):
            processes[18] = {**descendant, "start": "181"}
        return selected

    client = SimpleNamespace(query_all=AsyncMock(side_effect=query))
    return roots, child, client


@pytest.mark.asyncio
async def test_actual_workers_capture_the_exact_background_child(monkeypatch):
    roots, child, client = _fixture(monkeypatch)
    captured = await data.workers(client)
    assert [r["participant_id"] for r in captured] == [r["participant_id"] for r in roots]
    assert [c["participant_id"] for r in captured for c in r.get("children", [])] == [
        child["participant_id"]
    ]
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_actual_workers_keep_the_same_two_web_roots_without_a_background_child(monkeypatch):
    roots, _, client = _fixture(monkeypatch, background=False)
    captured = await data.workers(client)
    assert [r["participant_id"] for r in captured] == [r["participant_id"] for r in roots]
    assert not any(r.get("children", []) for r in captured)
    assert len(captured) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["root-reused", "root-parent"])
async def test_actual_workers_refuse_changed_web_identity(monkeypatch, fault):
    _, _, client = _fixture(monkeypatch, fault=fault)
    with pytest.raises(AssertionError):
        await data.workers(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "child-reused",
        "child-parent",
        "child-namespace",
        "child-command",
        "two-background",
        "missing-child",
        "duplicate-child",
        "child-changed-after-query",
    ],
)
async def test_actual_workers_refuse_uncertain_background_identity(monkeypatch, fault):
    _, _, client = _fixture(monkeypatch, fault=fault)
    with pytest.raises(AssertionError):
        await data.workers(client)


@pytest.mark.asyncio
async def test_actual_recreated_check_refuses_an_unretired_background_child(monkeypatch):
    roots, child, _ = _fixture(monkeypatch)
    captured = [{**r, "children": [child] if i == 0 else []} for i, r in enumerate(roots)]
    observed = [{**r, "state": "retired", "version": 6} for r in roots] + [
        {**child, "state": "drained", "version": 6}
    ]

    async def query(_sql, parameters):
        return [r for r in observed if r["participant_id"] in parameters["ids"]]

    client = SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(), query_all=AsyncMock(side_effect=query)
    )
    monkeypatch.setattr(data, "get_client", lambda: client)
    monkeypatch.setattr(data.sys, "argv", ["fixture", "retired", json.dumps(captured)])
    monkeypatch.setattr(provider_participants, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _: False)
    with pytest.raises(AssertionError, match="Recreated workers did not retire"):
        await data.main()
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_recreated_check_reports_only_closed_role_proof_counts(monkeypatch, capsys):
    roots, child, _ = _fixture(monkeypatch)
    captured = [{**r, "children": [child] if i == 0 else []} for i, r in enumerate(roots)]
    observed = [{**r, "state": "retired", "version": 6} for r in [*roots, child]]

    async def query(_sql, parameters):
        return [r for r in observed if r["participant_id"] in parameters["ids"]]

    client = SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(), query_all=AsyncMock(side_effect=query)
    )
    monkeypatch.setattr(data, "get_client", lambda: client)
    monkeypatch.setattr(data.sys, "argv", ["fixture", "retired", json.dumps(captured)])
    monkeypatch.setattr(provider_participants, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _: False)
    await data.main()
    prefix = "Provider subtree retirement diagnostic: "
    output = capsys.readouterr().out
    assert output.startswith(prefix)
    summary = json.loads(output.removeprefix(prefix))
    assert summary == {
        "web": {
            "captured": 2,
            "retirement_proved": 2,
            "death_proved": 0,
            "query_zero_proved": 2,
            "unproved": 0,
            "kernel_zombie_single_thread": 0,
            "kernel_zombie_multi_thread": 0,
            "kernel_present_non_zombie": 2,
            "kernel_missing": 0,
            "kernel_uninspectable": 0,
        },
        "background": {
            "captured": 1,
            "retirement_proved": 1,
            "death_proved": 0,
            "query_zero_proved": 1,
            "unproved": 0,
            "kernel_zombie_single_thread": 0,
            "kernel_zombie_multi_thread": 0,
            "kernel_present_non_zombie": 1,
            "kernel_missing": 0,
            "kernel_uninspectable": 0,
        },
    }
    assert all(row["host_proof"] not in output for row in [*roots, child])
    assert child["participant_id"] not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "threads", "start", "error", "expected"),
    [
        ("Z", "1", "180", None, "kernel_zombie_single_thread"),
        ("Z", "2", "180", None, "kernel_zombie_multi_thread"),
        ("S", "1", "180", None, "kernel_present_non_zombie"),
        ("S", "1", "180", FileNotFoundError, "kernel_missing"),
        ("S", "1", "180", PermissionError, "kernel_uninspectable"),
        ("Z", "1", "181", None, "kernel_uninspectable"),
    ],
)
async def test_subtree_kernel_counts_observe_only_the_captured_identity(
    monkeypatch, capsys, state, threads, start, error, expected
):
    roots, child, _ = _fixture(monkeypatch)
    captured = [{**roots[0], "children": [child]}]
    fixture_path = data.Path

    def read(path):
        if path != "/proc/18/stat":
            return fixture_path(path).read_text()
        if error is not None:
            raise error("kernel record unavailable")
        fields = ["0"] * 20
        fields[0], fields[17], fields[19] = state, threads, start
        return "18 (fixture child) " + " ".join(fields)

    monkeypatch.setattr(data, "Path", lambda path: SimpleNamespace(read_text=lambda: read(path)))
    monkeypatch.setattr(data.provider_process, "proves_stopped", lambda _: False)
    client = SimpleNamespace(query_all=AsyncMock(side_effect=AssertionError("no query expected")))
    await data._subtree_diagnostic(client, captured, proved=True)
    prefix = "Provider subtree retirement diagnostic: "
    output = capsys.readouterr().out
    assert output.startswith(prefix)
    summary = json.loads(output.removeprefix(prefix))
    common = {
        "captured": 1,
        "retirement_proved": 1,
        "death_proved": 0,
        "query_zero_proved": 1,
        "unproved": 0,
    }
    kernel_keys = {
        "kernel_zombie_single_thread",
        "kernel_zombie_multi_thread",
        "kernel_present_non_zombie",
        "kernel_missing",
        "kernel_uninspectable",
    }
    assert summary == {
        "web": {**common, **{key: int(key == "kernel_present_non_zombie") for key in kernel_keys}},
        "background": {**common, **{key: int(key == expected) for key in kernel_keys}},
    }
    assert all(row["host_proof"] not in output for row in [*roots, child])
    assert child["participant_id"] not in output
    client.query_all.assert_not_awaited()
