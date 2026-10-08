"""The ledger records who called; "last used" reads it.

Joining `loom.agent_events` on `user_id LIKE 'sa:%'` matches nothing:
`user_id` is the request body's field and `consumer` the subject's class, so
every token would read "never used" after scanning the org's rows. The
ledger row carries the verified token subject; the Setup
page's `sa_last_used` groups by it inside a 30-day window.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import server.auth.context as auth_ctx
from server.observability import events as ev
from server.observability import ledger
from tests.unit.schema_ddl import ddl


def test_the_ledger_row_carries_the_subject() -> None:
    row = ev.AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
        subject="sa:claude-abc",
    ).to_row()
    assert row["subject"] == "sa:claude-abc"
    assert (
        ev.AgentEvent(
            ts=datetime.now(UTC),
            org="o",
            namespace="n",
            agent="a",
            user_id="-",
            session_id="-",
            operation="x",
            request_id="r",
            latency_ms=1,
            success=True,
        ).to_row()["subject"]
        == ""
    )


def test_the_emitter_stamps_the_verified_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[ev.AgentEvent] = []
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    monkeypatch.setattr(
        auth_ctx,
        "current_auth",
        lambda: SimpleNamespace(subject="sa:cursor-1f2e", impersonated_by=""),
    )
    ledger.emit_agent_event(
        op_name="memory.search",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-"},
        request_id="r1",
        latency_ms=3,
        success=True,
        error_code="",
        raw_args={"org": "o", "namespace": "n"},
    )
    (event,) = captured
    assert event.subject == "sa:cursor-1f2e" and event.user_id == "u"
    # an explicit subject wins; outside a request the column is ''
    captured.clear()
    ledger.emit_agent_event(
        op_name="memory.search",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-"},
        request_id="r2",
        latency_ms=3,
        success=True,
        error_code="",
        subject="worker:x",
    )
    assert captured[0].subject == "worker:x"
    monkeypatch.setattr(auth_ctx, "current_auth", lambda: None)
    captured.clear()
    ledger.emit_agent_event(
        op_name="memory.search",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-"},
        request_id="r3",
        latency_ms=3,
        success=True,
        error_code="",
    )
    assert captured[0].subject == ""


def test_last_used_reads_the_subject_inside_a_month() -> None:
    src = Path("server/identity/app/me_service_accounts.py").read_text(encoding="utf-8")
    cte = src[src.index("sa_last_used AS (") :]
    cte = cte[: cte.index("\n        )")]
    assert "subject LIKE 'sa:%'" in cte and "GROUP BY subject" in cte
    # The row per subject, not a month of the ledger
    assert "FROM loom.subject_last_seen" in cte and "maxMerge(last_seen)" in cte
    assert "FROM loom.agent_events" not in cte and "user_id LIKE" not in cte


def test_the_schema_carries_the_subject_column() -> None:
    assert "`subject` LowCardinality(String) DEFAULT ''" in ddl("agent_events")
