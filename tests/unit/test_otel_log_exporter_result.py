"""The ClickHouse log exporter answers the SDK's `LogRecordExportResult`:
SUCCESS for an empty batch and a written one, FAILURE when the insert fails,
and SUCCESS once it has disabled itself on a missing logs table."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk._logs.export import LogRecordExportResult

from server.observability import otel_exporters


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.inserts: list[str] = []

    def insert(self, table: str, **_kw: Any) -> None:
        if self.error is not None:
            raise self.error
        self.inserts.append(table)

    def close(self) -> None:
        pass


def _exporter(monkeypatch: pytest.MonkeyPatch, client: _Client) -> Any:
    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: client)
    monkeypatch.setattr(otel_exporters, "_log_to_row", lambda rec, service: {"Body": str(rec)})
    return otel_exporters._make_clickhouse_log_exporter("loom")


def test_an_empty_and_a_written_batch_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    exporter = _exporter(monkeypatch, client)
    assert exporter.export([]) is LogRecordExportResult.SUCCESS
    assert exporter.export(["a record"]) is LogRecordExportResult.SUCCESS
    assert client.inserts == ["default.otel_logs"]


def test_a_failed_insert_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = _exporter(monkeypatch, _Client(RuntimeError("connection reset")))
    assert exporter.export(["a record"]) is LogRecordExportResult.FAILURE


def test_a_missing_logs_table_disables_export(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(RuntimeError("Code: 60. Unknown table default.otel_logs"))
    monkeypatch.setattr(otel_exporters, "_unwritable", lambda e: "it does not exist")
    exporter = _exporter(monkeypatch, client)
    assert exporter.export(["a record"]) is LogRecordExportResult.SUCCESS
    client.error = None
    assert exporter.export(["a record"]) is LogRecordExportResult.SUCCESS
    assert client.inserts == []


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        ("Code: 60. DB::Exception: Table default.otel_logs does not exist", True),
        ("Code: 497. DB::Exception: loom_app: Not enough privileges", True),
        ("Code: 241. DB::Exception: Memory limit exceeded", False),
    ],
)
def test_a_missing_table_or_a_refused_grant_disables_export(error: str, permanent: bool) -> None:
    from server.observability import otel_exporters

    assert bool(otel_exporters._unwritable(RuntimeError(error))) is permanent
