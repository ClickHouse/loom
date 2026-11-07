"""A run's rows stop competing at full standing when the run completes
(they are demoted, not reset)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from server.memory.app import run_retention as rr

T0 = datetime(2026, 9, 12, 18, 0, 0)


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "memory_id": "m1",
        "memory_key": "k1",
        "memory_type": "semantic",
        "agent": "a",
        "user_id": "-",
        "session_id": "s1",
        "pinned": 0,
        "verified_at": None,
        "status": "active",
        "confidence": 0.8,
        "expires_at": None,
        "locked": 0,
    }
    row.update(over)
    return row


def test_select_retains_pinned_verified_promoted_non_facts_and_non_active() -> None:
    rows = [
        _row(memory_id="m1", memory_key="fact"),
        _row(memory_id="m2", memory_key="pinned", pinned=1),
        _row(memory_id="m3", memory_key="verified", verified_at=T0),
        _row(memory_id="m4", memory_key="shared"),
        _row(memory_id="m5", memory_key="obs", memory_type="episodic"),
        _row(memory_id="m6", memory_key="lesson", memory_type="reflection"),
        _row(memory_id="m7", memory_key="old", status="superseded"),
        _row(memory_id="m8", memory_key="contested", status="conflicted"),
        _row(memory_id="", memory_key="noid"),
    ]
    act, retained = rr.select(rows, promoted_keys={"shared"})
    assert [r["memory_id"] for r in act] == ["m1", "m8"]
    assert retained == 7


def test_demoted_confidence_scales_with_floor() -> None:
    assert rr.demoted_confidence(0.8, factor=0.5, floor=0.05) == 0.4
    assert rr.demoted_confidence(0.05, factor=0.5, floor=0.05) == 0.05
    assert rr.demoted_confidence(1.7, factor=0.5, floor=0.05) == 0.85


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]], shared: list[str]) -> None:
        self.rows, self.shared = rows, shared
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        if "loom.shared" in sql:
            return [{"memory_key": k} for k in self.shared]
        return self.rows


class _Existing:
    """The live row `write_versions_by_ids` hands the update: identity and confidence."""

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.org, self.namespace, self.memory_id = "o", "n", "m1"
        self.agent, self.user_id, self.session_id = "a", "-", "-"
        self.memory_key = "fact"


def _overrides(update: dict[str, Any]) -> dict[str, Any]:
    """The update's columns but its version, which the webhook event repeats."""
    assert isinstance(update.pop("version"), int)
    return update


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_keep_is_a_no_op_and_touches_nothing(monkeypatch) -> None:
    ch = _FakeCH([_row()], [])
    counts = _run(
        rr.apply(
            org="o",
            namespace="n",
            run_id="r1",
            mode="keep",
            ch=ch,
            demote_factor=0.5,
            demote_floor=0.05,
            expire_days=7,
        )
    )
    assert counts.as_dict() == {"retained": 0, "demoted": 0, "expiring": 0}
    assert ch.calls == []


