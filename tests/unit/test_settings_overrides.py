"""A live setting takes a deployment-wide override stored in loom.runtime_overrides:
the writing process applies it at once, every other one on its next refresh, and
clearing it puts back the value each process started with."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.core import runtime_overrides as ro
from server.core import settings_overrides as so
from server.core.errors import ValidationError, register_exception_handler
from server.settings import settings

LIVE = "memory_ask_recency_weight"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(so, "_applied", {})
    monkeypatch.setattr(so, "_started_with", {})
    monkeypatch.setattr(so, "_warned", set())
    monkeypatch.setattr(so, "_assigned", {})
    monkeypatch.setattr(settings, LIVE, settings.__class__.model_fields[LIVE].default)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, ...], Any]:
    """loom.runtime_overrides' `setting` rows as `put`, `clear` and `load` see them."""
    rows: dict[tuple[str, ...], Any] = {}

    async def put(kind: str, key: tuple[str, ...], value: Any, *, ch: Any = None) -> None:
        assert kind == so.KIND
        rows[key] = value

    async def clear(kind: str, key: tuple[str, ...], *, ch: Any = None) -> None:
        rows.pop(key, None)

    async def load(*, ch: Any = None) -> ro.Snapshot:
        return {so.KIND: dict(rows)}

    monkeypatch.setattr(ro, "put", put)
    monkeypatch.setattr(ro, "clear", clear)
    monkeypatch.setattr(ro, "load", load)
    return rows


async def test_change_stores_the_validated_value_and_applies_it_here(stored: dict) -> None:
    before, row = await so.change(LIVE, "0.5")
    assert before is not None and (before["value"], before["source"]) == (0.2, "default")
    assert stored == {(LIVE,): 0.5}
    assert settings.memory_ask_recency_weight == 0.5
    assert row is not None and (row["value"], row["source"]) == (0.5, "override")
    before, row = await so.change(LIVE, clear=True)
    assert before is not None and (before["value"], before["source"]) == (0.5, "override")
    assert stored == {}
    assert settings.memory_ask_recency_weight == 0.2
    assert row is not None and row["source"] == "default"


async def test_the_value_before_is_the_stored_override_not_this_process_view(
    stored: dict,
) -> None:
    stored[(LIVE,)] = 0.35  # set through another process, not adopted here yet
    before, _ = await so.change(LIVE, 0.4)
    assert before is not None and (before["value"], before["source"]) == (0.35, "override")


async def test_a_setting_read_at_startup_takes_no_override(stored: dict) -> None:
    with pytest.raises(ValidationError, match="read once at startup"):
        await so.change("ch_host", "elsewhere")
    with pytest.raises(ValidationError, match="no setting is named"):
        await so.change("no_such_setting", 1)
    assert stored == {}


async def test_a_value_the_field_refuses_is_not_stored(stored: dict) -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        await so.change("memory_ask_synth_max_tokens_ceiling", 0)  # ge=1
    with pytest.raises(ValidationError, match="not 'nonsense'"):
        await so.change("memory_search_default_mode", "nonsense")
    assert stored == {}


async def test_reset_needs_a_stored_override(stored: dict) -> None:
    with pytest.raises(ValidationError, match="no override to reset"):
        await so.change("ch_host", clear=True)
    with pytest.raises(ValidationError, match="no override to reset"):
        await so.change(LIVE, clear=True)


async def test_reset_removes_the_override_of_a_setting_this_build_lacks(stored: dict) -> None:
    stored[("retired_setting",)] = 3
    assert await so.change("retired_setting", clear=True) == (None, None)
    assert stored == {}


def test_adopt_lays_the_table_over_this_process_and_restores_what_left_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, LIVE, 0.3)  # as this process's env set it
    so.adopt({so.KIND: {(LIVE,): 0.9}})
    assert settings.memory_ask_recency_weight == 0.9
    so.adopt({so.KIND: {(LIVE,): 0.7}})
    assert settings.memory_ask_recency_weight == 0.7
    so.adopt({})
    assert settings.memory_ask_recency_weight == 0.3
    assert so._applied == {} and so._started_with == {}


