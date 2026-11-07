"""entity_extractor: a garbled LLM reply is NOT "no entities".

`_parse_entities` returns None on parse failure, distinct from a well-formed
empty array: an [] would make the worker write the permanent `_none_`
sentinel and drop the row from the `empty(entities)` rescan on the strength
of one bad reply. The row is left for the next pass, and only after
`memory_entity_extract_parse_fail_max` consecutive failures is the DISTINCT `_parse_failed_`
sentinel written so the row stops costing budget.

CH-free: the repo functions are stubbed.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.workers import entity_extractor as ee
from server.settings import settings


def test_parse_entities_distinguishes_failure_from_empty() -> None:
    assert ee._parse_entities("[]") == []
    assert ee._parse_entities('["Alice", "clickhouse", "alice"]') == ["alice", "clickhouse"]
    # No JSON array at all -> parse failure, not "none".
    assert ee._parse_entities("Sure! The entities are Alice and Bob.") is None
    assert ee._parse_entities("") is None


class _Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.applied: list[dict[str, Any]] = []

    async def find_unentitied_memories(self, **kwargs):
        return list(self.rows)

    async def apply_entities_batch(self, items, **kwargs):
        # The worker writes the pass's rows in one batch.
        self.applied.extend(dict(it) for it in items)
        return {"memory": len(items), "memory_embeddings": len(items)}


def _row(key: str = "k1") -> dict[str, Any]:
    return {
        "org": "dev",
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": "User works with Alice at ClickHouse on the staging cluster.",
        "version": 1,
        "content_sha256": "",
    }


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    from server.memory import store as memory_repo

    repo = _Repo([_row()])
    monkeypatch.setattr(memory_repo, "find_unentitied_memories", repo.find_unentitied_memories)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", repo.apply_entities_batch)

    async def _under_cap(pairs):
        return dict.fromkeys(pairs, False)

    monkeypatch.setattr(ee, "namespaces_over_cap", _under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    ee._parse_fail_streaks.clear()
    return repo


@pytest.mark.asyncio
async def test_parse_failure_leaves_row_and_does_not_write_none(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    async def _garbled(value_str: str):
        return None

    monkeypatch.setattr(ee, "_extract", _garbled)
    result = await ee.run_pass(ee.EntityExtractorConfig())
    assert result.parse_failures == 1
    assert result.parse_poisoned == 0
    assert result.extracted == []
    # The row was NOT touched — it stays in the empty(entities) rescan.
    assert wired.applied == []


@pytest.mark.asyncio
async def test_parse_failure_streak_writes_distinct_sentinel(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    async def _garbled(value_str: str):
        return None

    monkeypatch.setattr(ee, "_extract", _garbled)
    for _ in range(settings.memory_entity_extract_parse_fail_max - 1):
        await ee.run_pass(ee.EntityExtractorConfig())
    assert wired.applied == []
    result = await ee.run_pass(ee.EntityExtractorConfig())
    assert result.parse_poisoned == 1
    assert len(wired.applied) == 1
    assert wired.applied[0]["entities"] == [ee._PARSE_FAILED_SENTINEL]
    assert wired.applied[0]["entities"] != [ee._NONE_SENTINEL]
    # Streak is cleared once the sentinel is written.
    assert ee._parse_fail_streaks == {}


@pytest.mark.asyncio
async def test_genuinely_empty_reply_still_writes_none_sentinel(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    async def _empty(value_str: str):
        return []

    monkeypatch.setattr(ee, "_extract", _empty)
    result = await ee.run_pass(ee.EntityExtractorConfig())
    assert result.parse_failures == 0
    assert wired.applied[0]["entities"] == [ee._NONE_SENTINEL]


@pytest.mark.asyncio
async def test_outcome_reports_skipped_no_value_and_parse_failures(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    from server.workers import status as worker_status

    blank = _row("blank")
    blank["value"] = "   "
    wired.rows.append(blank)

    async def _garbled(value_str: str):
        return None

    monkeypatch.setattr(ee, "_extract", _garbled)
    outcomes: list[dict[str, Any]] = []

    class _Reg:
        def record_success(self, name, *, duration_ms, outcome):
            outcomes.append(outcome)

        def record_failure(self, name, *, duration_ms, error):
            raise AssertionError(error)

    monkeypatch.setattr(worker_status, "get_registry", lambda: _Reg())
    await ee.run_pass(ee.EntityExtractorConfig())
    assert outcomes and outcomes[0]["skipped_no_value"] == 1
    assert outcomes[0]["parse_failures"] == 1


@pytest.mark.asyncio
async def test_a_failed_pass_raises_to_the_supervisor(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    """A pass that cannot read its candidates is a failed pass: recorded as a
    failure and raised, so the supervisor backs off and counts the streak,
    not returned as a success with the previous pass's outcome."""
    from server.workers.status import get_registry

    async def _down(cfg: Any) -> Any:
        raise RuntimeError("ch down")

    monkeypatch.setattr(ee, "_next_candidates", _down)
    get_registry().reset()
    with pytest.raises(RuntimeError, match="ch down"):
        await ee.run_pass(ee.EntityExtractorConfig())
    snap = get_registry().snapshot()["entity_extractor"]
    assert snap["last_error"] == "RuntimeError: ch down"


