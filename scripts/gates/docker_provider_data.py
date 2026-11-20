"""Seed/check nonempty rebuild surfaces inside the disposable Docker gate."""

import asyncio
import json
import os
import re
import signal
import sys
import time
from multiprocessing import spawn
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from server.core import provider_process
from server.core.ch import get_client
from server.insights.app.provider_command import model_command
from server.settings import settings
from server.workers.child import child_argv

if TYPE_CHECKING:
    from server.core.repo_base import _CHClient

SUPERVISOR = [
    "-m",
    "uvicorn",
    "docker_provider_app:app",
    "--host",
    "0.0.0.0",
    "--port",
    "7777",
    "--workers",
    "2",
    "--timeout-graceful-shutdown",
    "10",
]


class _WorkersPending(AssertionError):
    """A supervised replacement has not spawned or enrolled yet."""


def _process(pid: int) -> dict:
    root = Path(f"/proc/{pid}")
    stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
    return {
        "pid": pid,
        "parent": int(stat[1]),
        "start": stat[19],
        "command": (root / "cmdline").read_bytes().decode().rstrip("\0").split("\0"),
        "namespace": os.stat(root / "ns/pid").st_ino,
        "executable": os.readlink(root / "exe"),
    }


def _supervisor() -> dict:
    parent, me = _process(1), _process(os.getpid())
    assert parent["command"][1:] == SUPERVISOR, "Unexpected fixture supervisor"
    assert parent["executable"] == me["executable"], "Fixture supervisor executable mismatch"
    assert parent["namespace"] == me["namespace"], "Fixture supervisor PID namespace mismatch"
    return parent


def _worker_process(raw: str, parent: dict) -> dict:
    proof = raw.split("|")
    assert len(proof) == 9 and provider_process.has_certificate(raw), "Worker certificate invalid"
    assert proof[:4] == provider_process.host_proof().split("|")[:4], "Worker proof scope mismatch"
    process = _process(int(proof[4]))
    pid = process["pid"]
    assert pid > 1, "Worker identity names the supervisor"
    assert process["parent"] == 1, f"Worker PID {pid} parent mismatch: {process['parent']} != 1"
    assert process["start"] == proof[5], f"Worker PID {pid} process-start proof mismatch"
    assert process["namespace"] == parent["namespace"], f"Worker PID {pid} namespace mismatch"
    assert process["executable"] == parent["executable"], f"Worker PID {pid} executable mismatch"
    _spawn_command(process["command"], pid)
    return process


def _spawn_command(command: list[str], pid: int) -> None:
    # The fixed supervisor and exec helper use the same executable and container
    # startup flags. Let stdlib reproduce those flags, including inherited -B.
    expected = spawn.get_command_line(tracker_fd=0, pipe_handle=0)
    assert len(command) == len(expected) and command[1:-2] == expected[1:-2], (
        f"Worker PID {pid} interpreter flags or -c mismatch"
    )
    assert command[-1] == "--multiprocessing-fork", f"Worker PID {pid} fork argument mismatch"
    assert re.fullmatch(
        r"from multiprocessing\.spawn import spawn_main; "
        r"spawn_main\(tracker_fd=[0-9]+, pipe_handle=[0-9]+\)",
        command[-2],
    ), f"Worker PID {pid} spawn command mismatch"


def _kernel_workers() -> dict[int, dict]:
    """The supervisor's actual spawn children, before consulting participant history."""
    found = {}
    children = Path("/proc/1/task/1/children").read_text().split()
    for value in children:
        pid = int(value)
        try:
            process = _process(pid)
        except FileNotFoundError:
            # An exited child can remain in the supervisor's census until reaped.
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                continue
            if state in {"Z", "X"}:
                continue
            raise
        command = process["command"]
        if not any(
            value.startswith("from multiprocessing.spawn import spawn_main;") for value in command
        ):
            continue  # The multiprocessing resource tracker is not an application worker.
        _spawn_command(command, pid)
        assert process["parent"] == 1, f"Kernel child PID {pid} parent changed during census"
        assert pid > 1 and pid not in found, "Invalid supervisor child PID census"
        found[pid] = process
    if len(found) < 2:
        raise _WorkersPending("Expected two live supervisor spawn children")
    assert len(found) == 2, f"Expected two live supervisor spawn children; found {len(found)}"
    return found


