"""An operator allow-list defers set_from_messages for named
agents; the flag path, the record_schema exclusion and the explicit
defer=false override keep their contract."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.app.extract import handler as X


def _inp(**kw: Any) -> MemorySetFromMessagesInput:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "agent": "supportbot",
        "messages": [{"role": "user", "content": "we moved the standup to 10:30"}],
    }
    base.update(kw)
    return MemorySetFromMessagesInput(**base)


def _set(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "extraction_defer_agents", value)


def test_empty_list_defers_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "")
    assert X._deferred_by_allowlist(_inp()) is False


def test_named_agent_is_deferred_and_others_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "supportbot, lme-bench")
    assert X._deferred_by_allowlist(_inp()) is True
    assert X._deferred_by_allowlist(_inp(agent="lme-bench")) is True
    assert X._deferred_by_allowlist(_inp(agent="cursor")) is False


def test_record_schema_callers_are_never_deferred_by_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, "supportbot")
    inp = _inp(record_schema={"pr_number": "int"}, memory_key="pr_123")
    assert X._deferred_by_allowlist(inp) is False


def test_an_explicit_defer_false_wins_over_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "supportbot")
    assert X._deferred_by_allowlist(_inp(defer=False)) is False


def test_star_means_service_accounts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, "*")
    monkeypatch.setattr("server.auth.context.current_auth", lambda: None)
    assert X._deferred_by_allowlist(_inp()) is False

    class _Auth:
        is_service_account = True

    monkeypatch.setattr("server.auth.context.current_auth", lambda: _Auth())
    assert X._deferred_by_allowlist(_inp()) is True


@pytest.mark.asyncio
async def test_handler_takes_the_queue_path_for_an_allowlisted_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, "supportbot")
    seen: list[Any] = []

    async def _enqueue(inp: Any, scope: Any = None) -> str:
        seen.append(inp)
        return "q-1"

    monkeypatch.setattr("server.memory.app.extraction_queue.enqueue", _enqueue)
    out = await X.handle_memory_set_from_messages(_inp())
    assert out.deferred is True and out.queue_id == "q-1" and out.deferred_by == "allowlist"
    assert seen and seen[0].agent == "supportbot"
    # the flag path names itself too
    out2 = await X.handle_memory_set_from_messages(_inp(agent="cursor", defer=True))
    assert out2.deferred_by == "flag"
