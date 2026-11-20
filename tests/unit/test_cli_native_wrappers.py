"""Actual native entry points preserve first failures across their outer close."""

import argparse
import asyncio
import sqlite3
from functools import partial
from types import SimpleNamespace

import pytest

from server.cli import (
    _ch,
    analytics_export,
    code_run,
    import_brain,
    import_clickgap,
    import_recall,
    maintenance,
    sql_access,
)
from server.insights.app import analytics_export as export_app
from tests.unit.test_cli_native_first_error import FIRST
from tests.unit.test_cli_native_first_error import native as native


@pytest.fixture
def wrappers(native, monkeypatch):
    from server.code.app.index import repo_registry
    from server.memory.app import reextract

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _nothing)
    monkeypatch.setattr(analytics_export, "_refusal", lambda *_: "")
    monkeypatch.setattr(export_app, "check_credentials", lambda **_: None)
    monkeypatch.setattr(export_app, "name_hash_key", lambda _: b"inert")
    monkeypatch.setattr(export_app, "s3_put", lambda *_, **__: None)
    monkeypatch.setattr(maintenance, "resolve_token", lambda _: ("", "none"))
    monkeypatch.setattr(reextract, "replay", _report)
    monkeypatch.setattr(reextract, "render_report", lambda *_, **__: "report")
    return native


async def _nothing(*_, **__):
    return None


async def _report(*_, **__):
    return SimpleNamespace(failed=0, sessions=1)


def _invoke(family, work, monkeypatch):
    if family == "sql":
        return sql_access._connected(work, orgs=("o",))
    if family == "export":
        monkeypatch.setattr(export_app, "export_day", work)
        return analytics_export._cmd_export(
            argparse.Namespace(
                day=None,
                role_arn="",
                region="",
                bucket="scratch",
                expected_bucket_owner="",
                prefix="",
            )
        )
    if family == "reextract":
        from server.memory.app import reextract

        monkeypatch.setattr(reextract, "load_sessions", work)
        return maintenance._cmd_reextract(
            argparse.Namespace(
                namespace="n",
                into="other",
                token="",
                dry_run=True,
                org="o",
                limit_sessions=None,
                base_url="",
                dates=False,
                concurrency=1,
            )
        )
    if family == "code":
        return asyncio.run(code_run._then_disconnect(work()))
    raise AssertionError(family)


@pytest.mark.parametrize("family", ["sql", "export", "reextract", "code"])
@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.parametrize("second_close", [False, True])
def test_actual_wrappers_preserve_work_failure(
    wrappers, monkeypatch, family, first_class, second_close
):
    first = first_class("work")
    if second_close:
        wrappers.errors.update(pg=OSError("pg close"), ch=RuntimeError("ch close"))

    async def work(*_, **__):
        assert wrappers.gate.enrolled and wrappers.gate.active > 0
        raise first

    with pytest.raises(first_class) as caught:
        _invoke(family, work, monkeypatch)
    assert caught.value is first
    if second_close:
        assert wrappers.events[-2:] == ["pg", "ch"]
        assert "certificate" not in wrappers.events
    else:
        assert wrappers.events[-3:] == ["pg", "certificate", "ch"]
    assert len(wrappers.terminal_ends) == 1 and not wrappers.reversible_closes
    assert not wrappers.client.connected
    assert wrappers.gate.active == wrappers.model_gate.active == 0


@pytest.mark.parametrize("family", ["sql", "export", "reextract", "code"])
@pytest.mark.parametrize("failed_close", [False, True])
def test_successful_actual_wrappers_still_require_cleanup(
    wrappers, monkeypatch, family, failed_close
):
    first = OSError("pg close")
    if failed_close:
        wrappers.errors.update(pg=first, ch=RuntimeError("ch close"))

    async def work(*_, **__):
        return [] if family == "export" else ([{}] if family == "reextract" else 7)

    if failed_close:
        with pytest.raises(OSError) as caught:
            _invoke(family, work, monkeypatch)
        assert caught.value is first
    else:
        assert _invoke(family, work, monkeypatch) == (0 if family in ("export", "reextract") else 7)
    if failed_close:
        assert wrappers.events[-3:] == ["flush", "pg", "ch"]
        assert "certificate" not in wrappers.events
    else:
        assert wrappers.events[-3:] == ["pg", "certificate", "ch"]
    assert len(wrappers.terminal_ends) == 1 and not wrappers.reversible_closes