def test_adopt_skips_a_row_this_build_cannot_use(caplog: pytest.LogCaptureFixture) -> None:
    before = settings.ch_host
    so.adopt(
        {
            so.KIND: {
                ("ch_host",): "elsewhere",  # not live
                ("retired_setting",): 1,  # a field a later build removed
                ("memory_ask_synth_max_tokens_ceiling",): -1,  # out of range
                (LIVE,): 0.4,
            },
            ro.BUDGET_OP: {("memory.get",): 40},  # another kind: not a setting
        }
    )
    assert settings.ch_host == before and settings.memory_ask_recency_weight == 0.4
    assert len([r for r in caplog.records if "ignored" in r.message]) == 3
    so.adopt({so.KIND: {("ch_host",): "elsewhere", (LIVE,): 0.4}})
    assert len([r for r in caplog.records if "ignored" in r.message]) == 3  # said once


def test_rows_say_where_each_value_came_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LOOM_MEMORY_ASK_SYNTH_BUDGET_RATIO=0.5\nLOOM_CH_HOST=from-file\nLOOM_CH_PORT=9\n"
        "LOOM_LOG_FORMAT=JSON\n"
    )
    yaml_file = tmp_path / "loom.yaml"
    yaml_file.write_text(
        "version: 1\nsettings:\n  memory_ask_synth_budget_ratio: 0.4\n  ch_database: x\n  ch_telemetry_database: x_telemetry\n"
        "  web_workers: 3.0\n  runtime_dir: '/run/loom '\n"
    )
    for name in [k for k in os.environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(yaml_file))
    monkeypatch.setenv("LOOM_CH_HOST", "from-env")
    monkeypatch.setenv("LOOM_CH_PORT", "9")  # the file's value, copied in as the CLI does
    monkeypatch.setenv("LOOM_LOG_FORMAT", "JSON")  # copied too; the setting reads it as json
    monkeypatch.setenv("LOOM_WEB_WORKERS", "3")  # exported by scripts/serve.sh, validated
    monkeypatch.setenv("LOOM_RUNTIME_DIR", "/run/loom")  # and stripped
    monkeypatch.setenv("LOOM_LLM_MODEL", "gpt-4o")  # the provider is inferred from it
    loaded = settings.__class__(_env_file=str(env_file))
    monkeypatch.setattr(so, "settings", loaded)
    yaml_file.write_text("not: [valid")  # edited since: the listing does not read it again
    so.register_source("dashboard", lambda: ["embedding_model"])
    by_name = {r["name"]: r for r in so.rows()}
    assert by_name["ch_host"]["source"] == "env"  # the environment wins, as at startup
    assert by_name["ch_port"]["source"] == "env_file"
    assert (by_name["log_format"]["value"], by_name["log_format"]["source"]) == ("json", "env_file")
    assert by_name["memory_ask_synth_budget_ratio"]["source"] == "env_file"
    assert by_name["ch_database"]["source"] == "settings_file"
    assert by_name["web_workers"]["source"] == "settings_file"
    assert by_name["runtime_dir"]["source"] == "settings_file"
    assert by_name["llm_model"]["source"] == "env"
    assert (by_name["llm_provider"]["value"], by_name["llm_provider"]["source"]) == (
        "openai",
        "inferred",
    )
    assert by_name["embedding_model"]["source"] == "dashboard"
    assert by_name[LIVE]["source"] == "default" and by_name[LIVE]["live"] is True
    assert by_name["ch_host"]["live"] is False
    assert by_name[LIVE]["description"].startswith("memory.ask only: re-rank")
    assert by_name[LIVE]["env"] == "LOOM_MEMORY_ASK_RECENCY_WEIGHT"


