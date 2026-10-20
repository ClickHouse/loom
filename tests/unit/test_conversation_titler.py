"""Unit tests for the conversation-titler worker — mocked CH/LLM/insert."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from server.knowledge.workers.conversation_titler import run_pass
from tests.unit._fake_credentials import OPENAI_KEY


class StubCH:
    def __init__(self, candidates, turns, title_row) -> None:
        self._candidates = candidates
        self._turns = turns
        self._title_row = title_row

        self.calls: list[str] = []
        self.params: list[dict | None] = []

    async def query_all(self, sql, params=None):
        # The candidate scan groups by c.org; the thread rows are one keyed
        # read; the per-thread turn fetch is the rest.
        self.calls.append(sql)
        self.params.append(params)
        if "GROUP BY c.org" in sql:
            return self._candidates
        if "IN {keys:Array(Tuple(String, String, String))}" in sql:
            return [self._title_row] if self._title_row else []
        return self._turns

    async def query_one(self, sql, params=None):
        raise AssertionError("no per-thread point read")


class _LLMOut:
    text = "ClickHouse engine for an event log"


class StubLLM:
    async def complete(self, system, user, max_tokens):
        return _LLMOut()


def _patch(monkeypatch, ch):
    import server.core.ch as chmod
    import server.llm.client as llmmod

    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    monkeypatch.setattr(llmmod, "get_llm_client", lambda: StubLLM())


_CAND = {
    "org": "dev",
    "namespace": "ns",
    "thread_id": "t1",
    "agent": "claude-code",
    "n": 5,
    "created_at": None,
}
_TURNS = [
    {"role": "user", "content": "what engine?"},
    {"role": "assistant", "content": "MergeTree"},
]


@pytest.mark.asyncio
async def test_titles_an_untitled_thread(monkeypatch) -> None:
    ch = StubCH([_CAND], _TURNS, None)  # append-only thread: no thread row
    _patch(monkeypatch, ch)
    writes = []
    import server.knowledge.store.conversations as repo

    async def fake_insert(row, **k):
        writes.append(row)

    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    res = await run_pass()
    assert res.titled == 1
    assert writes[0]["thread_id"] == "t1"
    assert writes[0]["title"] == "ClickHouse engine for an event log"


@pytest.mark.asyncio
async def test_skips_already_titled_threads(monkeypatch) -> None:
    ch = StubCH([_CAND], _TURNS, _stored_row(title="Already named"))
    _patch(monkeypatch, ch)
    import server.knowledge.store.conversations as repo

    async def fake_insert(row, **k):
        raise AssertionError("should not write a title for an already-titled thread")

    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    res = await run_pass()
    assert res.titled == 0
    assert res.skipped_titled == 1


def _stored_row(**over):
    row = {
        "org": "dev",
        "namespace": "ns",
        "agent": "created-by-agent",
        "thread_id": "t1",
        "title": "",
        "metadata": '{"source": "ide"}',
        "run_id": "run-42",
        "user_id": "u-7",
        "created_at": datetime(2026, 1, 15, 9, 30, 0),
        "updated_at": datetime(2026, 1, 15, 9, 30, 0),
        "tombstoned_at": None,
    }
    row.update(over)
    return row


def _capture(monkeypatch):
    import server.knowledge.store.conversations as repo

    writes = []

    async def fake_insert(row, **k):
        writes.append(row)

    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    return writes


@pytest.mark.asyncio
async def test_does_not_resurrect_a_deleted_thread(monkeypatch) -> None:
    # A tombstoned, untitled thread row: writing a title version (newer
    # updated_at, tombstoned_at unset) would un-delete it under FINAL.
    ch = StubCH([_CAND], _TURNS, _stored_row(tombstoned_at=datetime(2026, 2, 1)))
    _patch(monkeypatch, ch)
    writes = _capture(monkeypatch)
    res = await run_pass()
    assert writes == []
    assert res.titled == 0
    assert res.skipped_deleted == 1


@pytest.mark.asyncio
async def test_title_write_carries_every_stored_column(monkeypatch) -> None:
    stored = _stored_row()
    ch = StubCH([_CAND], _TURNS, stored)
    _patch(monkeypatch, ch)
    writes = _capture(monkeypatch)
    res = await run_pass()
    assert res.titled == 1
    (w,) = writes
    assert w["title"] == "ClickHouse engine for an event log"
    assert w["updated_at"] > stored["updated_at"]
    for col in (
        "org",
        "namespace",
        "agent",
        "thread_id",
        "metadata",
        "run_id",
        "user_id",
        "created_at",
        "tombstoned_at",
    ):
        assert w[col] == stored[col], col


@pytest.mark.asyncio
async def test_the_scan_excludes_titled_and_deleted_threads_before_its_limit(
    monkeypatch,
) -> None:
    """Skipped after the LIMIT, the recent titled threads filled it every pass
    and an older untitled thread was never reached."""
    ch = StubCH([], _TURNS, None)
    _patch(monkeypatch, ch)
    await run_pass()
    (scan,) = ch.calls
    excl = scan.index("NOT IN (")
    assert excl < scan.index("GROUP BY") < scan.index("LIMIT {lim:UInt32}")
    assert "tombstoned_at IS NOT NULL OR title != ''" in scan


@pytest.mark.asyncio
async def test_a_credential_turn_never_reaches_the_title_model(monkeypatch) -> None:
    turns = [{"role": "user", "content": f"my key is {OPENAI_KEY}"}, *_TURNS]
    ch = StubCH([_CAND], turns, None)
    _patch(monkeypatch, ch)
    prompts: list[str] = []

    class _Recording:
        async def complete(self, system, user, max_tokens):
            prompts.append(user)
            return _LLMOut()

    import server.knowledge.store.conversations as repo
    import server.llm.client as llmmod

    async def fake_insert(row, **k):
        return None

    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _Recording())
    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    res = await run_pass()
    assert res.titled == 1
    assert prompts and OPENAI_KEY not in prompts[0] and "what engine?" in prompts[0]


@pytest.mark.asyncio
async def test_a_title_that_reads_as_a_credential_is_not_stored_or_asked_again(
    monkeypatch,
) -> None:
    """The thread is marked instead: neither the scan nor the pass's re-check picks it
    again, so a second pass makes no model call."""
    ch = StubCH([_CAND], _TURNS, None)
    _patch(monkeypatch, ch)
    prompts: list[str] = []

    class _KeyTitle:
        text = f"Deploy key {OPENAI_KEY}"

    class _KeyLLM:
        async def complete(self, system, user, max_tokens):
            prompts.append(user)
            return _KeyTitle()

    import server.knowledge.store.conversations as repo
    import server.llm.client as llmmod

    writes = []

    async def fake_insert(row, **k):
        writes.append(row)

    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _KeyLLM())
    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    res = await run_pass()
    assert res.titled == 0 and len(prompts) == 1
    [written] = writes
    assert written["title"] == "" and json.loads(written["metadata"]) == {"title_refused": True}
    assert "JSONExtractBool(metadata, {refused:String})" in ch.calls[0]
    assert ch.params[0]["refused"] == "title_refused"

    # A scan on a merge state behind the write still returns the thread.
    _patch(monkeypatch, StubCH([_CAND], _TURNS, written))
    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _KeyLLM())
    res = await run_pass()
    assert (res.titled, res.skipped_titled, len(prompts), len(writes)) == (0, 1, 1, 1)


@pytest.mark.asyncio
async def test_a_thread_whose_turns_all_hold_a_key_is_marked_and_skipped(monkeypatch) -> None:
    """Left unmarked, such a thread took a scan slot on every pass and starved older
    untitled threads."""
    turns = [{"role": "user", "content": f"my key is {OPENAI_KEY}"}] * 8
    ch = StubCH([_CAND], turns, None)
    _patch(monkeypatch, ch)
    prompts: list[str] = []

    class _Recording:
        async def complete(self, system, user, max_tokens):
            prompts.append(user)
            return _LLMOut()

    import server.knowledge.store.conversations as repo
    import server.llm.client as llmmod

    writes = []

    async def fake_insert(row, **k):
        writes.append(row)

    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _Recording())
    monkeypatch.setattr(repo, "insert_thread", fake_insert)
    res = await run_pass()
    assert (res.titled, prompts) == (0, [])
    [written] = writes
    assert written["title"] == "" and json.loads(written["metadata"]) == {"title_refused": True}

    _patch(monkeypatch, StubCH([_CAND], turns, written))
    monkeypatch.setattr(llmmod, "get_llm_client", lambda: _Recording())
    res = await run_pass()
    assert (res.skipped_titled, prompts, len(writes)) == (1, [], 1)


@pytest.mark.asyncio
async def test_a_thread_with_fewer_opening_turns_stays_unmarked_until_a_plain_turn_arrives(
    monkeypatch,
) -> None:
    """The opening window is not full yet, so a later plain turn can still title it."""
    turns = [{"role": "user", "content": f"my key is {OPENAI_KEY}"}]
    _patch(monkeypatch, StubCH([_CAND], turns, None))
    writes = _capture(monkeypatch)
    res = await run_pass()
    assert (res.titled, writes) == (0, [])

    _patch(monkeypatch, StubCH([_CAND], [*turns, *_TURNS], None))
    res = await run_pass()
    assert res.titled == 1
    [written] = writes
    assert written["title"] == "ClickHouse engine for an event log"
    assert json.loads(written["metadata"]) == {}
