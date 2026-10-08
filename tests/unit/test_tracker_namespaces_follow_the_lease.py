"""The tracker namespace directory rows are
written by whoever holds the supervisor lease — at boot, or when the lease is
acquired on retry under a blue/green deploy — and a failed registration is
logged, never fatal."""

from __future__ import annotations

import pytest

import server.boot as b
import server.main as m
import server.tracker.app.registry as reg


@pytest.mark.asyncio
async def test_registration_logs_and_never_raises(monkeypatch, caplog) -> None:
    async def _boom(*, ch):
        raise RuntimeError("directory unreachable")

    monkeypatch.setattr(reg, "ensure_configured_tracker_namespaces", _boom)
    await b.register_tracker_namespaces(ch=object())  # no raise
    assert any("qa namespace registration failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_registration_reports_what_it_registered(monkeypatch, caplog) -> None:
    caplog.set_level("INFO")

    async def _ok(*, ch):
        return ["tracker-clickhouse", "tracker-example-private"]

    monkeypatch.setattr(reg, "ensure_configured_tracker_namespaces", _ok)
    await b.register_tracker_namespaces(ch=object())
    assert any("qa namespaces registered" in r.message for r in caplog.records)


def test_lifespan_registers_on_lease_retry_too() -> None:
    # the retry callback in the lifespan schedules the same registration the
    # boot path awaits: the source names both call sites (no closure to import)
    boot_src = open(b.__file__).read()
    assert "await boot.register_tracker_namespaces(ch)" in open(m.__file__).read()
    assert boot_src.count("_spawn_boot_task(register_tracker_namespaces(ch)") == 1
    assert "retry_until_held(" in boot_src and "_on_lease_acquired" in boot_src


@pytest.mark.asyncio
async def test_registration_queries_carry_their_own_tag_and_leave_the_callers(
    monkeypatch,
) -> None:
    import server.tracker.app.registry_sources as rs
    from server.core.ch import current_query_tag, set_query_tag

    seen: list[str] = []

    async def _refresh(*, force=False, ch=None):
        seen.append(current_query_tag())

    async def _ok(*, ch):
        seen.append(current_query_tag())
        return []

    monkeypatch.setattr(rs, "refresh_db_sources", _refresh)
    monkeypatch.setattr(reg, "ensure_configured_tracker_namespaces", _ok)
    set_query_tag("")
    await b.register_tracker_namespaces(ch=object())
    assert seen == ["boot.tracker_namespaces"] * 2
    assert current_query_tag() == ""  # the lifespan's later queries are not registration


@pytest.mark.asyncio
async def test_the_sources_refresher_tags_its_queries(monkeypatch) -> None:
    import asyncio

    import server.tracker.app.contract as ct
    import server.tracker.app.registry_sources as rs
    from server.core.ch import current_query_tag

    seen: list[str] = []
    done = asyncio.Event()

    async def _refresh(*, force=False, ch=None):
        seen.append(current_query_tag())
        done.set()

    async def _warm():
        return None

    monkeypatch.setattr(rs, "refresh_db_sources", _refresh)
    monkeypatch.setattr(ct, "warm_at_boot", _warm)
    task = b.start_tracker_sources_refresher(object())
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()
    assert seen == ["tracker.sources_refresh"]


@pytest.mark.asyncio
async def test_boot_tasks_are_held_until_done_and_cancelled_at_shutdown() -> None:
    import asyncio

    gate = asyncio.Event()

    async def _quick() -> None:
        return None

    async def _slow() -> None:
        await gate.wait()

    b._spawn_boot_task(_quick(), name="t-quick")
    b._spawn_boot_task(_slow(), name="t-slow")
    held = {t.get_name() for t in b._BOOT_TASKS}
    assert {"t-quick", "t-slow"} <= held  # a strong reference while running
    for _ in range(3):
        await asyncio.sleep(0)
    assert "t-quick" not in {t.get_name() for t in b._BOOT_TASKS}  # released once done
    (slow,) = [t for t in b._BOOT_TASKS if t.get_name() == "t-slow"]
    b.cancel_boot_tasks()
    with pytest.raises(asyncio.CancelledError):
        await slow
    assert slow not in b._BOOT_TASKS