def test_a_secret_shows_only_its_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "s" * 40)
    row = so.row("jwt_secret")
    assert row["value"] == "<masked, 40 chars>" and "s" * 8 not in str(row)
    monkeypatch.setattr(settings, "scim_token", "")
    assert so.row("scim_token")["value"] == ""  # unset shows as unset


def test_changed_keeps_the_settings_off_their_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, LIVE, 0.6)
    names = {r["name"] for r in so.rows(prefix="memory_ask_", changed=True)}
    assert LIVE in names and "memory_ask_synth_budget_ratio" not in names


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, **kw: Any) -> str:
        rows.append((event, kw))
        return ""

    monkeypatch.setattr("server.observability.audit.emit", emit)
    return rows


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, stored: dict, audits: list) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", False)
    from server.insights.app.admin_routes import admin_router

    app = FastAPI()
    register_exception_handler(app)
    app.include_router(admin_router)
    return TestClient(app, base_url="http://localhost")


def test_the_admin_routes_set_list_and_reset(
    client: TestClient, stored: dict, audits: list
) -> None:
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "value": 0.45})
    assert r.status_code == 200, r.text
    assert r.json()["setting"]["value"] == 0.45 and r.json()["setting"]["source"] == "override"
    assert stored == {(LIVE,): 0.45}
    listed = client.post("/v1/admin/settings.list", json={"prefix": LIVE}).json()["settings"]
    assert [s["value"] for s in listed if s["name"] == LIVE] == [0.45]
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "clear": True})
    assert r.json()["setting"] == {**r.json()["setting"], "value": 0.2, "source": "default"}
    assert stored == {}
    assert [(e, kw["subject"]) for e, kw in audits] == [
        (
            "setting.changed",
            {
                "name": LIVE,
                "value": 0.45,
                "previous": 0.2,
                "previous_source": "default",
                "cleared": False,
            },
        ),
        (
            "setting.changed",
            {
                "name": LIVE,
                "value": 0.2,
                "previous": 0.45,
                "previous_source": "override",
                "cleared": True,
            },
        ),
    ]


def test_the_admin_route_refuses_what_it_cannot_store(client: TestClient, audits: list) -> None:
    r = client.post("/v1/admin/settings.set", json={"name": "ch_host", "value": "x"})
    assert r.status_code == 422 and "read once at startup" in r.text
    r = client.post("/v1/admin/settings.set", json={"name": LIVE})
    assert r.status_code == 422  # neither a value nor clear
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "clear": True})
    assert r.status_code == 422 and "no override to reset" in r.text
    assert audits == []


def test_the_cli_records_the_stored_value_before_its_change(
    monkeypatch: pytest.MonkeyPatch,
    stored: dict,
    audits: list,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import server.cli._ch as cli_ch
    from server.cli import settings_cmd

    def run_connected(run: Any, **_declared: object) -> Any:
        import asyncio

        return asyncio.run(run(None))

    monkeypatch.setattr(cli_ch, "run_connected", run_connected)
    monkeypatch.setattr(settings_cmd, "run_connected", run_connected)
    stored[(LIVE,)] = 0.35  # an override this CLI process has not adopted
    args = argparse.Namespace(settings_cmd="set", name=LIVE, value="0.4", expect="0.2")
    assert settings_cmd._cmd_change(args) == 2  # the value in force is 0.35
    assert "not the expected '0.2'" in capsys.readouterr().err
    args = argparse.Namespace(settings_cmd="set", name=LIVE, value="0.4", expect=None)
    assert settings_cmd._cmd_change(args) == 0
    assert "✓ set: memory_ask_recency_weight = 0.4 (override)" in capsys.readouterr().out
    args = argparse.Namespace(settings_cmd="reset", name=LIVE, expect=None)
    assert settings_cmd._cmd_change(args) == 0
    assert [kw["subject"]["previous"] for _, kw in audits] == [0.35, 0.4]
    assert all(kw["actor"].startswith("cli:") for _, kw in audits)
    assert {(kw["org"], kw["namespace"]) for _, kw in audits} == {("-", "-")}
    assert settings_cmd._cmd_change(args) == 2  # nothing stored any more
    assert "no override to reset" in capsys.readouterr().err


async def test_expected_refuses_a_value_that_moved(stored: dict) -> None:
    stored[(LIVE,)] = 0.35  # another operator's change
    with pytest.raises(so.SettingMovedError, match="not the expected 0.2") as e:
        await so.change(LIVE, 0.4, expected=0.2)
    assert e.value.extra == {"current": 0.35} and stored == {(LIVE,): 0.35}
    _, row = await so.change(LIVE, 0.4, expected="0.35")  # written as for set
    assert row is not None and row["value"] == 0.4


def test_the_admin_route_refuses_a_change_whose_expected_value_moved(
    client: TestClient, stored: dict, audits: list
) -> None:
    stored[(LIVE,)] = 0.35
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "value": 0.4, "expected": 0.2})
    assert r.status_code == 409 and r.json()["detail"]["current"] == 0.35
    assert stored == {(LIVE,): 0.35} and audits == []


