"""Private calls leave mentor advice and its deferred profile work untouched."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from server.core import diagnostics, request_work
from server.insights.app import mentor
from server.settings import settings


@pytest.fixture
def world(monkeypatch):
    inspected, warmed, evaluated = [], [], []

    class Result:
        def model_dump(self):
            inspected.append("result")
            return {"content": "PRIVATE-CONTENT"}

    def rule(op, arguments, result, facts, state):
        evaluated.append((op, arguments, result))
        return "test-advice", "Use the ordinary advice"

    monkeypatch.setattr(settings, "mentor_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "mentor_max_hints_per_session", 10)
    monkeypatch.setattr(mentor, "_cache", {})
    monkeypatch.setattr(mentor, "_sessions", {})
    monkeypatch.setattr(mentor, "_profile_shown", {})
    monkeypatch.setattr(mentor, "CALL_RULES", (rule,))
    monkeypatch.setattr(mentor, "warm_profile", lambda *identity: warmed.append(identity))

    def call():
        return mentor.call_hint(
            "knowledge.get",
            {"document_id": "PRIVATE-ID"},
            Result(),
            session_key="session-a",
            org="org-a",
            namespace="team-a",
            agent="reader",
            clock=lambda: 100.0,
        )

    return SimpleNamespace(call=call, inspected=inspected, warmed=warmed, evaluated=evaluated)


@contextmanager
def privacy(kind):
    if kind == "private":
        with diagnostics.private_work():
            yield
    elif kind == "http_owner":
        with request_work.http_owner({"type": "http"}):
            request_work.begin()
            assert request_work.mark_mirrored()
            request_work.begin()
            assert not request_work.mirror_marked() and request_work.http_mirror_marked()
            yield
    else:
        yield


@pytest.mark.parametrize("kind", ["ordinary", "private", "http_owner"])
@pytest.mark.parametrize("cached", [False, True])
def test_mentor_never_inspects_or_warms_private_request_advice(world, kind, cached):
    if cached:
        profile = SimpleNamespace(facts={}, hints=(), hint=lambda code: None)
        mentor._cache[("org-a", "team-a", "reader")] = (1000.0, profile)
    original_cache = dict(mentor._cache)

    def exercise():
        with privacy(kind):
            result = world.call()
        if kind == "ordinary":
            assert result == ("test-advice", "Use the ordinary advice")
            assert world.inspected == ["result"] and len(world.evaluated) == 1
            assert world.warmed == ([] if cached else [("org-a", "team-a", "reader")])
            assert mentor._sessions["session-a"]["shown"] == {"test-advice"}
            assert mentor._sessions["session-a"]["count"] == 1
        else:
            assert result is None
            assert world.inspected == world.evaluated == world.warmed == []
            assert mentor._sessions == mentor._profile_shown == {}
        assert mentor._cache == original_cache

    contextvars.copy_context().run(exercise)


def test_unavailable_privacy_decision_drops_advice_before_any_state_change(monkeypatch, world):
    def unavailable():
        raise RuntimeError("private classification unavailable")

    monkeypatch.setattr(request_work, "http_mirror_marked", unavailable)
    assert world.call() is None
    assert world.inspected == world.evaluated == world.warmed == []
    assert mentor._cache == mentor._sessions == mentor._profile_shown == {}
