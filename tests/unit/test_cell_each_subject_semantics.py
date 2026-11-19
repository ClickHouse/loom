"""The deployment CLI revokes one exact subject across orgs through shared steps."""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize("subject", ["sa:worker", "person@example.com"])
def test_ordinary_cli_global_subject_is_recorded_and_finished_without_org_expansion(
    subject: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import subject_revocation
    from server.cli import access

    calls: list[tuple[str, str, dict[str, Any]]] = []
    governing = {"reason": "retire", "issued_before": None, "expires_at": None}

    async def record(value: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        calls.append(("record", value, kwargs))
        return governing, False

    async def finish(value: str, row: Any, kept: bool, **kwargs: Any) -> Any:
        assert row is governing and kept is False
        calls.append(("finish", value, kwargs))
        return SimpleNamespace(row=row, kept=kept, sessions=0, tokens=0)

    def no_org_expansion(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the global command must not use org alias expansion")

    monkeypatch.setattr(subject_revocation, "record", record)
    monkeypatch.setattr(subject_revocation, "finish", finish)
    monkeypatch.setattr(subject_revocation, "revoke_in_org", no_org_expansion)
    monkeypatch.setattr(access, "run_connected", lambda work, **kwargs: asyncio.run(work(None)))
    monkeypatch.setattr(access, "_print_revoked", lambda *args, **kwargs: None)
    parser = argparse.ArgumentParser()
    access._add_revoke_token_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["revoke-token", "--subject", subject, "--reason", "retire"])
    assert access._cmd_revoke_token(args) == 0
    assert [name for name, _, _ in calls] == ["record", "finish"]
    assert all(value == subject for _, value, _ in calls)
    assert calls[0][2]["reason"] == "retire"
    assert calls[1][2]["org"] == "-"
    assert calls[0][2]["by"] == calls[1][2]["by"]
    assert calls[0][2]["by"].startswith("cli:")


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["closed", "unavailable"])
async def test_bounded_subject_completion_requires_connected_account_cleanup(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    import time

    from fastapi import HTTPException

    from server.auth import (
        account_lifecycle,
        oauth_store,
        revocation,
        service_accounts,
        subject_revocation,
    )
    from server.insights.app import cell_admin_security_actions as actions

    subject = "sa:retired"
    events = []
    recorded = []
    audits = []
    row = {"jti": subject, "issued_before": None}

    async def record(value, *, progress, **kwargs):
        assert value == subject
        recorded.append(dict(row))
        progress["recorded"] = True
        events.append("recorded")
        return row, False

    async def cutoffs(subjects, **kwargs):
        assert subjects == [subject]
        assert kwargs["deadline"] > time.monotonic()
        return {subject: None}

    async def cleanup(subjects, *, respect_cutoff):
        assert recorded == [row]
        assert subjects == [subject] and respect_cutoff is True
        events.append("cleanup")
        if cleanup_fails:
            raise ConnectionError("connected-account cleanup unavailable")
        events.append("closed")

    async def families(value, **kwargs):
        assert value == subject and "closed" in events
        events.append("sessions")
        return {"family": 0}

    async def revoke_created(value, **kwargs):
        assert value == subject and "closed" in events
        events.append("tokens")
        return [{"name": "retired-child"}]

    async def audit(*args, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(actions, "current_auth", lambda: SimpleNamespace(subject="operator"))
    monkeypatch.setattr(subject_revocation, "record", record)
    monkeypatch.setattr(revocation, "cutoffs_bounded", cutoffs)
    monkeypatch.setattr(revocation, "is_revoked", lambda _family: False)
    monkeypatch.setattr(account_lifecycle, "stop_connections", cleanup)
    monkeypatch.setattr(oauth_store, "session_families", families)
    monkeypatch.setattr(service_accounts, "revoke_created_by_bounded", revoke_created)
    monkeypatch.setattr(subject_revocation, "audit_revocation", audit)
    command = SimpleNamespace(subject=subject, reason="retire")
    if cleanup_fails:
        with pytest.raises(HTTPException) as caught:
            await actions.subject_revoke(command, deadline=time.monotonic() + 30, ch=object())
        assert caught.value.status_code == 503
        assert caught.value.detail == {
            "code": "SUBJECT_REVOCATION_INCOMPLETE",
            "recorded": True,
            "sessions_at_least": 0,
            "tokens_at_least": 0,
        }
        assert events == ["recorded", "cleanup"]
    else:
        result = await actions.subject_revoke(command, deadline=time.monotonic() + 30, ch=object())
        assert result["recorded"] is True and result["outright"] is True
        assert result["sessions"] == result["tokens"] == 1
        assert events == ["recorded", "cleanup", "closed", "sessions", "tokens"]
    assert recorded == [row]
    assert len(audits) == 1