def test_the_history_route_reads_the_recorded_changes(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from server.insights.store import admin as admin_repo

    seen: list[tuple[str, str, int]] = []

    async def setting_changes(event: str, name: str, limit: int, *, ch: Any = None) -> list:
        seen.append((event, name, limit))
        return _HISTORY

    monkeypatch.setattr(admin_repo, "setting_changes", setting_changes)
    r = client.post("/v1/admin/settings.history", json={"name": "", "limit": 5})
    assert r.status_code == 200, r.text
    newer, older = r.json()["changes"]
    assert (newer["actor"], newer["previous"], newer["value"]) == ("cli:ops", 0.35, 0.4)
    assert newer["previous_source"] == "env"
    assert older["previous_source"] is None  # the reset of a retired setting's override
    assert seen == [("setting.changed", "", 5)]


_HISTORY = [
    {
        "ts": "2026-01-02 03:04:05.000",
        "actor": "cli:ops",
        "subject": json.dumps(
            {
                "name": LIVE,
                "value": 0.4,
                "previous": 0.35,
                "previous_source": "env",
                "cleared": False,
            }
        ),
    },
    {
        "ts": "2026-01-01 03:04:05.000",
        "actor": "cli:ops",
        "subject": json.dumps(
            {
                "name": "retired_setting",
                "value": None,
                "previous": None,
                "previous_source": None,
                "cleared": True,
            }
        ),
    },
]


def test_the_cli_history_says_when_an_undo_is_a_reset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from server.cli import settings_cmd
    from server.insights.store import admin as admin_repo

    async def setting_changes(event: str, name: str, limit: int, *, ch: Any = None) -> list:
        return _HISTORY

    monkeypatch.setattr(admin_repo, "setting_changes", setting_changes)
    monkeypatch.setattr(
        settings_cmd, "run_connected", lambda run, **_declared: asyncio.run(run(None))
    )
    args = argparse.Namespace(name="", limit=5, json=False)
    assert settings_cmd._cmd_history(args) == 0
    newer, older = capsys.readouterr().out.splitlines()
    assert "0.35 (undo: reset) → 0.4 (set)" in newer
    assert "retired_setting: null → null (reset)" in older


def test_a_cross_site_origin_is_refused_with_auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    from server.insights.app.admin_routes import refuse_browser_reach

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _: True)

    def request(host: str, headers: dict[str, str]) -> Request:
        raw = [(k.encode(), v.encode()) for k, v in {"host": host, **headers}.items()]
        scope = {
            "type": "http",
            "scheme": "https",
            "server": (host, 443),
            "path": "/",
            "headers": raw,
        }
        return Request(scope)

    refuse_browser_reach(request("loom.example.com", {}), "setting changes")  # a remote CLI
    refuse_browser_reach(
        request("loom.example.com", {"origin": "https://loom.example.com"}), "setting changes"
    )
    with pytest.raises(HTTPException) as e:
        refuse_browser_reach(
            request("loom.example.com", {"origin": "https://attacker.example"}), "setting changes"
        )
    assert e.value.status_code == 403