@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.parametrize("reopen_failure", [False, True])
def test_code_reconnect_cannot_replace_subcommand_failure(
    wrappers, monkeypatch, first_class, reopen_failure
):
    first = first_class("subcommand")
    events = []

    async def reconnect(*_, **__):
        events.append("reconnect")
        if reopen_failure:
            raise OSError("reconnect")

    async def work():
        raise first

    monkeypatch.setattr(code_run, "connect_declared", reconnect)
    with pytest.raises(first_class) as caught:
        asyncio.run(code_run._then_disconnect(work()))
    assert caught.value is first
    assert events == ["reconnect"]
    assert wrappers.events[-3:] == ["pg", "certificate", "ch"]


@pytest.mark.parametrize("first_class", FIRST)
@pytest.mark.parametrize("phase", ["entry", "participant-close"])
@pytest.mark.asyncio
async def test_actual_recall_writer_entry_and_closer_preserve_first(
    wrappers, monkeypatch, first_class, phase
):
    first = first_class("first")
    if phase == "entry":
        wrappers.errors["acceptance"] = first
        wrappers.errors["pg"] = OSError("pg close")
    else:
        wrappers.errors["certificate"] = first
    wrappers.errors["ch"] = RuntimeError("ch close")
    args = argparse.Namespace(base_url=None, org="o")
    with pytest.raises(first_class) as caught:
        _, closer = await import_recall._make_writer(args, lambda: {}, timeout_s=1)
        await closer()
    assert caught.value is first
    if phase == "entry":
        assert wrappers.events[-2:] == ["pg", "ch"]
        assert "certificate" not in wrappers.events
    else:
        assert wrappers.events[-3:] == ["pg", "certificate", "ch"]
    assert len(wrappers.terminal_ends) == 1 and not wrappers.reversible_closes
    assert wrappers.gate.active == wrappers.model_gate.active == 0


