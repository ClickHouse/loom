from __future__ import annotations

import asyncio
import logging

import pytest

from server.core.logging_config import request_id_var
from server.knowledge.store import mirror_latch


def _record_unavailable() -> None:
    progress = mirror_latch._ProofProgress("enabled.latch")
    mirror_latch._log_unavailable(progress, TimeoutError("private exception body"), None)


@pytest.mark.asyncio
async def test_concurrent_proof_diagnostics_keep_request_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()

    async def emit(request_id: str, ready: asyncio.Event, other: asyncio.Event) -> None:
        token = request_id_var.set(request_id)
        try:
            ready.set()
            await other.wait()
            _record_unavailable()
        finally:
            request_id_var.reset(token)

    parent = request_id_var.set("parent-request")
    try:
        with caplog.at_level(logging.WARNING, logger=mirror_latch.log.name):
            await asyncio.gather(
                emit("proof-request-a", first_ready, second_ready),
                emit("proof-request-b", second_ready, first_ready),
            )
        assert request_id_var.get() == "parent-request"
    finally:
        request_id_var.reset(parent)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    for request_id in ("proof-request-a", "proof-request-b"):
        assert sum(f"request_id={request_id} " in message for message in messages) == 1
    assert all("stage=enabled.latch" in message for message in messages)
    assert all("exception=TimeoutError" in message for message in messages)
    assert all("parent-request" not in message for message in messages)
    assert all("private exception body" not in message for message in messages)


@pytest.mark.parametrize("request_id", [None, ""])
def test_proof_diagnostic_marks_missing_request_context(
    caplog: pytest.LogCaptureFixture, request_id: str | None
) -> None:
    token = request_id_var.set(request_id)
    try:
        with caplog.at_level(logging.WARNING, logger=mirror_latch.log.name):
            _record_unavailable()
    finally:
        request_id_var.reset(token)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "request_id=unavailable " in message
    assert "private exception body" not in message