def test_a_change_a_web_page_could_send_is_refused(
    client: TestClient, stored: dict, audits: list
) -> None:
    body = {"name": LIVE, "value": 0.4}
    rebound = TestClient(client.app, base_url="http://attacker.example")  # auth off, DNS rebinding
    assert rebound.post("/v1/admin/settings.set", json=body).status_code == 403
    r = client.post(
        "/v1/admin/settings.set", json=body, headers={"origin": "http://attacker.example"}
    )
    assert r.status_code == 403
    r = client.post("/v1/admin/settings.set", json=body, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert stored == {} and audits == []


async def test_resetting_a_retired_setting_is_audited_with_no_value_before(
    stored: dict, audits: list
) -> None:
    from server.insights.app import settings_changes

    stored[("retired_setting",)] = 3
    assert await settings_changes.change("retired_setting", clear=True, actor="cli:ops") is None
    ((event, kw),) = audits
    assert event == "setting.changed" and (kw["org"], kw["namespace"]) == ("-", "-")
    assert kw["subject"] == {
        "name": "retired_setting",
        "value": None,
        "previous": None,
        "previous_source": None,
        "cleared": True,
    }


async def test_a_native_command_chunks_as_the_http_op_does_under_an_override(
    monkeypatch: pytest.MonkeyPatch, stored: dict
) -> None:
    """`loom rechunk` and knowledge.upsert share `_chunk_text`: inside a native command
    it reads the stored override, not the startup default."""
    from server.insights.app import provider_command as pc
    from server.knowledge.app import writes

    async def _noop(*_a: Any, **_k: Any) -> bool:
        return True

    for name in ("register_process", "load_active_configuration", "retire_process"):
        monkeypatch.setattr(pc, name, _noop)
    monkeypatch.setattr(pc, "drain_task_sets", _noop)
    monkeypatch.setattr(pc, "flush_registered_buffers", _noop)
    stored[("knowledge_chunk_size",)] = 0  # stored deployment-wide: one chunk per document
    text = "words " * 1000
    try:
        assert len(writes._chunk_text(text)) > 1  # the startup default
        async with pc.model_command():
            assert len(writes._chunk_text(text)) == 1
    finally:
        so.adopt({})


async def test_a_running_command_follows_a_new_override_and_logs_a_failing_run_once(
    stored: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    import logging

    from server.insights.app import provider_command as pc

    ticks: asyncio.Queue[None] = asyncio.Queue()

    async def one_interval(_seconds: float) -> None:
        await ticks.get()

    async def tick() -> None:
        ticks.put_nowait(None)
        for _ in range(5):
            await asyncio.sleep(0)

    real_load = ro.load

    async def unreachable(*, ch: Any = None) -> ro.Snapshot:
        raise RuntimeError("ClickHouse client not connected")

    so.adopt({})
    keeper = asyncio.create_task(pc._keep_overrides(True, sleep=one_interval))
    try:
        stored[("knowledge_chunk_size",)] = 0  # stored while the command runs
        await tick()
        assert settings.knowledge_chunk_size == 0
        monkeypatch.setattr(ro, "load", unreachable)
        with caplog.at_level(logging.WARNING, logger=pc.log.name):
            for _ in range(3):
                await tick()
        assert [r.getMessage() for r in caplog.records].count(
            "live-setting overrides not adopted: ClickHouse client not connected"
        ) == 1
        assert settings.knowledge_chunk_size == 0  # the values it has
        monkeypatch.setattr(ro, "load", real_load)
        stored.pop(("knowledge_chunk_size",))
        await tick()
        assert settings.knowledge_chunk_size != 0
    finally:
        keeper.cancel()
        so.adopt({})