def _workers(rows: list[dict], parent: dict) -> list[dict]:
    here = provider_process.host_proof().split("|")
    children = _kernel_workers()
    workers = []
    for pid, process in children.items():
        matches = []
        for row in rows:
            proof = str(row["host_proof"]).split("|")
            if (
                len(proof) >= 6
                and row["state"] != "retired"
                and proof[:4] == here[:4]
                and proof[4] == str(pid)
                and proof[5] == process["start"]
            ):
                matches.append(row)
        if not matches:
            raise _WorkersPending(f"Worker PID {pid} exact registrations: 0; expected 1")
        assert len(matches) == 1, (
            f"Worker PID {pid} exact registrations: {len(matches)}; expected 1"
        )
        row = matches[0]
        assert _worker_process(row["host_proof"], parent) == process, (
            f"Worker PID {pid} changed during proof join"
        )
        workers.append({key: row[key] for key in ("participant_id", "host_proof", "version")})
    assert len({row["participant_id"] for row in workers}) == 2, "Worker participant IDs collide"
    assert _kernel_workers() == children, "Supervisor worker identities changed during census"
    return sorted(workers, key=lambda row: row["host_proof"])


def _proof(process: dict[str, Any]) -> str:
    parts = provider_process.host_proof().split("|")
    assert len(parts) == 8 and all(parts[:4]), "Incomplete fixture process scope"
    parts[4:6] = [str(process["pid"]), process["start"]]
    return provider_process.certified("|".join(parts))


async def _proof_rows(client: "_CHClient", proofs: list[str]) -> list[dict[str, Any]]:
    assert 1 <= len(proofs) <= 4 and len(set(proofs)) == len(proofs)
    rows = await client.query_all(
        "SELECT toString(participant_id) AS participant_id, state, version, host_proof "
        "FROM loom._provider_participants FINAL WHERE state != 'retired' "
        "AND host_proof IN {proofs:Array(String)} LIMIT 5",
        {"proofs": proofs},
    )
    assert len(rows) <= len(proofs), "Duplicate captured process registrations"
    return rows


async def _worker_rows(client) -> list[dict]:
    return await _proof_rows(client, [_proof(p) for p in _kernel_workers().values()])


