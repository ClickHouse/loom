"""A backend that refused or timed out logs one warning line, not a traceback.

An external backend failing closed is a failure the design expects (the
recovery worker retries), so a known backend failure names its step and
cause on one WARNING line instead of writing tracebacks on every retry; the
traceback stays for the unexpected exception.
"""

from __future__ import annotations

import logging

import pytest

from server.erasure.app import saga as h
from server.erasure.app.backends import ErasureBackendError
from tests.unit.test_erasure_census_tail import _run


async def _backend_refuses(org, namespace, *, completed, row, verify=None):
    async def _boom() -> int:
        raise ErasureBackendError("langfuse.delete_namespace ns transport error: ReadTimeout")

    return [
        await h._do_step(
            step_name="external.delete_namespace", step_fn=_boom, completed=completed, row=row
        )
    ]


async def _bug(org, namespace, *, completed, row, verify=None):
    async def _boom() -> int:
        raise KeyError("residue")

    return [
        await h._do_step(step_name="memory.tombstone", step_fn=_boom, completed=completed, row=row)
    ]


@pytest.mark.asyncio
async def test_a_backend_failure_is_one_warning_line(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=h.log.name)
    out, _, _ = await _run(monkeypatch, saga=_backend_refuses)
    assert out.status == "failed"
    ours = [r for r in caplog.records if "erasure e1 failed" in r.getMessage()]
    assert len(ours) == 1
    rec = ours[0]
    assert rec.levelno == logging.WARNING and rec.exc_info is None
    assert "external.delete_namespace" in rec.getMessage() and "ReadTimeout" in rec.getMessage()


@pytest.mark.asyncio
async def test_an_unexpected_failure_keeps_its_traceback(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=h.log.name)
    out, _, _ = await _run(monkeypatch, saga=_bug)
    assert out.status == "failed"
    ours = [r for r in caplog.records if "erasure e1 failed mid-saga" in r.getMessage()]
    assert len(ours) == 1 and ours[0].levelno == logging.ERROR and ours[0].exc_info is not None


def test_the_external_timeout_default_is_thirty_seconds() -> None:
    from server.settings import Settings

    assert Settings.model_fields["external_erasure_timeout_s"].default == 30.0


async def _bucket_down(org, namespace, *, completed, row, verify=None):
    from server.core.object_store import ObjectStoreError

    async def _boom() -> int:
        raise ObjectStoreError("object store list failed (SlowDown)", operation="list")

    return [
        await h._do_step(
            step_name="documents.tombstone", step_fn=_boom, completed=completed, row=row
        )
    ]


@pytest.mark.asyncio
async def test_a_document_bucket_outage_is_one_warning_line(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=h.log.name)
    out, _, _ = await _run(monkeypatch, saga=_bucket_down)
    assert out.status == "failed"
    ours = [r for r in caplog.records if "erasure e1 failed" in r.getMessage()]
    assert len(ours) == 1 and ours[0].levelno == logging.WARNING and ours[0].exc_info is None
    assert "documents.tombstone" in ours[0].getMessage()
