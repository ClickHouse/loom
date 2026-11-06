"""`loom knowledge migrate-originals` refuses to move originals before the server writes
new ones to the bucket (a dry run sizes the move before that), refuses a namespace without
its org, and reports every switched but unconfirmed original even when an error stops
the run."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest

from server.cli import knowledge
from server.settings import settings


def _args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**{"org": "", "namespace": "", "dry_run": False, **kw})


def test_it_refuses_while_new_originals_go_to_clickhouse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    monkeypatch.setattr(knowledge, "run_connected", lambda _run: pytest.fail("connected"))
    assert knowledge._cmd_migrate_originals(_args()) == 2
    assert "LOOM_DOCUMENT_OBJECT_STORE=s3" in capsys.readouterr().err


def test_a_namespace_needs_its_org(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "document_object_store", "s3")
    monkeypatch.setattr(knowledge, "run_connected", lambda _run: pytest.fail("connected"))
    assert knowledge._cmd_migrate_originals(_args(namespace="docs")) == 2
    assert "--namespace needs --org" in capsys.readouterr().err


def _connected(run: Any) -> Any:
    return asyncio.run(run(None))


def test_a_dry_run_sizes_the_move_before_the_store_is_switched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.knowledge.app import originals

    async def counted(**kw: Any) -> originals.Migrated:
        assert kw["dry_run"] is True
        return originals.Migrated(pending=3, bytes=300, unneeded=1)

    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    monkeypatch.setattr(knowledge, "run_connected", _connected)
    monkeypatch.setattr(originals, "migrate_originals", counted)
    assert knowledge._cmd_migrate_originals(_args(dry_run=True)) == 0
    assert "3 original(s), 300 bytes, held in ClickHouse" in capsys.readouterr().out


def test_an_unconfirmed_switch_is_printed_when_an_error_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import object_store
    from server.knowledge.app import originals

    async def stopped(**kw: Any) -> originals.Migrated:
        kw["out"].unconfirmed.append("acme/docs/abc: switched to s3://b/k, but the check failed")
        raise RuntimeError("ClickHouse went away")

    monkeypatch.setattr(settings, "document_object_store", "s3")
    monkeypatch.setattr(knowledge, "run_connected", _connected)
    monkeypatch.setattr(object_store, "require_client", lambda: None)
    monkeypatch.setattr(originals, "migrate_originals", stopped)
    with pytest.raises(RuntimeError, match="ClickHouse went away"):
        knowledge._cmd_migrate_originals(_args())
    printed = capsys.readouterr()
    assert "switched, unconfirmed: acme/docs/abc: switched to s3://b/k" in printed.err
    assert "1 switched but unconfirmed" in printed.out
