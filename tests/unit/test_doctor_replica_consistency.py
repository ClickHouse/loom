"""The `replica_consistency` doctor line: sequential reads are exact only on
SharedMergeTree or one server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.cli import doctor


def test_one_replica_or_shared_merge_tree_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: [])
    assert (
        doctor._doctor_replica_consistency_check(SimpleNamespace(storage_authority="clickhouse"))[1]
        == doctor.PASS
    )


def test_a_replicated_attempts_table_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"table": "auth_attempts", "total_replicas": 3}]
    monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: rows)
    name, grade, detail = doctor._doctor_replica_consistency_check(
        SimpleNamespace(storage_authority="clickhouse")
    )
    assert (name, grade) == ("replica_consistency", doctor.WARN)
    assert "loom.auth_attempts (3 replicas)" in detail and "one replica" in detail


def test_an_unreadable_catalog_warns_and_is_skipped_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: object) -> list[dict[str, str]]:
        raise ConnectionError("down")

    monkeypatch.setattr(doctor, "_doctor_ch_query", boom)
    assert (
        doctor._doctor_replica_consistency_check(SimpleNamespace(storage_authority="clickhouse"))[1]
        == doctor.WARN
    )
    assert "replica_consistency" in [n for n, _, _ in doctor._doctor_ch_read_checks(None)]


def test_the_task_tables_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    # task.create's idempotency and the coverage ready event's exactly-once rest on a
    # sequential read seeing another process's insert, as the sign-in limits do.
    asked: list[dict[str, list[str]]] = []
    monkeypatch.setattr(
        doctor, "_doctor_ch_query", lambda _s, _sql, params: asked.append(params) or []
    )
    doctor._doctor_replica_consistency_check(SimpleNamespace(storage_authority="clickhouse"))
    assert {"tasks", "task_events"} <= set(asked[0]["tables"])
    # The document bucket's walk and user erasure rest on it too.
    assert {"document_blobs", "blob_uploads"} <= set(asked[0]["tables"])


def test_loom_documents_is_checked_under_either_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A knowledge delete re-reads the document after a restore under either authority."""
    asked: list[dict[str, list[str]]] = []

    def query(_s: object, _sql: str, params: dict[str, list[str]]) -> list[dict[str, str]]:
        asked.append(params)
        return []

    monkeypatch.setattr(doctor, "_doctor_ch_query", query)
    doctor._doctor_replica_consistency_check(SimpleNamespace())
    assert "documents" in asked[0]["tables"]
