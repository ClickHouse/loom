"""conversation.append respects the per-namespace logging switch."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_append_is_a_noop_when_logging_disabled(monkeypatch) -> None:
    from server.knowledge.api.models_conversations import ConversationAppendInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    stored = {"v": False}

    async def disabled(org, ns, **k):
        return False

    async def fake_append(*a, **k):
        stored["v"] = True

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", disabled)
    monkeypatch.setattr(conversation_repo, "append", fake_append)

    out = await h.handle_conversation_append(
        ConversationAppendInput(
            org="dev",
            namespace="x",
            agent="a",
            thread_id="t",
            msg_id="m1",
            role="user",
            content="hi",
        )
    )
    assert out.msg_id == "m1"  # call still accepted
    assert stored["v"] is False  # but nothing was written
    # ... and the response says so: a client that plans
    # around the transcript must not be told its turn was kept.
    assert out.stored is False
    assert out.reason == "logging_disabled"


@pytest.mark.asyncio
async def test_append_reports_a_written_turn_and_a_duplicate(monkeypatch) -> None:
    from server.knowledge.api.models_conversations import ConversationAppendInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    async def enabled(org, ns, **k):
        return True

    seen = {"v": False}

    async def exists(*a, **k):
        return seen["v"]

    async def fake_append(*a, **k):
        seen["v"] = True

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", enabled)
    monkeypatch.setattr(conversation_repo, "message_exists", exists)
    monkeypatch.setattr(conversation_repo, "append", fake_append)

    # A system turn takes the no-embed path; it is the shortest real write.
    inp = ConversationAppendInput(
        org="dev",
        namespace="x",
        agent="a",
        thread_id="t",
        msg_id="m2",
        role="system",
        content="reminder",
    )
    first = await h.handle_conversation_append(inp)
    assert first.stored is True and first.reason == ""
    again = await h.handle_conversation_append(inp)
    assert again.stored is True and again.reason == "duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, {"e": None}],
    ids=["no-namespace-row", "null-column"],
)
async def test_logging_gate_fails_closed(monkeypatch, row) -> None:
    """No expressed preference means NOT recording.

    Both branches are "nobody has answered the question": a namespace written to
    before it was ever registered, and a row predating the column. Defaulting
    to True would record transcripts for namespaces whose owner had never
    been shown the Record conversations switch.
    """
    from server.knowledge.store import conversations as conversation_repo

    class _CH:
        async def query_one(self, *a, **k):
            return row

    assert (await conversation_repo.namespace_logging_enabled("dev", "x", ch=_CH())) is False


@pytest.mark.asyncio
async def test_explicit_opt_in_still_records() -> None:
    """The off-by-default rule must not make the Record conversations switch unusable."""
    from server.knowledge.store import conversations as conversation_repo

    class _CH:
        async def query_one(self, *a, **k):
            return {"e": True}

    assert (await conversation_repo.namespace_logging_enabled("dev", "x", ch=_CH())) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("row_present", [False, True], ids=["no-row", "row-exists"])
async def test_set_logging_persists_without_an_existing_row(monkeypatch, row_present) -> None:
    """Turning recording ON must persist, row or no row, as one INSERT.

    With a live row the write is that row copied forward (every column, the
    switch flipped); a mutation would be a no-op where no row exists.
    """
    from server.knowledge.api.models_conversations import ConversationSetLoggingInput
    from server.knowledge.app import conversations as h

    live = {
        "org": "dev",
        "namespace": "ns",
        "owner_email": "a@b",
        "conversation_logging_enabled": False,
    }
    calls: dict[str, object] = {"inserted": None, "executed": False}

    class _CH:
        async def query_one(self, *a, **k):
            return dict(live) if row_present else None

        async def insert(self, table, rows):
            calls["inserted"] = (table, rows)

        async def execute(self, *a, **k):
            calls["executed"] = True

    monkeypatch.setattr(h_ch_module(), "get_client", lambda: _CH())

    out = await h.handle_conversation_set_logging(
        ConversationSetLoggingInput(org="dev", namespace="ns", enabled=True)
    )
    assert out.enabled is True
    assert calls["executed"] is False
    table, rows = calls["inserted"]  # type: ignore[misc]
    assert table == "loom.namespaces"
    assert rows[0]["namespace"] == "ns"
    assert rows[0]["conversation_logging_enabled"] is True
    assert "updated_at" in rows[0]
    if row_present:
        assert rows[0]["owner_email"] == "a@b"  # carried forward, not reset


def h_ch_module():
    """The handler imports `get_client` from server.core.ch at call time."""
    import server.core.ch as ch_mod

    return ch_mod


def test_new_namespace_does_not_start_recording() -> None:
    """Structural: register's no-existing-row branch must resolve to False.

    The handler builds the row inline, so there is no seam to call without a
    live ClickHouse; assert on the source instead.
    """
    import inspect

    from server.identity.app import namespace as ns_handler

    src = inspect.getsource(ns_handler)
    assert 'else (bool(existing["conversation_logging_enabled"]) if existing else False)' in src, (
        "a brand-new namespace must default conversation_logging_enabled to "
        "False — transcript capture is opt-in"
    )