def _background_children(
    roots: list[dict[str, Any]], parent: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    found = {}
    for root in roots:
        web = _worker_process(root["host_proof"], parent)
        children = []
        for value in Path(f"/proc/{web['pid']}/task/{web['pid']}/children").read_text().split():
            process = _process(int(value))
            command = process["command"]
            if "server.worker_main" not in command:
                continue
            assert process["parent"] == web["pid"], "Background child parent changed"
            assert process["namespace"] == web["namespace"], "Background child namespace changed"
            assert process["executable"] == web["executable"], "Background child executable changed"
            assert len(command) == 7, "Background child command mismatch"
            fds = [int(command[4]), int(command[6])]
            assert min(fds) >= 0 and command == child_argv(*fds), (
                "Background child command mismatch"
            )
            children.append(process)
        assert len(children) <= 1, "More than one background child per web worker"
        assert _worker_process(root["host_proof"], parent) == web, (
            "Web worker changed during child census"
        )
        found[root["participant_id"]] = children
    return found


async def workers(client) -> list[dict]:
    parent = _supervisor()
    result = _workers(await _worker_rows(client), parent)
    assert len(result) == 2, "Worker registration census is incomplete"
    children = _background_children(result, parent)
    proofs = [_proof(p) for values in children.values() for p in values]
    rows = await _proof_rows(client, proofs) if proofs else []
    for root in result:
        root["children"] = []
        for process in children[root["participant_id"]]:
            matches = [row for row in rows if row["host_proof"] == _proof(process)]
            if not matches:
                raise _WorkersPending("Background child exact registration is pending")
            assert len(matches) == 1, "Background child registration is ambiguous"
            row = matches[0]
            assert provider_process.has_certificate(row["host_proof"]), (
                "Background child certificate invalid"
            )
            root["children"].append(
                {key: row[key] for key in ("participant_id", "host_proof", "version")}
            )
    assert _background_children(result, parent) == children, (
        "Background child identities changed during census"
    )
    assert len({row["participant_id"] for row in _captured(result)}) == 2 + len(proofs)
    assert _supervisor() == parent, "Fixture supervisor changed during registration census"
    return result


def _captured(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert 1 <= len(roots) <= 2
    assert all(type(root["children"]) is list and len(root["children"]) <= 1 for root in roots)
    return [row for root in roots for row in (root, *root["children"])]


def _kill_worker(worker: dict, parent: dict) -> None:
    proof = worker["host_proof"].split("|")
    pid = int(proof[4])
    assert pid > 1, "Refusing to signal the supervisor"
    captured = _worker_process(worker["host_proof"], parent)
    fd = os.pidfd_open(pid)
    try:
        # A reused PID or changed supervisor refuses before the process-bound signal.
        assert _process(pid) == captured, f"Worker PID {pid} changed after pidfd open"
        assert _supervisor() == parent, "Fixture supervisor changed before worker signal"
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)


async def _retirement_rows(client, captured: list[dict]) -> list[dict]:
    assert 1 <= len(captured) <= 4
    rows = await client.query_all(
        "SELECT participant_id, state, version, host_proof "
        "FROM loom._provider_participants FINAL WHERE participant_id IN {ids:Array(UUID)} LIMIT 5",
        {"ids": [row["participant_id"] for row in captured]},
    )
    return [{**row, "participant_id": str(row["participant_id"])} for row in rows]


async def _native_retirement_observation(client, captured: list[dict]) -> dict:
    try:
        rows = await client.query_all(
            "SELECT participant_id "
            "FROM loom._provider_participants FINAL WHERE participant_id IN {ids:Array(UUID)}",
            {"ids": [row["participant_id"] for row in captured]},
        )
        return {
            "reference_count": len(rows),
            "reference_identity": {str(row["participant_id"]) for row in rows}
            == {row["participant_id"] for row in captured},
        }
    except Exception:  # noqa: BLE001 — diagnostic failure cannot replace the unproved retirement
        return {"reference_read": "query_error"}


async def retired(client, captured: list[dict], *, observation: dict | None = None) -> bool:
    from server.insights.store.provider_participants import running_queries

    def result(reason: str) -> bool:
        if observation is not None:
            observation["retirement"] = reason
        return reason == "proved"

    rows = await _retirement_rows(client, captured)
    if len(rows) != len(captured):
        return result("row_count")
    expected = {row["participant_id"]: row for row in captured}
    for row in rows:
        old = expected[row["participant_id"]]
        if row["state"] != "retired":
            return result("state")
        if int(row["version"]) <= int(old["version"]):
            return result("version")
        if row["host_proof"] != old["host_proof"]:
            return result("host_proof")
        if not provider_process.has_certificate(row["host_proof"]):
            return result("certificate")
        running = await running_queries(row["participant_id"], ch=client)
        if running != 0:
            return result("queries_unknown" if running is None else "queries_running")
    return result("proved")


async def _subtree_diagnostic(
    client: "_CHClient", roots: list[dict[str, Any]], *, proved: bool
) -> None:
    """Print closed role counts, preserving the original retirement failure."""
    try:
        roles = {"web": roots, "background": [c for r in roots for c in r["children"]]}
        summary = {}
        for role, captured in roles.items():
            counts = {
                "captured": len(captured),
                "retirement_proved": 0,
                "death_proved": 0,
                "query_zero_proved": 0,
                "unproved": 0,
                "kernel_zombie_single_thread": 0,
                "kernel_zombie_multi_thread": 0,
                "kernel_present_non_zombie": 0,
                "kernel_missing": 0,
                "kernel_uninspectable": 0,
            }
            for row in captured:
                kernel = "kernel_uninspectable"
                proof = row["host_proof"].split("|")
                here = provider_process.host_proof().split("|")
                if len(proof) in {8, 9} and proof[:4] == here[:4] and proof[4].isdecimal():
                    try:
                        raw = await asyncio.to_thread(Path(f"/proc/{int(proof[4])}/stat").read_text)
                        stat = raw.rsplit(")", 1)[1].split()
                        if stat[19] == proof[5]:
                            threads = int(stat[17])
                            if stat[0] == "Z" and threads >= 1:
                                kernel = (
                                    "kernel_zombie_single_thread"
                                    if threads == 1
                                    else "kernel_zombie_multi_thread"
                                )
                            elif threads >= 1:
                                kernel = "kernel_present_non_zombie"
                    except FileNotFoundError:
                        kernel = "kernel_missing"
                    except (OSError, IndexError, ValueError):
                        pass  # An unreadable identity does not establish a kernel state.
                counts[kernel] += 1
                counts["death_proved"] += int(provider_process.proves_stopped(row["host_proof"]))
                accepted = proved or (bool(roles["background"]) and await retired(client, [row]))
                counts["retirement_proved"] += int(accepted)
                counts["query_zero_proved"] += int(accepted)
                counts["unproved"] += int(not accepted)
            summary[role] = counts
        print("Provider subtree retirement diagnostic: " + json.dumps(summary, sort_keys=True))
    except BaseException:  # noqa: BLE001 — diagnostics must preserve the original retirement failure
        print("Provider subtree retirement diagnostic unavailable")


async def crash_worker(client) -> None:
    parent = _supervisor()
    before = await workers(client)
    victim = before[0]
    for worker in _captured(before):
        rows = await _retirement_rows(client, [worker])
        identity_matches = len(rows) == 1 and rows[0]["participant_id"] == worker["participant_id"]
        if not identity_matches:
            readback = {
                "reader_count": len(rows),
                "reader_identity": identity_matches,
                **await _native_retirement_observation(client, [worker]),
            }
            raise AssertionError(
                "Captured worker readback failed: " + json.dumps(readback, sort_keys=True)
            )
    _kill_worker(victim, parent)
    deadline = time.monotonic() + 240
    observation: dict = {"death": "not_checked", "retirement": "not_checked"}
    while time.monotonic() < deadline:
        assert _supervisor() == parent
        stopped = provider_process.proves_stopped(victim["host_proof"])
        observation = {"death": "proved" if stopped else "unproved", "retirement": "not_checked"}
        if stopped and await retired(client, _captured([victim]), observation=observation):
            try:
                after = _workers(await _worker_rows(client), parent)
            except _WorkersPending:
                observation["replacement"] = "pending"
                await asyncio.sleep(1)
                continue
            old = {row["participant_id"] for row in before}
            new = {row["participant_id"] for row in after}
            observation.update(
                replacement_count=len(new),
                survivors=len(old & new),
                victim_absent=victim["participant_id"] not in new,
            )
            if len(new) == 2 and len(old & new) == 1 and victim["participant_id"] not in new:
                await _subtree_diagnostic(client, [victim], proved=True)
                return
        await asyncio.sleep(1)
    if observation["retirement"] == "row_count":
        observation.update(await _native_retirement_observation(client, [victim]))
    await _subtree_diagnostic(client, [victim], proved=False)
    raise AssertionError(
        "Crashed worker retirement or supervised replacement was not proved: "
        + json.dumps(observation, sort_keys=True)
    )


async def seed() -> None:
    from server import boot
    from server.core.background import register_buffer_drainer

    register_buffer_drainer("boot-writes", boot.drain_writes)
    # The served width: a coded vector column takes vectors of that length only.
    width = settings.embedding_dim
    vector = [1.0] + [0.0] * (width - 1)
    async with model_command():
        client = get_client()
        await client.execute(
            "INSERT INTO loom.memory_embeddings "
            "(org, namespace, agent, user_id, session_id, memory_key, memory_type, scope, "
            "content_excerpt, embedding_model, embedding_dim, embedding, memory_key_embedding, "
            "memory_key_embedding_dim, verified, tags) SETTINGS async_insert = 0 VALUES "
            "('dev', 'local', 'docker-smoke', '-', '-', 'docker/vector-smoke', 'semantic', "
            "'private', 'Provider rebuild preserves metadata', {model:String}, {dim:UInt16}, "
            "{vec:Array(BFloat16)}, {vec:Array(BFloat16)}, {dim:UInt16}, 1, ['provider-smoke'])",
            {"model": settings.embedding_model, "dim": width, "vec": vector},
        )
        await client.execute(
            "INSERT INTO loom.code_chunks "
            "(org, namespace, embedding_model, path, chunk_id, content, lexemes, embedding, "
            "embedding_dim, start_line, end_line) SETTINGS async_insert = 0 VALUES "
            "('dev', 'code-provider-smoke', {model:String}, 'example.py', "
            "'ace68a73-808a-4b37-9ae0-2ab1a03b1c53', 'def greet(): return 42', "
            "'greet return', {vec:Array(BFloat16)}, {dim:UInt16}, 1, 1)",
            {"model": settings.embedding_model, "dim": width, "vec": vector},
        )


async def verify(model: str, dimensions: int) -> None:
    client = get_client()
    memory = await client.query_one(
        "SELECT embedding_model, embedding_dim, memory_key_embedding_dim, "
        "length(embedding) AS width, verified, tags, content_excerpt "
        "FROM loom.memory_embeddings FINAL WHERE org='dev' AND namespace='local' "
        "AND memory_key='docker/vector-smoke' AND tombstoned_at IS NULL",
    )
    assert memory is not None
    assert memory["embedding_model"] == model and memory["embedding_dim"] == dimensions, memory
    assert memory["memory_key_embedding_dim"] == dimensions and memory["width"] == dimensions
    assert memory["verified"] == 1 and memory["tags"] == ["provider-smoke"]
    assert memory["content_excerpt"] == "Provider rebuild preserves metadata"
    code = await client.query_one(
        "SELECT embedding_model, embedding_dim, length(embedding) AS width, content, start_line "
        "FROM loom.code_chunks FINAL WHERE org='dev' AND namespace='code-provider-smoke' "
        "AND path='example.py' AND tombstoned_at IS NULL",
    )
    assert code is not None
    assert code["embedding_model"] == model and code["embedding_dim"] == dimensions, code
    assert code["width"] == dimensions and code["content"] == "def greet(): return 42"
    assert code["start_line"] == 1
    copies = await client.query_one(
        "SELECT count() AS n FROM system.tables WHERE database = currentDatabase() "
        "AND startsWith(name, '_provider_rebuild_')"
    )
    assert copies is not None and copies["n"] == 0, copies
    print(f"PASS provider data: {model}/{dimensions}, memory + code metadata, no retained copies")


async def diagnostic(client, transition: str) -> None:
    """Read capped aggregate evidence; never acknowledge or retire a participant."""
    from server.insights.store.provider_participants import PARTICIPANTS_SQL

    scope = provider_process.host_proof().split("|")[:4]
    if len(scope) != 4 or not all(scope):
        raise ValueError("Unavailable provider diagnostic scope")
    sql = (
        "WITH retained AS (" + PARTICIPANTS_SQL + "), "
        "lease AS (SELECT holder, renewed_at + toIntervalSecond(ttl_s) < now64(3) AS expired "
        "FROM loom._migration_lock WHERE name = {name:String} LIMIT 2) "
        "SELECT count() AS retained, "
        "countIf(p.state = 'running') AS running, "
        "countIf(p.state = 'drained' AND p.transition_id = {transition:UUID}) AS matching_drained, "
        "countIf(p.state = 'drained' AND p.transition_id != {transition:UUID}) AS other_drained, "
        "countIf(p.state = 'retired') AS retained_retired, "
        "countIf(p.expired) AS expired, "
        "countIf(arraySlice(splitByChar('|', p.host_proof), 1, 4) = {scope:Array(String)}) "
        "AS same_scope, "
        "(SELECT count() FROM lease) AS lease_rows, "
        "(SELECT countIf(expired) FROM lease) AS expired_leases, "
        "countIf(l.holder != '') AS retained_holders, "
        "countIf(l.holder != '' AND p.state = 'drained' "
        "AND p.transition_id = {transition:UUID}) AS matching_drained_holders "
        "FROM retained AS p ANY LEFT JOIN lease AS l "
        "ON toString(p.participant_id) = arrayElement(splitByChar('/', l.holder), 1) "
        "SETTINGS max_execution_time = 3, max_rows_to_read = 10000, "
        "max_bytes_to_read = 10485760, read_overflow_mode = 'throw', "
        "max_result_rows = 1, max_result_bytes = 4096, result_overflow_mode = 'throw', "
        "max_memory_usage = 134217728, max_threads = 1, select_sequential_consistency = 1"
    )
    row = await asyncio.wait_for(
        client.query_one(
            sql,
            {
                "name": "provider_configuration",
                "transition": str(UUID(transition)),
                "scope": scope,
            },
        ),
        timeout=5,
    )
    keys = {
        "retained",
        "running",
        "matching_drained",
        "other_drained",
        "retained_retired",
        "expired",
        "same_scope",
        "lease_rows",
        "expired_leases",
        "retained_holders",
        "matching_drained_holders",
    }
    if (
        row is None
        or row.keys() != keys
        or any(type(value) is not int or not 0 <= value <= 10000 for value in row.values())
    ):
        raise ValueError("Invalid provider diagnostic aggregate")
    print("Provider reset diagnostic: " + json.dumps(row, sort_keys=True))


async def main() -> None:
    client = get_client()
    await client.connect()
    try:
        if sys.argv[1] == "diagnostic":
            await diagnostic(client, sys.argv[2])
        elif sys.argv[1] == "seed":
            await seed()
        elif sys.argv[1] == "workers":
            print(json.dumps(await workers(client)))
        elif sys.argv[1] == "crash-worker":
            await crash_worker(client)
        elif sys.argv[1] == "retired":
            captured = json.loads(sys.argv[2])
            accepted = await retired(client, _captured(captured))
            await _subtree_diagnostic(client, captured, proved=accepted)
            assert accepted, "Recreated workers did not retire"
        else:
            await verify(sys.argv[1], int(sys.argv[2]))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
