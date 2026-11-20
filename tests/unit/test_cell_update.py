"""The update control command validates its action before connecting or admitting work."""

from __future__ import annotations

import argparse
import importlib

import pytest

from server.settings import settings


def args(**changes):
    return argparse.Namespace(
        cell="first", wait=0, config=None, finish=False, revert=False, probe_url=None, **changes
    )


@pytest.mark.parametrize("wait", [-1, float("inf"), float("nan"), 86401])
def test_update_refuses_invalid_wait_before_connect(monkeypatch, wait):
    command = importlib.import_module("server.cli.cell_update")
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://configured")
    values = args()
    values.wait = wait
    assert "wait" in command._args_problem(values)


@pytest.mark.parametrize(
    "actions",
    [
        {"config": "config.yaml", "finish": True},
        {"finish": True, "revert": True},
        {"probe_url": "http://probe", "revert": True},
    ],
)
def test_update_refuses_ambiguous_actions_before_connect(monkeypatch, actions):
    command = importlib.import_module("server.cli.cell_update")
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://configured")
    values = args()
    for name, value in actions.items():
        setattr(values, name, value)
    assert "one" in command._args_problem(values)


@pytest.mark.parametrize("action", [None, "config", "finish", "revert"])
def test_scim_cutoffs_require_a_configuration_action(monkeypatch, action):
    command = importlib.import_module("server.cli.cell_update")
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://configured")
    values = args(revoke_unprovisioned=True)
    if action is not None:
        setattr(values, action, "target.yaml" if action == "config" else True)
    result = command._args_problem(values)
    assert result == (
        "--revoke-unprovisioned needs --config, --finish or --revert" if action is None else ""
    )


@pytest.mark.parametrize("refused", [False, True])
def test_control_coordinator_verifies_and_gates_without_becoming_a_participant(
    monkeypatch, refused
):
    from unittest.mock import AsyncMock

    from server.cli import _ch

    events = []
    client = object()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)

    async def connect(ch):
        assert ch is client
        events.append("paired-stores")

    async def acceptance(*, org_free):
        assert org_free.kind == "write"
        events.append("org-free-gate")
        if refused:
            raise _ch.OrgNotHeld("wrong cell")

    async def body(ch):
        assert ch is client
        events.append("body")
        return 7

    async def close(ch):
        assert ch is client
        events.append("disconnect")

    forbidden = AsyncMock(side_effect=AssertionError("update must not enroll"))
    monkeypatch.setattr(_ch, "connect_verified", connect)
    monkeypatch.setattr(_ch, "check_acceptance", acceptance)
    monkeypatch.setattr(_ch, "disconnect", close)
    monkeypatch.setattr("server.insights.app.provider_command.model_command", forbidden)
    if refused:
        with pytest.raises(_ch.OrgNotHeld, match="wrong cell"):
            _ch.run_control_connected(body, org_free=_ch.writes("cell control"))
    else:
        assert _ch.run_control_connected(body, org_free=_ch.writes("cell control")) == 7
    assert events == [
        "paired-stores",
        "org-free-gate",
        *([] if refused else ["body"]),
        "disconnect",
    ]
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure_at", ["acceptance", "body", None])
def test_control_coordinator_preserves_failure_when_disconnect_fails(monkeypatch, failure_at):
    from unittest.mock import AsyncMock

    from server.cli import _ch

    client = object()
    first = ValueError("coordinator refused")
    closing = RuntimeError("disconnect failed")
    closed = []

    async def acceptance(**kwargs):
        if failure_at == "acceptance":
            raise first

    async def body(ch):
        assert ch is client
        if failure_at == "body":
            raise first
        return 7

    async def close(ch):
        closed.append(ch)
        raise closing

    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(_ch, "connect_verified", AsyncMock())
    monkeypatch.setattr(_ch, "check_acceptance", acceptance)
    monkeypatch.setattr(_ch, "disconnect", close)
    expected = first if failure_at is not None else closing
    with pytest.raises(type(expected)) as raised:
        _ch.run_control_connected(body, org_free=_ch.writes("cell control"))
    assert raised.value is expected
    assert closed == [client]