def test_demote_writes_scaled_confidence_and_mirrors_it(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import apply as memory_apply

    written: list[tuple[list[str], dict[str, Any]]] = []
    mirrored: list[tuple[str, dict[str, Any]]] = []

    async def fake_write(
        org: str, ids: list[str], update: Any, *, namespace: str, ch: Any = None
    ) -> int:
        written.append((ids, _overrides(update(_Existing(0.8)))))
        return len(ids)

    async def fake_mirror(row: Any, **overrides: Any) -> None:
        # The mirror is a versioned re-emit with the changed columns.
        mirrored.append({**overrides, "key": row.memory_key})

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", fake_write)
    monkeypatch.setattr(memory_apply, "_mirror", fake_mirror)
    ch = _FakeCH(
        [
            _row(memory_id="m1", memory_key="fact"),
            _row(memory_id="m2", memory_key="locked", locked=1),
            _row(memory_id="m3", memory_key="shared"),
        ],
        shared=["shared"],
    )
    counts = _run(
        rr.apply(
            org="o",
            namespace="n",
            run_id="r1",
            mode="demote",
            ch=ch,
            now=T0,
            demote_factor=0.5,
            demote_floor=0.05,
            expire_days=7,
        )
    )
    assert counts.as_dict() == {"retained": 2, "demoted": 1, "expiring": 0}
    assert written == [(["m1"], {"confidence": 0.4})]
    assert mirrored == [{"confidence": 0.4, "key": "fact"}]
    # The run's rows are selected by run_id; the shared check saw only unlocked keys.
    sql, params = ch.calls[0]
    assert "run_id = {rid:String}" in sql and params["rid"] == "r1"
    assert ch.calls[1][1]["keys"] == ["fact", "shared"]


def test_expire_sets_expiry_and_demote_post_action(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import apply as memory_apply

    written: list[dict[str, Any]] = []
    mirrored: list[tuple[str, dict[str, Any]]] = []

    async def fake_write(
        org: str, ids: list[str], update: Any, *, namespace: str, ch: Any = None
    ) -> int:
        written.append(_overrides(update(_Existing(0.8))))
        return len(ids)

    async def fake_mirror(row: Any, **overrides: Any) -> None:
        mirrored.append(overrides)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", fake_write)
    monkeypatch.setattr(memory_apply, "_mirror", fake_mirror)
    ch = _FakeCH([_row(memory_id="m1", memory_key="fact")], shared=[])
    counts = _run(
        rr.apply(
            org="o",
            namespace="n",
            run_id="r1",
            mode="expire",
            ch=ch,
            now=T0,
            demote_factor=0.5,
            demote_floor=0.05,
            expire_days=7,
        )
    )
    assert counts.as_dict() == {"retained": 0, "demoted": 0, "expiring": 1}
    assert written == [
        {
            "expires_at": T0 + timedelta(days=7),
            "on_expire": "demote",
            "expire_reason": "run r1 completed (retain=expire)",
        }
    ]
    assert mirrored == [{"expires_at": T0 + timedelta(days=7)}]


def test_apply_is_fail_open() -> None:
    class _Boom:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            raise RuntimeError("keeper session expired")

    counts = _run(
        rr.apply(
            org="o",
            namespace="n",
            run_id="r1",
            mode="demote",
            ch=_Boom(),
            demote_factor=0.5,
            demote_floor=0.05,
            expire_days=7,
        )
    )
    assert counts.as_dict() == {"retained": 0, "demoted": 0, "expiring": 0}


def test_run_complete_threads_retain_and_the_server_default(monkeypatch) -> None:
    """The handler passes the call's `retain` (else the settings default) and
    reports the counts; the ledger row itself is written either way."""
    from unittest.mock import patch

    from server.memory import store as memory_repo
    from server.memory.app import run_retention
    from server.settings import settings
    from server.tasks.api.models_run import RunCompleteInput
    from server.tasks.app import run as h

    seen: list[str] = []

    async def fake_apply(**kw: Any) -> run_retention.Counts:
        seen.append(kw["mode"])
        return run_retention.Counts(retained=2, demoted=1)

    async def record(**kw: Any) -> None:
        return None

    class _CH:
        def __init__(self) -> None:
            self.inserted: list[Any] = []

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            return {
                "org": "o",
                "namespace": "n",
                "agent": "claude",
                "user_id": "-",
                "session_id": "-",
                "run_id": "r1",
                "goal": "g",
                "user_request": "",
                "expected_outcome": "",
                "environment": "",
                "model": "",
                "consumer": "",
                "status": "active",
                "started_at": T0,
                "completed_at": None,
                "duration_ms": 0,
                "final_response": "",
                "confidence": 0.0,
                "memories_used": [],
                "memories_written": [],
                "metadata": "{}",
            }

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

        async def insert(self, table: str, rows: Any, settings: Any = None) -> None:
            self.inserted.append((table, rows))

    monkeypatch.setattr(run_retention, "apply", fake_apply)
    monkeypatch.setattr(memory_repo, "record_usage_event", record)
    monkeypatch.setattr(settings, "memory_run_complete_retain", "expire")
    with patch.object(h, "get_client", return_value=_CH()):
        out = _run(
            h.handle_run_complete(
                RunCompleteInput(
                    org="o", namespace="n", run_id="r1", status="completed", retain="demote"
                )
            )
        )
        assert (out.retain, out.retained, out.demoted, out.expiring) == ("demote", 2, 1, 0)
        out2 = _run(
            h.handle_run_complete(
                RunCompleteInput(org="o", namespace="n", run_id="r1", status="completed")
            )
        )
        assert out2.retain == "expire"
    assert seen == ["demote", "expire"]
