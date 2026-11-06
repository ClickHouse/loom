"""`loom knowledge migrate-originals` refuses to run before the server writes new
originals to the bucket, and refuses a namespace without its org; neither touches
ClickHouse."""

from __future__ import annotations

import argparse

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
