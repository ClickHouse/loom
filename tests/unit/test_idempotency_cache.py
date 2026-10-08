"""The idempotency cache replays only to the caller who caused the answer.

`server.core.idempotency` covers every branch: the row key is scoped to (org,
subject, operation, client key); a stored answer replays only under that
exact scope and body; a changed body under the same key is a conflict; an
expired row is a miss; and the cache fails open. A read or write that
raises is a miss or a no-op, so a retry re-runs the handler rather than
failing the call. There is no in-flight state: two racing first calls both
miss and both run, and the rows collapse to one (module docstring).

The table is faked as a dict with the row semantics `check` reads.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

import server.core.idempotency as idem


class _Table:
    """`loom.idempotency` as the client sees it: one row per key, last write
    wins (ReplacingMergeTree under FINAL)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail_reads = False
        self.fail_writes = False

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        assert "FINAL" in sql and "idempotency_key = {k:String}" in sql
        if self.fail_reads:
            raise ConnectionError("replica down")
        return self.rows.get(params["k"])

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        assert table == "loom.idempotency"
        if self.fail_writes:
            raise ConnectionError("replica down")
        for r in rows:
            self.rows[r["idempotency_key"]] = dict(r)


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> _Table:
    t = _Table()
    monkeypatch.setattr(idem, "get_client", lambda: t)
    return t


def _key(org: str = "acme", subject: str = "alice", op: str = "memory.set", k: str = "k1") -> str:
    return idem.scoped_key(k, org=org, subject=subject, operation=op)


BODY = {"org": "acme", "namespace": "team", "memory_key": "a", "value": "x"}


async def _store(
    key: str,
    body: dict[str, Any] = BODY,
    response: Any = None,
    *,
    org: str = "acme",
    subject: str = "alice",
) -> None:
    await idem.store(
        key,
        idem.hash_body(body),
        "memory.set",
        200,
        {"version": 1} if response is None else response,
        {"X-Request-Id": "r1"},
        org=org,
        subject=subject,
    )


# ------------------------------------------------------------------ scoping


def test_the_row_key_differs_for_every_scope_component() -> None:
    keys = {
        _key(),
        _key(org="rival"),
        _key(subject="bob"),
        _key(op="memory.delete"),
        _key(k="k2"),
    }
    assert len(keys) == 5
    assert all(len(k) == 64 for k in keys)  # fixed-width ORDER BY column


def test_the_key_components_cannot_be_shifted_into_each_other() -> None:
    assert _key(org="a\x1fb", subject="c") != _key(org="a", subject="b\x1fc")


async def test_the_same_caller_and_body_replay_the_stored_answer(table: _Table) -> None:
    await _store(_key())
    got = await idem.check(_key(), idem.hash_body(BODY))
    assert got.conflict is False and got.hit is not None
    assert (got.hit.status_code, got.hit.body, got.hit.headers) == (
        200,
        {"version": 1},
        {"X-Request-Id": "r1"},
    )


@pytest.mark.parametrize(
    "other",
    [
        {"org": "rival"},
        {"subject": "bob"},
        {"op": "memory.delete"},
        {"k": "k2"},
    ],
    ids=["another org", "another subject", "another op", "another client key"],
)
async def test_nobody_else_is_replayed_another_callers_answer(
    table: _Table, other: dict[str, str]
) -> None:
    await _store(_key())
    got = await idem.check(_key(**other), idem.hash_body(BODY))
    assert got == idem.CacheResult(hit=None, conflict=False)


async def test_the_same_key_for_another_namespace_is_a_conflict_not_a_replay(
    table: _Table,
) -> None:
    """The namespace is in the body, not the key: reusing a key for a call
    into another namespace is refused, never answered with the first call's
    result."""
    await _store(_key())
    got = await idem.check(_key(), idem.hash_body({**BODY, "namespace": "other"}))
    assert got == idem.CacheResult(hit=None, conflict=True)


async def test_a_different_body_under_the_same_key_is_a_conflict(table: _Table) -> None:
    await _store(_key())
    got = await idem.check(_key(), idem.hash_body({**BODY, "value": "y"}))
    assert got.hit is None and got.conflict is True


# ------------------------------------------------------------------ lifetime


