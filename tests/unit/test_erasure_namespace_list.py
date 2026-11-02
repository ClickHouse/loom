"""Erasure over a list of namespaces: one saga, one mutation per
table for the whole list; `purge` deletes where a single-namespace erasure
would tombstone."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.erasure.api.erasure import ErasureRequestInput, parse_namespace_list, parse_purge_flag
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps


class _CH:
    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.polls: list[tuple[str, dict[str, Any]]] = []
        self.poll_rows: list[list[dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return {"n": self.count}

    async def execute(self, sql: str, params: dict[str, Any] | None = None):
        self.commands.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        if "startsWith(t.name, '_provider_rebuild_')" in sql:
            return []
        self.polls.append((" ".join(sql.split()), dict(params or {})))
        return (
            self.poll_rows.pop(0)
            if self.poll_rows
            else [
                {
                    "mutation_id": "mutation_1.txt",
                    "is_done": 1,
                    "parts_to_do": 0,
                    "latest_fail_reason": "",
                }
            ]
        )


async def test_a_list_in_ns_becomes_one_in_predicate_for_count_and_mutation() -> None:
    ch = _CH()
    n = await memory_steps.tombstone_memory_by_namespace("o", ["lme-a", "lme-b"], ch=ch)
    assert n == 3
    csql, cparams = ch.queries[0]
    msql, mparams = ch.commands[0]
    assert "namespace IN {ns:Array(String)}" in csql and "namespace = {ns:String}" not in csql
    assert "namespace IN {ns:Array(String)}" in msql
    assert cparams["ns"] == ["lme-a", "lme-b"] and mparams["ns"] == ["lme-a", "lme-b"]
    assert "UPDATE tombstoned_at" in msql  # no purge: a tombstone, as for one namespace


async def test_a_single_namespace_keeps_the_equality_predicate() -> None:
    ch = _CH()
    await memory_steps.tombstone_memory_by_namespace("o", "lme-a", ch=ch)
    assert "namespace = {ns:String}" in ch.queries[0][0]
    assert ch.queries[0][1]["ns"] == "lme-a"


async def test_purge_mode_turns_tombstones_into_deletes_and_leaves_deletes_alone() -> None:
    ch = _CH()
    with erasure_mutations.purge_scope(True):
        await memory_steps.tombstone_embeddings_by_namespace("o", ["a", "b"], ch=ch)
        await memory_steps.delete_memory_versions_by_namespace("o", ["a", "b"], ch=ch)
    # The wide table purges with a lightweight DELETE.
    assert ch.commands[0][0].lstrip().startswith("DELETE FROM loom.memory_embeddings")
    assert ch.commands[1][0].startswith("ALTER TABLE loom.memory_versions DELETE WHERE")
    # outside the saga the mode is off again
    ch2 = _CH()
    await memory_steps.tombstone_embeddings_by_namespace("o", "a", ch=ch2)
    assert "UPDATE tombstoned_at" in ch2.commands[0][0]


async def test_residue_census_counts_the_whole_list() -> None:
    class _CensusCH(_CH):
        async def query_one(self, sql, params=None):
            self.queries.append((" ".join(sql.split()), dict(params or {})))
            return {"n": 0}

    ch = _CensusCH()
    out = await erasure_census.residue_census("o", "namespace", ["a", "b"], ch=ch)
    assert out and all(v == 0 for v in out.values())
    # loom.idempotency has no namespace column and is counted org-wide.
    scoped = [q for q, _ in ch.queries if "FROM loom.idempotency" not in q]
    assert all("namespace IN {ns:Array(String)}" in q for q in scoped)
    assert all(p["ns"] == ["a", "b"] for _, p in ch.queries)


def test_parse_namespace_list_accepts_array_or_object_and_folds_duplicates() -> None:
    assert parse_namespace_list('["a", "b", "a"]') == ["a", "b"]
    assert parse_namespace_list('{"namespaces": [" x ", "y"], "purge": true}') == ["x", "y"]
    assert parse_purge_flag('{"namespaces": ["x"], "purge": true}') is True
    assert parse_purge_flag('["x"]', default=False) is False
    with pytest.raises(ValueError):
        parse_namespace_list("not json")
    with pytest.raises(ValueError):
        parse_namespace_list("[1, 2]")


def test_request_validator_rejects_empty_or_oversized_lists() -> None:
    ok = ErasureRequestInput(
        org="o", scope_type="namespace_list", scope_value='["a","b"]', requester="r", purge=True
    )
    assert ok.purge is True
    with pytest.raises(ValueError):
        ErasureRequestInput(org="o", scope_type="namespace_list", scope_value="[]", requester="r")
    with pytest.raises(ValueError):
        ErasureRequestInput(
            org="o",
            scope_type="namespace_list",
            scope_value=str([f"n{i}" for i in range(501)]).replace("'", '"'),
            requester="r",
        )
    # a plain namespace scope is untouched by the validator
    ErasureRequestInput(org="o", scope_type="namespace", scope_value="ns", requester="r")


async def test_saga_dispatch_passes_the_list_and_purge_to_the_namespace_saga(monkeypatch) -> None:
    import server.erasure.app.saga as h

    seen: dict[str, Any] = {}

    async def fake_saga(org, namespace, *, completed, row, verify=None, external=True):
        seen["namespace"] = namespace
        seen["purge"] = erasure_mutations.purge_mode()
        return []

    async def fake_checkpoint(**kw):
        seen.setdefault("checkpoints", []).append(kw.get("status"))

    async def fake_emit(**kw):
        return None

    monkeypatch.setattr(h, "_saga_namespace", fake_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(h.audit, "emit", fake_emit)
    inp = ErasureRequestInput(
        org="o",
        scope_type="namespace_list",
        scope_value='{"namespaces": ["lme-1", "lme-2"], "purge": true}',
        requester="cli",
    )
    out = await h.run_saga(
        input=inp,
        row={},
        erasure_id="e1",
        requested_at=h.datetime.now(h.UTC).replace(tzinfo=None),
        previously_completed=set(),
    )
    assert seen["namespace"] == ["lme-1", "lme-2"] and seen["purge"] is True
    assert erasure_mutations.purge_mode() is False  # reset after the saga
    assert out.status == "completed"


async def test_gc_candidates_fence_off_excluded_prefixes_and_registered_namespaces() -> None:
    from server.cli.hygiene import _gc_candidates

    class _CH:
        def __init__(self) -> None:
            self.sql = ""
            self.params: dict[str, Any] = {}

        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.sql = " ".join(sql.split())
            self.params = dict(params or {})
            return []

    ch = _CH()
    await _gc_candidates(
        ch,
        org="dev",
        prefixes=("lme-", "mb-"),
        idle_hours=24,
        include_registered=False,
        exclude_prefixes=("locomo-seq2-conv-",),
    )
    assert "namespace LIKE {p0:String} OR namespace LIKE {p1:String}" in ch.sql
    assert "namespace NOT LIKE {x0:String}" in ch.sql and ch.params["x0"] == "locomo-seq2-conv-%"
    assert "namespace NOT IN (SELECT namespace FROM loom.namespaces FINAL" in ch.sql
    assert "INTERVAL {idle:UInt32} HOUR" in ch.sql and ch.params["idle"] == 24
    ch2 = _CH()
    await _gc_candidates(
        ch2,
        org="dev",
        prefixes=(),
        idle_hours=1,
        include_registered=True,
        explicit=("lme-a", "lme-b"),
    )
    assert "namespace IN {explicit:Array(String)}" in ch2.sql and "NOT IN (SELECT" not in ch2.sql
    # explicit names count tombstoned rows too (a half-erased namespace can be finished);
    # the prefix scan above is live-only
    assert (
        "AND tombstoned_at IS NULL" not in ch2.sql
        and "countIf(tombstoned_at IS NULL) AS live_rows" in ch2.sql
    )
    assert "AND tombstoned_at IS NULL" in ch.sql


async def test_gc_run_keeps_the_client_for_the_sagas_and_the_after_footprint(monkeypatch) -> None:
    """The erasure handler reads the same module-level client, so a run that
    disconnected after the scan would fail every saga on its first checkpoint
    insert. `_gc_run` takes the connected client and never disconnects it;
    the footprint after the sagas uses the same client."""
    import argparse

    from server.cli import hygiene

    calls: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            calls.append("scan" if "FROM loom.memory FINAL" in sql else "parts")
            if "FROM loom.memory FINAL" in sql:
                return [{"namespace": "lme-x", "rows": 5, "last_write": None, "recalled_rows": 0}]
            return [{"table": "memory", "parts": 1, "rows": 5}]

        async def disconnect(self):
            calls.append("disconnect")

    async def fake_erase(inp):
        calls.append(f"erase:{inp.scope_type}:{inp.purge}")
        return SimpleNamespace(
            erasure_id="e",
            status="completed",
            steps=[],
            residue_count=0,
        )

    monkeypatch.setattr("server.erasure.app.handlers.handle_erasure_request", fake_erase)
    args = argparse.Namespace(
        org="dev",
        idle_hours=24,
        include_registered=False,
        exclude_prefix=None,
        show=5,
        erase=True,
        no_purge=False,
        batch=200,
        requester="t",
    )
    rc = await hygiene._gc_run(_CH(), args, prefixes=("lme-",), explicit=())
    assert rc == 0
    assert calls == ["scan", "parts", "erase:namespace_list:True", "parts"]
    assert "disconnect" not in calls


# ---------------------------------------------------------------------------
# A mutation is awaited by state, not by socket.
# ---------------------------------------------------------------------------


def _no_sleep(monkeypatch) -> None:
    async def _zero(_s: float) -> None:
        return None

    monkeypatch.setattr(erasure_mutations.asyncio, "sleep", _zero)


async def test_run_mutation_submits_async_with_a_tag_and_polls_system_mutations(
    monkeypatch,
) -> None:
    _no_sleep(monkeypatch)
    ch = _CH()
    ch.poll_rows = [
        [],  # not registered yet on the first poll
        [{"mutation_id": "m7", "is_done": 0, "parts_to_do": 4, "latest_fail_reason": ""}],
        [{"mutation_id": "m7", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}],
    ]
    row = await erasure_mutations.run_mutation(
        "loom.memory_embeddings",
        mutation="DELETE",
        where="org = {org:String} AND namespace IN {ns:Array(String)}",
        params={"org": "dev", "ns": ["lme-a"]},
        ch=ch,
    )
    sql, params = ch.commands[0]
    assert "SETTINGS mutations_sync = 0" in sql and "mutations_sync = 2" not in sql
    assert "AND ({erasure_mutation_tag:String} = {erasure_mutation_tag:String})" in sql
    assert params["erasure_mutation_tag"].startswith("erasure:") and params["ns"] == ["lme-a"]
    assert len(ch.polls) == 3
    psql, pparams = ch.polls[0]
    assert "FROM system.mutations" in psql and pparams == {
        "db": "loom",
        "t": "memory_embeddings",
        "pat": f"%{params['erasure_mutation_tag']}%",
    }
    assert row is not None and row["mutation_id"] == "m7" and row["is_done"] == 1


async def test_a_mutation_that_keeps_failing_fails_the_step_with_the_reason(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    ch = _CH()
    bad = {
        "mutation_id": "m9",
        "is_done": 0,
        "parts_to_do": 1,
        "latest_fail_reason": "Code: 241 MEMORY_LIMIT_EXCEEDED",
    }
    ch.poll_rows = [[bad], [bad], [bad], [bad]]
    with pytest.raises(erasure_mutations.MutationFailed, match="MEMORY_LIMIT_EXCEEDED"):
        await erasure_mutations.wait_for_mutation("loom.memory", "erasure:x", ch=ch)


async def test_a_mutation_still_running_past_the_budget_raises_not_hangs(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    ch = _CH()
    running = {"mutation_id": "m9", "is_done": 0, "parts_to_do": 3, "latest_fail_reason": ""}
    ch.poll_rows = [[running]] * 50
    clock = iter([0.0, 0.0, 0.5, 1.0, 2.0, 5.0, 10.0])
    monkeypatch.setattr(erasure_mutations.time, "monotonic", lambda: next(clock, 10.0))
    with pytest.raises(erasure_mutations.MutationStillRunning, match="parts_to_do=3"):
        await erasure_mutations.wait_for_mutation("loom.memory", "erasure:x", ch=ch, wait_s=3.0)


async def test_a_mutation_never_observed_returns_none_after_the_grace(monkeypatch) -> None:
    _no_sleep(monkeypatch)
    ch = _CH()
    ch.poll_rows = [[]] * 50
    monkeypatch.setattr(erasure_mutations, "_MUTATION_FIRST_SEEN_GRACE_S", 0.0)
    assert await erasure_mutations.wait_for_mutation("loom.memory", "erasure:x", ch=ch) is None
    assert len(ch.polls) == 1


async def test_count_then_mutate_still_returns_the_count_and_skips_the_alter_at_zero() -> None:
    ch = _CH(count=0)
    n = await memory_steps.tombstone_memory_by_namespace("o", ["lme-a"], ch=ch)
    assert n == 0 and ch.commands == [] and ch.polls == []


def test_the_handler_keeps_a_purge_written_inside_scope_value() -> None:
    """The CLI writes purge into the scope_value object; the handler's re-fold
    reads the object first, so a namespaces-gc run deletes the rows instead
    of tombstoning them on `input.purge` (False)."""
    inp = ErasureRequestInput(
        org="o",
        scope_type="namespace_list",
        scope_value='{"namespaces": ["lme-1"], "purge": true}',
        requester="cli",
    )
    folded = inp.model_copy(
        update={
            "purge": parse_purge_flag(inp.scope_value, default=bool(inp.purge)),
            "scope_value": json.dumps(
                {
                    "namespaces": parse_namespace_list(inp.scope_value),
                    "purge": parse_purge_flag(inp.scope_value, default=bool(inp.purge)),
                }
            ),
        }
    )
    assert folded.purge is True and json.loads(folded.scope_value)["purge"] is True
    # and the field still wins when the object says nothing
    assert parse_purge_flag('["lme-1"]', default=True) is True


# ---------------------------------------------------------------------------
# A namespace_list erasure may run loom-only.
# ---------------------------------------------------------------------------


def test_external_false_is_only_allowed_for_a_namespace_list() -> None:
    from server.erasure.api.erasure import parse_external_flag

    ErasureRequestInput(
        org="o",
        scope_type="namespace_list",
        scope_value='["lme-1"]',
        requester="cli",
        external=False,
    )
    with pytest.raises(ValueError, match="external=false"):
        ErasureRequestInput(
            org="o", scope_type="user_id", scope_value="u1", requester="cli", external=False
        )
    with pytest.raises(ValueError, match="external=false"):
        ErasureRequestInput(
            org="o", scope_type="namespace", scope_value="ns", requester="cli", external=False
        )
    assert parse_external_flag('{"namespaces": ["a"], "external": false}') is False
    assert parse_external_flag('["a"]', default=True) is True


async def test_a_loom_only_saga_never_calls_the_external_backends(monkeypatch) -> None:
    import server.erasure.app.saga as h

    class _Backend:
        name = "langfuse"

        async def delete_namespace(self, org: str, ns: str) -> int:
            raise AssertionError("external backend called on a loom-only saga")

    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0, status="completed")

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    import server.erasure.app.backends as eb

    monkeypatch.setattr(eb, "all_backends", lambda: [_Backend()])
    steps = await h._saga_namespace(
        "o", ["lme-1", "lme-2"], completed=set(), row={}, external=False
    )
    assert steps and not any(name.endswith(".delete_namespace") for name in calls)

    calls.clear()
    await h._saga_namespace("o", ["lme-1"], completed=set(), row={}, external=True)
    assert "langfuse.delete_namespace" in calls  # the default still runs every backend


async def test_saga_dispatch_reads_external_from_scope_value(monkeypatch) -> None:
    import server.erasure.app.saga as h

    seen: dict[str, Any] = {}

    async def fake_saga(org, namespace, *, completed, row, verify=None, external=True):
        seen["external"] = external
        return []

    async def fake_checkpoint(**kw):
        return None

    async def fake_emit(**kw):
        return None

    monkeypatch.setattr(h, "_saga_namespace", fake_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(h.audit, "emit", fake_emit)
    inp = ErasureRequestInput(
        org="o",
        scope_type="namespace_list",
        scope_value='{"namespaces": ["lme-1"], "purge": true, "external": false}',
        requester="cli",
    )
    await h.run_saga(
        input=inp,
        row={},
        erasure_id="e2",
        requested_at=h.datetime.now(h.UTC).replace(tzinfo=None),
        previously_completed=set(),
    )
    assert seen["external"] is False


# ---------------------------------------------------------------------------
# A purge removes every row of the scope, the entity
# graph included, and its census counts every row.
# ---------------------------------------------------------------------------


async def test_purge_counts_and_deletes_tombstoned_rows_too() -> None:
    ch = _CH()
    with erasure_mutations.purge_scope(True):
        await memory_steps.tombstone_memory_by_namespace("o", ["lme-a"], ch=ch)
    csql, _ = ch.queries[0]
    msql, _ = ch.commands[0]
    assert "tombstoned_at IS NULL" not in csql and "tombstoned_at IS NULL" not in msql
    assert "ALTER TABLE loom.memory DELETE WHERE" in msql
    # without purge the live predicate stays on both
    ch2 = _CH()
    await memory_steps.tombstone_memory_by_namespace("o", ["lme-a"], ch=ch2)
    assert (
        "tombstoned_at IS NULL" in ch2.queries[0][0]
        and "tombstoned_at IS NULL" in ch2.commands[0][0]
    )


async def test_the_entity_graph_is_deleted_by_namespace() -> None:
    ch = _CH(count=5)
    n = await graph_steps.delete_entity_graph_by_namespace("o", ["lme-a", "lme-b"], ch=ch)
    assert n == 15  # three tables (+degree)
    tables = [c[0].split(" ")[2] for c in ch.commands]
    assert tables == [
        "loom.entity_edges",
        "loom.entity_triples",
        "loom.entity_degree",
    ]
    assert all(
        "DELETE WHERE (org = {org:String} AND namespace IN {ns:Array(String)})" in c[0]
        for c in ch.commands
    )
    assert "loom.entity_edges" in erasure_census.census_tables("namespace")
    assert "loom.entity_edges" not in erasure_census.census_tables("user_id")


async def test_the_namespace_saga_runs_the_entity_graph_step(monkeypatch) -> None:
    import server.erasure.app.saga as h

    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0, status="completed")

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    await h._saga_namespace("o", ["lme-1"], completed=set(), row={}, external=False)
    assert calls.index("entity_graph.delete") == calls.index("graph_links.delete") + 1


async def test_a_purge_census_counts_every_row_not_only_live_ones() -> None:
    ch = _CH(count=0)
    with erasure_mutations.purge_scope(True):
        await erasure_census.residue_census("o", "namespace", ["lme-a"], ch=ch)
    assert ch.queries and all(
        "tombstoned_at IS NULL" not in q[0] and " FINAL" not in q[0] for q in ch.queries
    )
    ch2 = _CH(count=0)
    await erasure_census.residue_census("o", "namespace", ["lme-a"], ch=ch2)
    assert any("tombstoned_at IS NULL" in q[0] for q in ch2.queries)


# ---------------------------------------------------------------------------
# A re-run step finds its own pending mutation.
# ---------------------------------------------------------------------------


async def test_inside_a_saga_the_tag_is_deterministic_and_a_pending_mutation_is_awaited_not_resubmitted(
    monkeypatch,
) -> None:
    _no_sleep(monkeypatch)
    where = "org = {org:String}"
    with erasure_mutations.saga_scope("e-123"):
        t1 = erasure_mutations.mutation_tag("loom.memory", "DELETE", where, {"org": "o"})
        t2 = erasure_mutations.mutation_tag("loom.memory", "DELETE", where, {"org": "o"})
        t3 = erasure_mutations.mutation_tag("loom.memory_embeddings", "DELETE", where, {"org": "o"})
        assert t1 == t2 and t1 != t3 and t1.startswith("erasure:e-123:")
        # two slices of one step bind different values: each waits for its own mutation
        assert t1 != erasure_mutations.mutation_tag("loom.memory", "DELETE", where, {"org": "p"})
        ch = _CH()
        # first poll = the pending-mutation probe finds one running; then it finishes
        ch.poll_rows = [
            [{"mutation_id": "m1", "is_done": 0, "parts_to_do": 3, "latest_fail_reason": ""}],
            [{"mutation_id": "m1", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}],
        ]
        row = await erasure_mutations.run_mutation(
            "loom.memory", mutation="DELETE", where="org = {org:String}", params={"org": "o"}, ch=ch
        )
        assert ch.commands == []  # nothing resubmitted
        assert row is not None and row["is_done"] == 1
        assert "is_done = 0" in ch.polls[0][0] and ch.polls[0][1]["pat"] == f"%{t1}%"
        # no pending mutation → submit as usual, with the same deterministic tag; the
        # stamp a re-run binds is a later time and stays out of the tag
        ch2 = _CH()
        ch2.poll_rows = [
            [],
            [{"mutation_id": "m2", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}],
        ]
        await erasure_mutations.run_mutation(
            "loom.memory",
            mutation="DELETE",
            where="org = {org:String}",
            params={"org": "o"},
            stamp={"now": "a later time"},
            ch=ch2,
        )
        ((_, sent),) = ch2.commands
        assert sent["erasure_mutation_tag"] == t1 and sent["now"] == "a later time"
    # outside a saga: unique per call, no pending probe
    assert erasure_mutations.mutation_tag(
        "loom.memory", "DELETE", "x"
    ) != erasure_mutations.mutation_tag("loom.memory", "DELETE", "x")


async def test_the_dispatcher_sets_the_saga_id_for_the_run_and_resets_it(monkeypatch) -> None:
    import server.erasure.app.saga as h

    seen: dict[str, Any] = {}

    async def fake_saga(org, namespace, *, completed, row, verify=None, external=True):
        seen["saga_id"] = erasure_mutations._SAGA_ID.get()
        return []

    async def fake_checkpoint(**kw):
        return None

    async def fake_emit(**kw):
        return None

    monkeypatch.setattr(h, "_saga_namespace", fake_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(h.audit, "emit", fake_emit)
    inp = ErasureRequestInput(org="o", scope_type="namespace", scope_value="ns", requester="cli")
    await h.run_saga(
        input=inp,
        row={},
        erasure_id="e-777",
        requested_at=h.datetime.now(h.UTC).replace(tzinfo=None),
        previously_completed=set(),
    )
    assert seen["saga_id"] == "e-777" and erasure_mutations._SAGA_ID.get() == ""


async def test_gc_run_erases_explicit_names_even_when_the_memory_table_has_no_rows_for_them(
    monkeypatch,
) -> None:
    """A namespace whose loom.memory rows are already gone can still hold rows
    in the side tables (500 embedding rows re-emitted by a worker after the
    purge); an explicit --namespace is erased whether or not the scan sees it."""
    import argparse

    from server.cli import hygiene

    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            return [{"table": "memory", "parts": 1, "rows": 0}] if "system.parts" in sql else []

    async def fake_erase(inp):
        seen["names"] = json.loads(inp.scope_value)["namespaces"]
        return SimpleNamespace(erasure_id="e", status="completed", steps=[], residue_count=0)

    monkeypatch.setattr("server.erasure.app.handlers.handle_erasure_request", fake_erase)
    args = argparse.Namespace(
        org="dev",
        idle_hours=0,
        include_registered=False,
        exclude_prefix=None,
        show=5,
        erase=True,
        no_purge=False,
        batch=200,
        requester="t",
        skip_external=True,
    )
    rc = await hygiene._gc_run(_CH(), args, prefixes=(), explicit=("lme-a", "lme-b"))
    assert rc == 0 and seen["names"] == ["lme-a", "lme-b"]


async def test_gc_run_does_not_readd_explicit_names_the_scan_fenced_off(monkeypatch) -> None:
    """Only an explicit name with no loom.memory row at all is re-added. One
    the scan dropped for being registered, under --exclude-prefix, or not idle
    (it has rows, so the scan saw and refused it) stays out of the erasure."""
    import argparse

    from server.cli import hygiene

    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            if "system.parts" in sql:
                return [{"table": "memory", "parts": 1, "rows": 0}]
            if "GROUP BY namespace" in sql:
                return []  # the candidate scan: nothing idle
            if "SELECT DISTINCT namespace FROM loom.memory" in sql:
                return [{"namespace": "lme-busy"}]  # has rows, but written recently
            assert "FROM loom.namespaces FINAL" in sql
            return [{"namespace": "lme-owned"}]

    async def fake_erase(inp):
        seen["names"] = json.loads(inp.scope_value)["namespaces"]
        return SimpleNamespace(erasure_id="e", status="completed", steps=[], residue_count=0)

    monkeypatch.setattr("server.erasure.app.handlers.handle_erasure_request", fake_erase)
    args = argparse.Namespace(
        org="dev",
        idle_hours=24,
        include_registered=False,
        exclude_prefix=["keep-"],
        show=5,
        erase=True,
        no_purge=False,
        batch=200,
        requester="t",
        skip_external=True,
    )
    explicit = ("lme-empty", "lme-busy", "lme-owned", "keep-ab")
    rc = await hygiene._gc_run(_CH(), args, prefixes=(), explicit=explicit)
    assert rc == 0 and seen["names"] == ["lme-empty"]