@pytest.mark.asyncio
async def test_a_failed_heal_makes_the_pass_partial(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    from server.memory import store as memory_repo
    from server.workers import pass_context

    async def _empty(value_str: str):
        return []

    async def _heal_down(**kwargs: Any) -> int:
        raise RuntimeError("join timed out")

    monkeypatch.setattr(ee, "_extract", _empty)
    monkeypatch.setattr(ee, "_heal_due", lambda: True)
    monkeypatch.setattr(memory_repo, "heal_divergent_entities", _heal_down)
    ctx = pass_context.begin("entity_extractor")
    try:
        await ee.run_pass(ee.EntityExtractorConfig())
    finally:
        pass_context.clear()
    assert ("heal", "RuntimeError") in [f[:2] for f in ctx.failures]
    assert wired.applied  # the extraction itself still landed


@pytest.mark.asyncio
async def test_an_unlabelled_credential_row_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    # copy_to or an import leaves the row unlabelled until the sweep; a token on its own line
    # (JSON-escaped, it follows `\n`) still keeps the value from the extractor's model.
    import json

    token = "ghp_" + "a" * 36
    wired.rows = [
        {**_row("as-object"), "value": json.dumps({"text": "the token:\n" + token})},
        # memory.set stores a string value as a quoted JSON literal
        {**_row("as-string"), "value": json.dumps("the token:\n" + token)},
    ]
    sent: list[str] = []

    async def _model(value_str: str):
        sent.append(value_str)
        return ["thing"]

    monkeypatch.setattr(ee, "_extract", _model)
    result = await ee.run_pass(ee.EntityExtractorConfig())
    assert sent == [] and result.skipped_sensitive == 2
    # the sentinel keeps the next pass from picking the row up again
    assert [a["entities"] for a in wired.applied] == [[ee._NONE_SENTINEL]] * 2


@pytest.mark.asyncio
async def test_a_value_the_authority_no_longer_serves_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, wired: _Repo
) -> None:
    """Under Postgres a candidate ClickHouse still shows, whose head was revised, deleted
    or redacted in the authority, is left out before the model call."""
    from datetime import UTC, datetime

    from server.core import pg
    from server.memory.store import pg_memory

    wired.rows = [_row("k1") | {"redacted_at": None}, _row("k2") | {"redacted_at": None}]
    heads = {
        ("dev", "ns", "a", "u", "-", "k1"): {"version": 1, "redacted_at": None},
        ("dev", "ns", "a", "u", "-", "k2"): {"version": 1, "redacted_at": datetime.now(UTC)},
    }

    async def heads_by_keys(keys: Any) -> dict[Any, Any]:
        return {k: heads[k] for k in keys if k in heads}

    sent: list[str] = []

    async def _extract(value_str: str):
        sent.append(value_str)
        return ["alice"]

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(ee, "_extract", _extract)
    await ee.run_pass(ee.EntityExtractorConfig())
    assert len(sent) == 1
    assert [it["memory_key"] for it in wired.applied] == ["k1"]
