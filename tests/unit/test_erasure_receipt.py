"""The erasure receipt: the census a saga's last complete count recorded, every
substrate with its count, carried on the ledger row and returned by erasure.status;
the purge a saga applies; and the ledger reads that act on a saga's rows. CH-free."""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.erasure.app import saga
from server.erasure.domain.scopes import applied_purge
from server.erasure.store import ledger, provider_copies
from server.erasure.store.provider_copies import REBUILD_COPIES, fold_census
from server.erasure.store.rows import LedgerRow, VerifyColumns

_AT = datetime(2026, 1, 2, tzinfo=UTC).replace(tzinfo=None)


@pytest.mark.parametrize(
    ("scope_type", "scope_value", "requested", "applied"),
    [
        ("user_id", "alice", True, True),
        ("user_id", "alice", False, False),
        ("namespace", "ns", True, False),  # a single namespace never purges
        ("namespace_list", '{"namespaces": ["a"], "purge": true}', False, True),
        ("namespace_list", '{"namespaces": ["a"], "purge": false}', True, False),
        ("namespace_list", '["a"]', True, True),
    ],
)
def test_the_purge_a_saga_applies(
    scope_type: str, scope_value: str, requested: bool, applied: bool
) -> None:
    assert applied_purge(scope_type, scope_value, requested) is applied


async def test_the_rebuild_copies_fold_into_one_key() -> None:
    """The keys copy_residue emits, not hand-written ones: the fold and the copy names
    share one prefix."""
    copies = {
        "_provider_rebuild_" + "a" * 32 + "_memory_source": 2,
        "_provider_rebuild_" + "b" * 32 + "_chunks_target": 1,
    }

    class _Fake:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"table": name, "columns": ["org"]} for name in copies]

        async def query_one(self, sql: str, params: Any = None) -> dict[str, int]:
            return {"n": next(n for name, n in copies.items() if f"`{name}`" in sql)}

    residue = await provider_copies.copy_residue("o", "user_id", "alice", ch=_Fake())  # type: ignore[arg-type]
    per_table = {"loom.memory": 0, "pg:loom.memory_current": 0, **residue}
    assert fold_census(per_table) == {
        "loom.memory": 0,
        "pg:loom.memory_current": 0,
        REBUILD_COPIES: 3,
    }
    assert fold_census({"loom.memory": 0}) == {"loom.memory": 0, REBUILD_COPIES: 0}


def test_a_complete_census_is_kept_whole_and_an_incomplete_one_not() -> None:
    complete = {"per_table": {"loom.memory": 0, "loom.chunks": 0}, "count": 0, "verified_at": _AT}
    columns = saga._verify_columns(complete)
    assert json.loads(columns["census"]) == {"loom.memory": 0, "loom.chunks": 0, REBUILD_COPIES: 0}
    assert json.loads(columns["residue"]) == {}
    found = {"per_table": {"loom.memory": 2}, "count": 2, "verified_at": None}
    # A census that left a store uncounted keeps its residue as evidence, no receipt.
    assert saga._verify_columns(found)["census"] == ""
    # A complete census that found rows fails the saga, and its receipt says where.
    failed = {"per_table": {"loom.memory": 2, "loom.chunks": 0}, "count": 2, "verified_at": _AT}
    columns = saga._verify_columns(failed)
    assert json.loads(columns["census"]) == {"loom.memory": 2, "loom.chunks": 0, REBUILD_COPIES: 0}
    assert json.loads(columns["residue"]) == {"loom.memory": 2}


def test_the_recovery_worker_carries_the_census_it_read() -> None:
    from server.erasure.workers import recovery

    row: Any = {"residue_count": 0, "residue": "{}", "verified_at": _AT, "census": '{"a": 0}'}
    assert recovery._ledger_verify(row)["census"] == '{"a": 0}'


def test_every_ledger_read_that_feeds_a_checkpoint_selects_every_verify_column() -> None:
    """A re-checkpoint writes the verify columns it read: one it left out would land as
    the column's default over the census the saga recorded."""
    source = inspect.getsource(ledger.list_unabandoned_exhausted)
    select = source.split("SELECT", 1)[1].split("FROM loom.erasure_requests", 1)[0]
    selected = {c.strip() for c in select.replace("\n", " ").split(",")}
    assert set(VerifyColumns.__annotations__) <= selected, selected
    assert set(VerifyColumns.__annotations__) <= set(LedgerRow.__annotations__)


