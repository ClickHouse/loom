"""A live setting takes a deployment-wide override stored in loom.runtime_overrides:
the writing process applies it at once, every other one on its next refresh, and
clearing it puts back the value each process started with."""

from __future__ import annotations

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
    monkeypatch.setattr(settings, LIVE, settings.__class__.model_fields[LIVE].default)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, ...], Any]:
    """loom.runtime_overrides as `put` and `clear` leave it: key -> value."""
    rows: dict[tuple[str, ...], Any] = {}

    async def put(kind: str, key: tuple[str, ...], value: Any, *, ch: Any = None) -> None:
        assert kind == so.KIND
        rows[key] = value

    async def clear(kind: str, key: tuple[str, ...], *, ch: Any = None) -> None:
        rows.pop(key, None)

    monkeypatch.setattr(ro, "put", put)
    monkeypatch.setattr(ro, "clear", clear)
    return rows


async def test_put_stores_the_validated_value_and_applies_it_here(stored: dict) -> None:
    assert await so.put(LIVE, "0.5") == 0.5
    assert stored == {(LIVE,): 0.5}
    assert settings.memory_ask_recency_weight == 0.5
    assert so.row(LIVE)["source"] == "override"
    await so.clear(LIVE)
    assert stored == {} and settings.memory_ask_recency_weight == 0.2
    assert so.row(LIVE)["source"] == "default"


async def test_a_setting_read_at_startup_takes_no_override(stored: dict) -> None:
    with pytest.raises(ValidationError, match="read once at startup"):
        await so.put("ch_host", "elsewhere")
    with pytest.raises(ValidationError, match="no setting is named"):
        await so.put("no_such_setting", 1)
    assert stored == {}


async def test_a_value_the_field_refuses_is_not_stored(stored: dict) -> None:
    with pytest.raises(ValidationError, match="memory_ask_synth_max_tokens_ceiling"):
        await so.put("memory_ask_synth_max_tokens_ceiling", 0)  # ge=1
    with pytest.raises(ValidationError):
        await so.put("memory_search_default_mode", "nonsense")
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
    env_file.write_text("LOOM_MEMORY_ASK_SYNTH_BUDGET_RATIO=0.5\nLOOM_CH_HOST=from-file\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(env_file))
    monkeypatch.setenv("LOOM_CH_HOST", "from-env")
    by_name = {r["name"]: r for r in so.rows()}
    assert by_name["ch_host"]["source"] == "env"  # the environment wins, as at startup
    assert by_name["memory_ask_synth_budget_ratio"]["source"] == "env_file"
    assert by_name[LIVE]["source"] == "default" and by_name[LIVE]["live"] is True
    assert by_name["ch_host"]["live"] is False
    assert by_name[LIVE]["description"].startswith("memory.ask only: re-rank")
    assert by_name[LIVE]["env"] == "LOOM_MEMORY_ASK_RECENCY_WEIGHT"


def test_a_secret_shows_only_its_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "s" * 40)
    row = so.row("jwt_secret")
    assert row["value"] == "<masked, 40 chars>" and "s" * 8 not in str(row)


def test_changed_keeps_the_settings_off_their_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, LIVE, 0.6)
    names = {r["name"] for r in so.rows(prefix="memory_ask_", changed=True)}
    assert LIVE in names and "memory_ask_synth_budget_ratio" not in names


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, stored: dict) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", False)
    audits: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, **kw: Any) -> str:
        audits.append((event, kw))
        return ""

    monkeypatch.setattr("server.observability.audit.emit", emit)
    from server.insights.app.admin_routes import admin_router

    app = FastAPI()
    register_exception_handler(app)
    app.include_router(admin_router)
    c = TestClient(app)
    c.audits = audits  # type: ignore[attr-defined]
    return c


def test_the_admin_routes_set_list_and_reset(client: TestClient, stored: dict) -> None:
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "value": 0.45})
    assert r.status_code == 200, r.text
    assert r.json()["setting"]["value"] == 0.45 and r.json()["setting"]["source"] == "override"
    assert stored == {(LIVE,): 0.45}
    listed = client.post("/v1/admin/settings.list", json={"prefix": LIVE}).json()["settings"]
    assert [s["value"] for s in listed if s["name"] == LIVE] == [0.45]
    r = client.post("/v1/admin/settings.set", json={"name": LIVE, "clear": True})
    assert r.json()["setting"] == {**r.json()["setting"], "value": 0.2, "source": "default"}
    assert stored == {}
    events = [(e, kw["subject"]) for e, kw in client.audits]  # type: ignore[attr-defined]
    assert events == [
        ("setting.changed", {"name": LIVE, "value": 0.45, "previous": 0.2, "cleared": False}),
        ("setting.changed", {"name": LIVE, "value": 0.2, "previous": 0.45, "cleared": True}),
    ]


def test_the_admin_route_refuses_what_it_cannot_store(client: TestClient) -> None:
    r = client.post("/v1/admin/settings.set", json={"name": "ch_host", "value": "x"})
    assert r.status_code == 422 and "read once at startup" in r.text
    r = client.post("/v1/admin/settings.set", json={"name": LIVE})
    assert r.status_code == 422  # neither a value nor clear
