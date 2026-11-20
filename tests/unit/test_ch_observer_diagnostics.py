"""Native observer diagnostics retain codes without exporting policy or transport text."""

from __future__ import annotations

import asyncio
import logging

import pytest

from server.core import ch_observer
from tests.unit.test_ch_policy_observer import _body
from tests.unit.test_ch_policy_observer import observer as observer


async def test_http_refusal_logs_status_without_reading_its_body_or_replaying(observer, caplog):
    class UnreadBody:
        def __len__(self):
            raise AssertionError("private-response-body was read")

    instance, http = observer([(503, UnreadBody()), (200, _body([{"closed": 1}]))])
    try:
        with (
            caplog.at_level("WARNING", logger=ch_observer.__name__),
            pytest.raises(
                ch_observer.ObservationUnavailable,
                match="^ClickHouse observation authority could not be confirmed$",
            ),
        ):
            await instance.bootstrap()
        records = [r for r in caplog.records if r.name == ch_observer.__name__]
        assert len(records) == 1
        record = records[0]
        assert record.msg == (
            "Native observation refused: stage=%s exception=%s code=%s http_status=%s"
        )
        assert record.args == ("http_status", "ObservationUnavailable", None, 503)
        assert record.exc_info is None and record.stack_info is None
        assert record.__dict__.keys() <= logging.makeLogRecord({}).__dict__.keys() | {
            "message",
            "asctime",
        }
        assert "private-response-body" not in caplog.text and "writer" not in caplog.text
        with pytest.raises(ch_observer.ObservationUnavailable):
            await instance.bootstrap()
        assert len(http.calls) == 1
    finally:
        await instance.close()
    assert http.closed
    assert [call[1]["params"]["session_check"] for call in http.calls] == ["0", "1"]


async def test_transport_failure_logs_only_phase_type_and_numeric_code(observer, caplog):
    instance, http = observer(
        [
            RuntimeError(
                "Code: 999. private-policy SELECT private-key https://example.invalid/private-session"
            ),
            (200, _body([{"closed": 1}])),
        ]
    )
    try:
        with (
            caplog.at_level("WARNING", logger=ch_observer.__name__),
            pytest.raises(ch_observer.ObservationUnavailable),
        ):
            await instance.bootstrap()
        records = [r for r in caplog.records if r.name == ch_observer.__name__]
        assert len(records) == 1
        assert records[0].args == ("send", "RuntimeError", 999, None)
        assert records[0].exc_info is None and records[0].stack_info is None
        assert all(
            text not in caplog.text
            for text in (
                "private-policy",
                "SELECT",
                "private-key",
                "example.invalid",
                "private-session",
            )
        )
        assert len(http.calls) == 1
    finally:
        await instance.close()
    assert http.closed and len(http.calls) == 2


async def test_diagnostic_failure_preserves_native_refusal_cancellation_and_local_close(
    observer, monkeypatch
):
    warnings = []

    def broken_warning(self, *args, **kwargs):
        warnings.append(True)
        raise ValueError("diagnostic sink failed")

    monkeypatch.setattr(logging.Logger, "warning", broken_warning)
    instance, http = observer(
        [RuntimeError("Code: 999. private-policy"), (200, _body([{"closed": 1}]))]
    )
    try:
        with pytest.raises(ch_observer.ObservationUnavailable):
            await instance.bootstrap()
    finally:
        await instance.close()
    cancelled, cancelled_http = observer([asyncio.CancelledError(), (200, _body([{"closed": 1}]))])
    try:
        with pytest.raises(asyncio.CancelledError):
            await cancelled.bootstrap()
    finally:
        await cancelled.close()
    assert warnings == [True]
    assert http.closed and cancelled_http.closed
    assert len(http.calls) == len(cancelled_http.calls) == 2