# The fences read every row they find: a stale unfinished row keeps fencing, which
# fails closed.
_FENCES = {"unfinished_since", "unfinished_reaching"}


def test_every_ledger_read_but_the_fences_skips_rows_past_the_ttl() -> None:
    tree = ast.parse(inspect.getsource(ledger))
    readers = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and "loom.erasure_requests" in ast.unparse(node)
        and "insert" not in node.name
    ]
    assert readers
    for node in readers:
        text = ast.unparse(node)
        if node.name in _FENCES:
            assert "LIVE_LEDGER" not in text, node.name
        else:
            assert "LIVE_LEDGER" in text, node.name


def _status_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "00000000-0000-4000-8000-000000000001",
        "org": "o",
        "scope_type": "namespace",
        "scope_value": "ns",
        "requester": "ops",
        "status": "completed",
        "current_step": 99,
        "failure_reason": "",
        "requested_at": _AT,
        "completed_at": _AT,
        "residue_count": 0,
        "verified_at": _AT,
        "residue": "{}",
        "census": '{"loom.memory": 0}',
        "purge": False,
    }
    row.update(over)
    return row


async def _status(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> Any:
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app import handlers

    async def fetch(*_: Any, **__: Any) -> dict[str, Any]:
        return row

    async def allowed(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(handlers.erasure_ledger, "fetch_request_status", fetch)
    monkeypatch.setattr(handlers, "_authorize_status_read", allowed)
    return await handlers.handle_erasure_status(ErasureStatusInput(erasure_id=row["id"]))


async def test_the_status_serves_the_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _status(monkeypatch, _status_row())
    assert out.census == {"loom.memory": 0} and out.purge is False


async def test_a_row_an_older_build_wrote_has_no_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """N-1 writes no census; the column's default reads as no receipt."""
    out = await _status(monkeypatch, _status_row(census=""))
    assert out.census is None


async def test_an_older_single_namespace_row_that_asked_to_purge_reads_as_not_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older build stored the requested flag; the saga never purged a single namespace."""
    out = await _status(monkeypatch, _status_row(purge=True))
    assert out.purge is False
    user = await _status(
        monkeypatch, _status_row(scope_type="user_id", scope_value="a", purge=True)
    )
    assert user.purge is True


async def test_the_status_read_stops_at_89_days() -> None:
    """The statement sent, not the source text: the predicate must reach the WHERE."""
    sent: list[str] = []

    class _Fake:
        async def query_one(self, sql: str, params: dict[str, Any]) -> None:
            sent.append(" ".join(sql.split()))

    assert await ledger.fetch_request_status("x", ch=_Fake()) is None  # type: ignore[arg-type]
    assert "WHERE id = {id:UUID} AND requested_at > now() - INTERVAL 89 DAY" in sent[0]


@pytest.mark.parametrize(("stored", "applied"), [(True, True), (False, False)])
def test_the_re_verify_of_an_older_namespace_list_row_takes_the_purge_it_ran(
    stored: bool, applied: bool
) -> None:
    """A bare-array scope (an older row) names no purge; the column holds the one the saga ran."""
    from server.cli import hygiene

    row = {"scope_type": "namespace_list", "scope_value": '["a", "b"]', "purge": stored}
    assert hygiene._re_verify_scope(row) == ("namespace", ["a", "b"], applied, True)


def test_the_read_window_ends_a_day_before_the_ledger_ttl() -> None:
    """LIVE_LEDGER must stop reading before the TTL drops a saga's rows part by part."""
    import re
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS loom.erasure_requests") :]
    (ttl_days,) = re.findall(r"TTL requested_at \+ toIntervalDay\((\d+)\)", table.split(";")[0])
    (window_days,) = re.findall(r"INTERVAL (\d+) DAY", ledger.LIVE_LEDGER)
    assert int(window_days) == int(ttl_days) - 1
