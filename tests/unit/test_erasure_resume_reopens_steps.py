"""A resumed saga re-runs the steps that own the last census's
residue, and the census and the sagas' step lists derive from one registry.
CH-free."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from typing import Any

import pytest

import server.erasure.app.saga as h
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.store import census as erasure_census
from server.erasure.store import health as erasure_health
from server.erasure.store import ledger as erasure_ledger

_OPERATOR_RESIDUE = {
    "loom.memory_belief_sources": 454610,
    "loom.memory_chunk_sources": 1812448,
    "loom.memory_chunks": 176246,
    "loom.session_summaries": 9058,
}


def _now():
    return h.datetime.now(h.UTC).replace(tzinfo=None)


async def _async_noop(*a, **kw):
    return None


def _record_steps(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0, notes="")

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    return calls


# ---------------------------------------------------------------------------
# one registry: census tables ⇄ saga steps
# ---------------------------------------------------------------------------


def test_every_census_table_names_an_owner_step_in_each_scope_it_is_counted_in() -> None:
    for sub in erasure_census._CENSUS:
        assert sub.ns_step, sub.table
        assert (sub.user_pred is None) == (sub.user_step is None), sub.table
    owners_ns = erasure_census.step_owners("namespace")
    owners_user = erasure_census.step_owners("user_id")
    assert set(sum(owners_ns.values(), ())) == set(erasure_census.census_tables("namespace"))
    assert set(sum(owners_user.values(), ())) == set(erasure_census.census_tables("user_id"))
    # the four tables of the operator's residue are owned by three steps in both scopes
    assert erasure_census.steps_to_reopen(_OPERATOR_RESIDUE, "namespace") == {
        "memory_chunks.tombstone",
        "source_links.tombstone",
        "summaries.tombstone",
    }
    assert erasure_census.steps_to_reopen(_OPERATOR_RESIDUE, "user_id") == {
        "memory_chunks.tombstone",
        "source_links.tombstone",
        "summaries.tombstone",
    }


async def test_the_namespace_saga_runs_exactly_the_owner_steps_plus_the_documented_step_only_ones(
    monkeypatch,
) -> None:
    calls = _record_steps(monkeypatch)
    await h._saga_namespace("o", ["ns-1"], completed=set(), row={}, external=False)
    ran = set(calls) - {"verify.residue"}
    owners = set(erasure_census.step_owners("namespace"))
    assert owners <= ran, owners - ran  # every census table's owner step runs
    unexplained = ran - owners - set(erasure_census.STEP_ONLY_TABLES)
    assert not unexplained, (
        unexplained
    )  # a step without a census row must say so in STEP_ONLY_TABLES
    assert calls[-1] == "verify.residue"


async def test_the_user_saga_runs_exactly_the_owner_steps_plus_the_documented_step_only_ones(
    monkeypatch,
) -> None:
    import server.erasure.app.backends as eb

    monkeypatch.setattr(eb, "all_backends", list)
    monkeypatch.setattr("server.core.namespace_write_marks.bump", _async_noop)
    calls = _record_steps(monkeypatch)
    await h._saga_user_id("o", "alice", completed=set(), row={})
    ran = set(calls) - {"verify.residue"}
    owners = set(erasure_census.step_owners("user_id"))
    assert owners <= ran, owners - ran
    # entity_graph.delete owns the graph tables in the namespace census; the user
    # saga runs it through the link tables, uncounted by user.
    unexplained = (
        ran
        - owners
        - set(erasure_census.step_owners("namespace"))
        - set(erasure_census.STEP_ONLY_TABLES)
    )
    assert not unexplained, unexplained
    assert calls[-1] == "verify.residue"


def test_step_only_tables_are_not_census_tables() -> None:
    """A table is either counted (and owned) or documented as uncounted — the
    same table cannot be both, or the registry lies about coverage."""
    census = set(erasure_census.census_tables("namespace"))
    for step, tables in erasure_census.STEP_ONLY_TABLES.items():
        if step in ("consolidated.tombstone", "shared_embeddings.tombstone"):
            continue  # second passes over loom.memory / memory_embeddings under another identity
        assert not (set(tables) & census), (step, set(tables) & census)


def test_steps_to_reopen_reads_the_ledgers_json_and_ignores_zero_and_unknown_tables() -> None:
    ledger = json.dumps({"loom.memory_embeddings": 500, "loom.memory": 0, "loom.not_a_table": 9})
    assert erasure_census.steps_to_reopen(ledger, "namespace") == {"memory_embeddings.tombstone"}
    assert erasure_census.steps_to_reopen({"memory_chunks": 2}, "namespace") == {
        "memory_chunks.tombstone"
    }  # bare name
    assert erasure_census.steps_to_reopen("", "namespace") == set()
    assert erasure_census.steps_to_reopen(None, "user_id") == set()
    assert erasure_census.steps_to_reopen("not json", "namespace") == set()
    assert erasure_census.steps_to_reopen({"loom.entity_edges": 3}, "namespace") == {
        "entity_graph.delete"
    }
    # no user axis on the graph tables: the user census never names them
    assert erasure_census.steps_to_reopen({"loom.entity_edges": 3}, "user_id") == set()


# ---------------------------------------------------------------------------
# the resume path: the census wins over the checkpoint
# ---------------------------------------------------------------------------


async def _drive(monkeypatch, *, previously_completed, residue):
    seen: dict[str, Any] = {}

    async def fake_saga(org, namespace, *, completed, row, verify=None, external=True):
        seen["completed_at_entry"] = set(completed)
        if verify is not None:
            verify.update({"per_table": {}, "count": 0, "verified_at": _now()})
        return []

    monkeypatch.setattr(h, "_saga_namespace", fake_saga)
    monkeypatch.setattr(h, "checkpoint", _async_noop)
    monkeypatch.setattr(h.audit, "emit", _async_noop)
    inp = ErasureRequestInput(
        org="o",
        scope_type="namespace_list",
        scope_value='{"namespaces": ["lme-1"], "purge": true, "external": false}',
        requester="t",
    )
    out = await h.run_saga(
        input=inp,
        row={"id": "e1", "actor": "human:t"},
        erasure_id="e1",
        requested_at=_now(),
        previously_completed=set(previously_completed),
        residue=residue,
    )
    return out, seen


async def test_a_resumed_saga_reopens_the_owners_of_the_residue_tables(monkeypatch) -> None:
    done = {
        "memory.tombstone",
        "memory_chunks.tombstone",
        "source_links.tombstone",
        "summaries.tombstone",
        "entity_graph.delete",
    }
    out, seen = await _drive(
        monkeypatch, previously_completed=done, residue=json.dumps(_OPERATOR_RESIDUE)
    )
    assert out.status == "completed"
    assert seen["completed_at_entry"] == {"memory.tombstone", "entity_graph.delete"}


async def test_a_resume_without_residue_keeps_every_checkpointed_step(monkeypatch) -> None:
    done = {"memory.tombstone", "memory_chunks.tombstone"}
    _out, seen = await _drive(monkeypatch, previously_completed=done, residue=None)
    assert seen["completed_at_entry"] == done
    _out, seen = await _drive(monkeypatch, previously_completed=done, residue='{"loom.memory": 0}')
    assert seen["completed_at_entry"] == done


async def test_the_recovery_worker_hands_the_ledgers_residue_to_the_saga(monkeypatch) -> None:
    from server.erasure.workers import recovery as w

    seen: dict[str, Any] = {}

    async def fake_run_saga(
        *, input, row, erasure_id, requested_at, previously_completed, residue=None
    ):
        seen["residue"] = residue
        seen["previously_completed"] = set(previously_completed)
        seen["actor"] = row["actor"]
        return SimpleNamespace(
            status="completed", failure_reason="", steps_completed=[], residue_count=0
        )

    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    monkeypatch.setattr(w.audit, "emit", _async_noop)
    row = {
        "id": "e9",
        "org": "o",
        "scope_type": "namespace_list",
        "scope_value": '{"namespaces": ["lme-1"], "purge": true}',
        "requester": "t",
        "actor": "human:t",
        "urgency": "normal",
        "attempts": 1,
        "steps_completed": ["memory_chunks.tombstone"],
        "requested_at": _now(),
        "failure_reason": "verify.residue: …",
        "residue": json.dumps(_OPERATOR_RESIDUE),
    }
    result = w.RecoveryResult()
    await w._recover_one(row=row, cfg=w.RecoveryConfig(), result=result)
    assert seen["residue"] == json.dumps(_OPERATOR_RESIDUE)
    assert result.completed == 1
    assert seen["actor"] == "human:t"  # the ledger's actor, carried into the resumed run


async def test_the_cli_resume_reopens_from_the_fresh_census_and_reports_what_ran(
    monkeypatch, capsys
) -> None:
    from server.cli import hygiene

    seen: dict[str, Any] = {}
    row = {
        "id": "e9",
        "org": "o",
        "scope_type": "namespace_list",
        "scope_value": '{"namespaces": ["lme-1"], "purge": true, "external": false}',
        "requester": "t",
        "actor": "human:t",
        "urgency": "normal",
        "attempts": 6,
        "requested_at": _now(),
        "failure_reason": "verify.residue: …",
        "steps_completed": [
            "memory.tombstone",
            "memory_chunks.tombstone",
            "source_links.tombstone",
            "summaries.tombstone",
        ],
        "residue": "",
        "residue_count": 0,
        "verified_at": None,
    }

    async def fake_list(*, org=None, ch=None):
        return [row]

    async def fake_census(org, st, sv, *, ch=None):
        return dict(_OPERATOR_RESIDUE)

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    async def fake_run_saga(
        *, input, row, erasure_id, requested_at, previously_completed, residue=None
    ):
        seen["previously_completed"] = set(previously_completed)
        seen["actor"] = row["actor"]
        steps = [
            SimpleNamespace(
                step="memory.tombstone", rows_affected=0, notes="skipped (resumed from checkpoint)"
            ),
            SimpleNamespace(step="memory_chunks.tombstone", rows_affected=176246, notes=""),
            SimpleNamespace(step="source_links.tombstone", rows_affected=2267058, notes=""),
            SimpleNamespace(step="summaries.tombstone", rows_affected=9058, notes=""),
            SimpleNamespace(step="verify.residue", rows_affected=0, notes=""),
        ]
        return SimpleNamespace(
            status="completed",
            steps=steps,
            residue_count=0,
            residue={},
            verified_at=_now(),
            failure_reason="",
            steps_completed=[],
        )

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_list)
    monkeypatch.setattr(erasure_census, "residue_census", fake_census)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=True,
        skip_external=True,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=None,
    )
    assert await hygiene._re_verify_run(object(), args) == 0
    assert seen["previously_completed"] == {"memory.tombstone"}
    assert seen["actor"] == "human:t"  # the ledger's actor, carried into the resumed run
    text = capsys.readouterr().out
    assert (
        "re-opening ['memory_chunks.tombstone', 'source_links.tombstone', 'summaries.tombstone']"
        in text
    )
    assert '"source_links.tombstone": 2267058' in text and "skipped=1 ran=4" in text


def test_the_stuck_scan_selects_the_residue_column() -> None:
    import inspect

    src = inspect.getsource(erasure_ledger.list_stuck_in_progress)
    assert "failure_reason, residue" in src


@pytest.mark.parametrize("scope", ["namespace", "user_id"])
def test_census_tables_and_step_owner_tables_are_the_same_set(scope) -> None:
    assert set(erasure_census.census_tables(scope)) == set(
        sum(erasure_census.step_owners(scope).values(), ())
    )


def test_a_ledger_row_resumes_as_the_request_it_records() -> None:
    row = {
        "org": "o",
        "scope_type": "namespace",
        "scope_value": "ns",
        "requester": "t",
        "actor": "human:t",
        "urgency": "",
    }
    inp = h.input_from_ledger_row(row)
    assert (inp.org, inp.scope_type, inp.scope_value, inp.requester) == (
        "o",
        "namespace",
        "ns",
        "t",
    )
    assert inp.urgency == "normal" and inp.purge is False and inp.external is True


async def test_a_ledger_row_that_no_longer_validates_is_counted_and_never_runs(monkeypatch) -> None:
    from server.erasure.workers import recovery as w

    ran: list[str] = []

    async def fake_run_saga(**kw):
        ran.append(kw["erasure_id"])

    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    row = {
        "id": "e10",
        "org": "o",
        "scope_type": "namespace_list",
        "scope_value": "not json",
        "requester": "t",
        "actor": "human:t",
        "urgency": "normal",
        "attempts": 1,
        "steps_completed": [],
        "requested_at": _now(),
        "failure_reason": "",
    }
    result = w.RecoveryResult()
    await w._recover_one(row=row, cfg=w.RecoveryConfig(), result=result)
    assert ran == [] and result.completed == 0
    assert [e["erasure_id"] for e in result.errors] == ["e10"]
    assert "namespace_list scope_value" in result.errors[0]["error"]
