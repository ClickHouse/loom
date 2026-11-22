"""Derived text checks patterns even when each source fact remains eligible."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.core import pg
from server.core.errors import BackendUnavailableError
from server.memory.app import blacklist_guard, period_digest
from server.memory.store import blacklist_policy, lifecycle, pg_memory, pg_publish, summary_sources
from server.memory.store.rows import identity_key
from tests.unit.test_memory_verify import _row
from tests.unit.test_period_digest import _FakeEmbedder, _period
from tests.unit.test_summary_sources import digest


@pytest.fixture
def blacklist(monkeypatch):
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(blacklist_guard, "_ensure_loaded", AsyncMock())
    monkeypatch.setattr(blacklist_guard, "note_hit", AsyncMock())

    def install(pattern, *, regex=False):
        monkeypatch.setattr(
            blacklist_guard,
            "_cache",
            [
                blacklist_guard.BlacklistPattern(
                    "pattern",
                    "o",
                    "ns",
                    pattern,
                    "regex" if regex else "substring",
                    "namespace",
                    "refused",
                    compiled=blacklist_guard.compile_regex(pattern) if regex else None,
                )
            ],
        )

    install("blocked")
    return install


def source_rows(monkeypatch, facts):
    sources = [_row(memory_key=f"source-{n}", value=value) for n, value in enumerate(facts)]
    heads = {identity_key(row): row for row in sources}
    monkeypatch.setattr(summary_sources, "_source_heads", AsyncMock(return_value=heads))
    row = digest(
        sources[0],
        org="o",
        namespace="ns",
        derived_from=[r.memory_key for r in sources],
        key_facts=facts,
        summary_text="\n".join(facts),
    )
    return row


@pytest.mark.parametrize("combined", [False, True])
async def test_current_sources_do_not_admit_blacklisted_digest_text(
    monkeypatch, blacklist, combined
):
    facts = ["north", "star"] if combined else ["blocked fact"]
    if combined:
        blacklist(r"^north\nstar$", regex=True)
    row = source_rows(monkeypatch, facts)
    assert (
        await summary_sources.current_summary_sources(object(), [row], org="o", namespace="ns")
        == []
    )


async def test_blacklisted_digest_is_refused_before_any_publication(monkeypatch, blacklist):
    row = source_rows(monkeypatch, ["blocked fact"])
    ch = SimpleNamespace(insert=AsyncMock(), query_all=AsyncMock(return_value=[]))
    with pytest.raises(HTTPException) as error:
        await summary_sources.insert_verified_summaries(ch, [row])
    assert error.value.status_code == 422 and error.value.detail["code"] == "BLACKLISTED"
    ch.insert.assert_not_awaited()


async def test_unmatched_digest_remains_eligible(monkeypatch, blacklist):
    row = source_rows(monkeypatch, ["allowed fact"])
    assert await summary_sources.current_summary_sources(
        object(), [row], org="o", namespace="ns"
    ) == [row]


async def test_period_blacklist_refusal_precedes_embedding_and_write(monkeypatch, blacklist):
    monkeypatch.setattr(period_digest, "_source_memories", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(period_digest, "_period_writer", AsyncMock(return_value="writer"))
    write = AsyncMock(return_value=True)
    vector = AsyncMock(return_value=[1.0, 0.0])
    monkeypatch.setattr(period_digest, "insert_documented_versions", write)
    monkeypatch.setattr(period_digest, "document_vector", vector)
    llm = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(text="blocked fact")))
    result = period_digest.PeriodDigestResult()
    assert not await period_digest._write_period(
        object(), llm, _FakeEmbedder(), _period(1, 2), result
    )
    assert result.failures
    vector.assert_not_awaited()
    write.assert_not_awaited()


async def test_policy_unavailability_never_serves_a_proven_digest(monkeypatch, blacklist):
    row = source_rows(monkeypatch, ["allowed fact"])
    monkeypatch.setattr(blacklist_guard, "_ensure_loaded", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await summary_sources.current_summary_sources(object(), [row], org="o", namespace="ns")
    monkeypatch.setattr(blacklist_policy, "_policy", None)
    with pytest.raises(BackendUnavailableError, match="not wired"):
        await summary_sources.current_summary_sources(object(), [row], org="o", namespace="ns")


async def test_later_blacklisted_batch_is_checked_before_any_summary_write(monkeypatch, blacklist):
    allowed = source_rows(monkeypatch, ["allowed fact"])
    refused = {**allowed, "key_facts": ["blocked fact"], "summary_text": "blocked fact"}
    ch = SimpleNamespace(insert=AsyncMock(), query_all=AsyncMock(return_value=[]))
    with pytest.raises(HTTPException):
        await summary_sources.insert_verified_summaries(ch, [allowed, refused])
    ch.insert.assert_not_awaited()


async def test_native_policy_change_refuses_summary_before_send(monkeypatch, blacklist):
    row = source_rows(monkeypatch, ["allowed fact"])
    monkeypatch.setattr(pg, "enabled", lambda: True)
    generations = {("", "*"): 1, ("o", "*"): 4}
    monkeypatch.setattr(pg_memory, "policy_generations", AsyncMock(return_value=generations))
    conn = SimpleNamespace(
        execute=AsyncMock(),
        fetch=AsyncMock(
            return_value=[
                {"org": "", "scope": "*", "generation": 1},
                {"org": "o", "scope": "*", "generation": 5},
            ]
        ),
    )

    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(pg, "transaction", transaction)
    monkeypatch.setattr("server.memory.store.refusals.lock_postgres", AsyncMock())
    ch = SimpleNamespace(insert=AsyncMock())
    with pytest.raises(pg_memory.AdmissionContendedError, match="blacklist changed"):
        await summary_sources.insert_verified_summaries(ch, [row])
    assert "pg_advisory_xact_lock_shared" in conn.execute.call_args.args[0]
    ch.insert.assert_not_awaited()


async def test_native_documented_write_keeps_checked_policy(monkeypatch):
    monkeypatch.setattr(pg, "enabled", lambda: True)
    policy = {("", "*"): 1, ("o", "*"): 4}
    commit, publish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(lifecycle, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(pg_publish, "commit", commit)
    monkeypatch.setattr(pg_publish, "publish", publish)
    assert await lifecycle.insert_documented_versions([{}], [{}], ch=object(), policy=policy)
    assert commit.call_args.kwargs["admission"].generations == policy
    publish.assert_awaited_once()