@pytest.mark.parametrize("family", ["recall", "brain"])
@pytest.mark.parametrize("first_class", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
def test_actual_importer_outer_closer_preserves_unhandled_work_failure(
    wrappers, monkeypatch, tmp_path, family, first_class
):
    first = first_class("write cancelled or fatal")
    wrappers.errors.update(pg=OSError("pg close"), ch=RuntimeError("ch close"))

    async def handler(_):
        raise first

    if family == "brain":
        (tmp_path / "MEMORY.md").write_text("## A\ncontent\n")
        monkeypatch.setattr(import_brain, "_brain_inproc_ops", lambda: {"profile.set": handler})
        args = argparse.Namespace(
            dir=str(tmp_path), dry_run=False, org="o", namespace="n", agent="a", base_url=None
        )
        call = import_brain._cmd_import_brain
    else:
        path = tmp_path / "source.db"
        with sqlite3.connect(path):
            pass
        report = {
            "tables": {},
            "memories_imported": 1,
            "messages_imported": 0,
            "threads": 0,
            "skipped_rows": 0,
        }
        monkeypatch.setattr(
            import_recall,
            "_recall_plan_writes",
            lambda *_, **__: ([{"op": "memory.set", "payload": {}}], report),
        )
        monkeypatch.setattr(import_recall, "_recall_inproc_ops", lambda: {"memory.set": handler})
        args = argparse.Namespace(
            sqlite=str(path), dry_run=False, org="o", namespace="n", agent="a", base_url=None
        )
        call = import_recall._cmd_import_recall
    with pytest.raises(first_class) as caught:
        call(args)
    assert caught.value is first
    assert wrappers.events[-2:] == ["pg", "ch"]


@pytest.mark.parametrize("first_class", FIRST)
def test_actual_clickgap_outer_disconnect_preserves_admission_failure(
    wrappers, monkeypatch, tmp_path, first_class
):
    from server import embedding

    first = first_class("admission")
    wrappers.errors.update(acceptance=first, pg=OSError("pg close"), ch=RuntimeError("ch close"))
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as source:
        source.execute(
            "CREATE TABLE lessons (id TEXT, text TEXT, source TEXT, created_at TEXT, kind TEXT, expires_at TEXT, evidence_count INT, recall_count INT, last_recalled_at TEXT, reasoning TEXT)"
        )
    monkeypatch.setattr(import_clickgap, "_clickgap_count_source", lambda *_, **__: {})
    monkeypatch.setattr(import_clickgap, "_clickgap_preload", lambda *_, **__: ({}, [], {}, {}))
    monkeypatch.setattr(embedding, "get_embedder", lambda: SimpleNamespace(model_name="inert"))
    args = argparse.Namespace(
        sqlite=str(path),
        org="o",
        namespace="n",
        agent="a",
        dry_run=False,
        batch_size=1,
        seed_recall_events=False,
    )
    with pytest.raises(first_class) as caught:
        import_clickgap._cmd_import_clickgap(args)
    assert caught.value is first
    assert wrappers.events[-2:] == ["pg", "ch"]


@pytest.mark.doctor_tier_read
@pytest.mark.parametrize("first_class", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("diagnostic", ["cell", "strays", "registry", "tiers"])
def test_actual_diagnostic_closes_preserve_unhandled_native_failure(
    wrappers, monkeypatch, first_class, diagnostic
):
    from server.cli import doctor, doctor_cell
    from server.code.app.index import repo_registry, tier_health
    from server.core import settings_overrides
    from server.settings import settings

    first = first_class("diagnostic")
    wrappers.errors.update(pg=OSError("pg close"), ch=RuntimeError("ch close"))

    async def fail(*_, **__):
        raise first

    if diagnostic == "cell":
        monkeypatch.setattr(doctor_cell, "_checks", fail)
        call = doctor_cell.cell_checks
    elif diagnostic == "strays":
        monkeypatch.setattr(doctor_cell, "_deployment_check", _nothing)
        monkeypatch.setattr(doctor_cell, "_stray_reads", fail)
        call = partial(doctor_cell.stray_org_checks, 1)
    else:
        monkeypatch.setattr(settings, "ch_host", "inert")
        if diagnostic == "registry":
            monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
            monkeypatch.setattr(repo_registry, "refresh_db_repos", fail)
            call = doctor._read_code_registry
        else:
            monkeypatch.setattr(settings, "code_intel_enabled", True)
            monkeypatch.setattr(doctor, "_read_code_registry", lambda: None)
            monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
            monkeypatch.setattr(settings_overrides, "refresh", _nothing)
            monkeypatch.setattr(tier_health, "refresh_failing", fail)
            doctor._doctor_tier_failures.cache_clear()
            call = doctor._doctor_tier_failures
    with pytest.raises(first_class) as caught:
        call()
    assert caught.value is first
    assert wrappers.events[-2:] == ["pg", "ch"]
    doctor._doctor_tier_failures.cache_clear()


@pytest.mark.parametrize("first_class", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_actual_setup_store_diagnostic_preserves_unhandled_failure(
    wrappers, monkeypatch, first_class
):
    from server.core import pg, pg_profile
    from server.settings import settings

    first = first_class("setup diagnostic")
    wrappers.errors.update(pg=OSError("pg close"), ch=RuntimeError("ch close"))
    monkeypatch.setattr(settings, "cell", "c")
    monkeypatch.setattr(pg, "enabled", lambda: True)

    async def refusal(_):
        raise first

    monkeypatch.setattr(pg_profile, "recorded_authority", refusal)
    with pytest.raises(first_class) as caught:
        await _ch.check_setup_stores()
    assert caught.value is first
    assert wrappers.events[-2:] == ["pg", "ch"]
