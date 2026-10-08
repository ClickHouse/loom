"""bench/bench_namespaces.py — bench namespaces register as bench with capture on,
and are erased when the run ends, including after a failure or an interrupt."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from bench.bench_namespaces import (
    BENCH_OWNER_EMAIL,
    erase_on_exit,
    erase_on_exit_sync,
    erasure_bodies,
    gc_command,
    register_bench_namespace,
    register_body,
    remember_bench_namespace,
)


def test_register_body_turns_capture_on_and_keeps_caller_fields() -> None:
    body = register_body("dev", "lme-abc", description="LongMemEval item q1")
    assert body["org"] == "dev" and body["namespace"] == "lme-abc"
    assert body["capture_generation_io"] is True
    assert body["owner_email"] == BENCH_OWNER_EMAIL and body["tags"] == ["bench"]
    # No retention by default: a per-namespace retention is a table-level TTL
    # clause and registering one re-issues MODIFY TTL on eight tables; bench
    # namespaces retire through namespaces-gc by idleness.
    assert "retention_days" not in body
    assert register_body("dev", "lme-abc", description="x", retention_days=7)["retention_days"] == 7
    off = register_body("dev", "lme-abc", description="x", capture=False, retention_days=0)
    assert "capture_generation_io" not in off  # the server default applies unchanged
    assert "retention_days" not in off
    extra = register_body(
        "dev", "mab-1", description="x", extra={"default_conflict_policy": "newest_wins"}
    )
    assert extra["default_conflict_policy"] == "newest_wins"
    assert extra["capture_generation_io"] is True


@pytest.mark.asyncio
async def test_register_posts_once_and_never_raises() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, body))
        return {"ok": True}

    assert await register_bench_namespace(_post, org="dev", namespace="lme-1", description="d")
    assert calls == [("/v1/namespace.register", register_body("dev", "lme-1", description="d"))]

    async def _boom(path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("503")

    # a failed registration is logged, not raised: the bench keeps running
    assert not await register_bench_namespace(_boom, org="dev", namespace="lme-2", description="d")


def test_harnesses_expose_the_flag() -> None:
    import pathlib

    for f in ("bench/longmemeval.py", "bench/locomo_qa.py"):
        src = pathlib.Path(f).read_text()
        assert "--no-capture-consent" in src and "register_bench_namespace" in src


def test_register_body_registers_the_namespace_as_bench_traffic() -> None:
    """Without a bench kind or class, a registered row is production traffic that
    the idle-namespace gc never retires; a kept namespace stays bench traffic but
    is `memory` so the gc leaves it for the run that reuses it."""
    body = register_body("dev", "lme-abc", description="x")
    assert body["kind"] == "bench" and body["traffic_class"] == "bench"
    kept = register_body("dev", "lme-abc", description="x", keep=True)
    assert kept["kind"] == "memory" and kept["traffic_class"] == "bench"


def test_erasure_bodies_batch_the_names_into_list_scoped_purges() -> None:
    bodies = erasure_bodies("dev", ["a", "b", "c"], batch=2)
    assert [json.loads(b["scope_value"]) for b in bodies] == [
        {"namespaces": ["a", "b"], "purge": True},
        {"namespaces": ["c"], "purge": True},
    ]
    assert {b["scope_type"] for b in bodies} == {"namespace_list"}
    assert {b["external"] for b in bodies} == {False}
    assert erasure_bodies("dev", []) == []


def test_gc_command_names_the_real_cli_and_includes_registered_rows() -> None:
    cmd = gc_command("dev", ["lme-1", "lme-2"])
    assert cmd.startswith("loom namespaces-gc --org dev --include-registered --erase")
    assert "--namespace lme-1 --namespace lme-2" in cmd


@pytest.mark.asyncio
async def test_erase_on_exit_erases_after_an_error() -> None:
    calls: list[dict[str, Any]] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return {}

    remember_bench_namespace("dev", "lme-before")  # minted outside the block: kept
    with pytest.raises(RuntimeError):
        async with erase_on_exit(_post, org="dev"):
            remember_bench_namespace("dev", "lme-failed-run")
            remember_bench_namespace("other", "lme-other-org")
            raise RuntimeError("the run died")
    assert [json.loads(c["scope_value"])["namespaces"] for c in calls] == [["lme-failed-run"]]


@pytest.mark.asyncio
async def test_erase_on_exit_runs_when_the_task_is_cancelled() -> None:
    calls: list[dict[str, Any]] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return {}

    async def _run() -> None:
        async with erase_on_exit(_post, org="dev"):
            remember_bench_namespace("dev", "lme-interrupted")
            await asyncio.sleep(3600)

    task = asyncio.create_task(_run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads(calls[0]["scope_value"])["namespaces"] == ["lme-interrupted"]


@pytest.mark.asyncio
async def test_erase_on_exit_keep_and_refusal_never_raise() -> None:
    async def _refuse(path: str, body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("403 org-admin required")

    async with erase_on_exit(_refuse, org="dev"):
        remember_bench_namespace("dev", "lme-refused")  # logged with the gc command
    calls: list[str] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(path)
        return {}

    async with erase_on_exit(_post, org="dev", keep=True):
        remember_bench_namespace("dev", "lme-kept")
    assert calls == []


def test_erase_on_exit_sync_erases_after_an_error() -> None:
    calls: list[dict[str, Any]] = []

    def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return {}

    with pytest.raises(KeyboardInterrupt), erase_on_exit_sync(_post, org="dev"):
        remember_bench_namespace("dev", "qabench-sync")
        raise KeyboardInterrupt
    assert json.loads(calls[0]["scope_value"])["namespaces"] == ["qabench-sync"]


def test_register_sync_posts_the_bench_body_and_never_raises() -> None:
    from bench.bench_namespaces import register_bench_namespace_sync

    calls: list[tuple[str, dict[str, Any]]] = []
    assert register_bench_namespace_sync(
        lambda p, b: calls.append((p, b)), org="dev", namespace="qabench-1", description="d"
    )
    assert calls == [("/v1/namespace.register", register_body("dev", "qabench-1", description="d"))]

    def _boom(path: str, body: dict[str, Any]) -> None:
        raise RuntimeError("503")

    assert not register_bench_namespace_sync(_boom, org="dev", namespace="q", description="d")