async def test_an_expired_row_is_a_miss_even_with_a_conflicting_body(table: _Table) -> None:
    await _store(_key())
    table.rows[_key()]["expires_at"] = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    assert await idem.check(_key(), idem.hash_body(BODY)) == idem.CacheResult(None, False)
    assert await idem.check(_key(), idem.hash_body({"other": 1})) == idem.CacheResult(None, False)


async def test_a_stored_row_lives_for_the_ttl(table: _Table) -> None:
    before = datetime.now(UTC).replace(tzinfo=None)
    await _store(_key())
    row = table.rows[_key()]
    assert row["expires_at"] - row["created_at"] == idem.TTL
    assert before <= row["created_at"] <= datetime.now(UTC).replace(tzinfo=None)


async def test_an_unreadable_expiry_does_not_expire_the_row(table: _Table) -> None:
    await _store(_key())
    table.rows[_key()]["expires_at"] = None
    assert (await idem.check(_key(), idem.hash_body(BODY))).hit is not None


async def test_a_miss_on_an_unknown_key(table: _Table) -> None:
    assert await idem.check(_key(), idem.hash_body(BODY)) == idem.CacheResult(None, False)


# ------------------------------------------------------------------ stored shapes


async def test_bytes_columns_and_empty_bodies_decode(table: _Table) -> None:
    await _store(_key())
    row = table.rows[_key()]
    row["body_hash"] = row["body_hash"].encode("utf-8")
    row["response_body"] = ""
    row["response_headers"] = None
    got = await idem.check(_key(), idem.hash_body(BODY))
    assert got.hit is not None and got.hit.body is None and got.hit.headers == {}


async def test_non_dict_answers_round_trip(table: _Table) -> None:
    await _store(_key(), response=[1, "two"])
    got = await idem.check(_key(), idem.hash_body(BODY))
    assert got.hit is not None and got.hit.body == [1, "two"]


# ------------------------------------------------------------------ fail open


async def test_a_failed_read_is_a_miss_and_is_logged(
    table: _Table, caplog: pytest.LogCaptureFixture
) -> None:
    await _store(_key())
    table.fail_reads = True
    with caplog.at_level(logging.ERROR, logger="server.core.idempotency"):
        got = await idem.check(_key(), idem.hash_body(BODY))
    assert got == idem.CacheResult(hit=None, conflict=False)
    assert any("treating as miss" in r.getMessage() for r in caplog.records)


async def test_a_failed_write_never_raises_and_stores_nothing(
    table: _Table, caplog: pytest.LogCaptureFixture
) -> None:
    table.fail_writes = True
    with caplog.at_level(logging.ERROR, logger="server.core.idempotency"):
        await _store(_key())
    assert table.rows == {}
    assert any("idempotency.store failed" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ the body hash


class _In(BaseModel):
    org: str
    namespace: str
    run_id: str = "-"
    tags: list[str] = []


def test_the_body_hash_ignores_key_order_and_treats_none_as_empty() -> None:
    assert idem.hash_body({"a": 1, "b": 2}) == idem.hash_body({"b": 2, "a": 1})
    assert idem.hash_body(None) == idem.hash_body({})
    assert idem.hash_body({"a": 1}) != idem.hash_body({"a": 2})


def test_the_request_hash_is_the_validated_input_with_transport_fields_reset() -> None:
    plain = _In(org="o", namespace="n")
    stamped = _In(org="o", namespace="n", run_id="session-run")
    assert idem.request_hash(plain) != idem.request_hash(stamped)
    assert idem.request_hash(stamped, unset=("run_id",)) == idem.request_hash(plain)
    # a name the model does not have is ignored, not added
    assert idem.request_hash(plain, unset=("nope",)) == idem.request_hash(plain)
    # defaults count: an explicit default hashes as the omitted field
    assert idem.request_hash(_In(org="o", namespace="n", tags=[])) == idem.request_hash(plain)


def test_the_request_hash_of_a_raw_body_is_its_body_hash() -> None:
    assert idem.request_hash({"a": 1}) == idem.hash_body({"a": 1})
    assert idem.request_hash(["x"], unset=("a",)) == idem.hash_body(["x"])
