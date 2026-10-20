"""Pipeline writes carry a principal, the sensitivity sweep
labels what the write path skipped, and three row-level invariants read as
gauges with a repair for the one that hides rows from search.

Most clients are stubs; the sweep's paging runs its real SQL on clickhouse-local."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import extraction_queue as queue
from server.memory.app import sensitivity_sweep as sweep
from server.memory.app.extraction_queue import ENQUEUED_BY_KEY, enqueued_actor, payload_for
from server.memory.app.provenance import writer_actor
from server.memory.domain import provenance as prov
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

# ------------------------------------------------------------- (a) principal


def test_writer_actor_falls_back_to_the_pipeline_actor_only_without_a_token() -> None:
    set_current_auth(None)
    assert writer_actor() == ""
    with prov.pipeline_actor("process:b5-memo-bench"):
        assert writer_actor() == "process:b5-memo-bench"
        # a live request's own principal wins over the stage's
        set_current_auth(SimpleNamespace(org="o", subject="alice", is_service_account=False))
        assert writer_actor() == "human:alice"
        set_current_auth(None)
        with prov.pipeline_actor(""):  # empty leaves the chain as it was
            assert writer_actor(fallback="process:x") == "process:x"
    assert writer_actor() == ""
    assert prov.cli_actor("import-clickgap") == "process:cli:import-clickgap"
    assert prov.cli_actor("") == "process:cli:unknown"


def _input(**kw: Any):
    from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message

    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="u1",
        messages=[_Message(role="user", content="I moved to Lisbon last week")],
        defer=True,
    )
    base.update(kw)
    return MemorySetFromMessagesInput(**base)


def test_the_queue_payload_carries_the_enqueuer_and_the_rebuild_strips_it() -> None:
    set_current_auth(SimpleNamespace(org="o", subject="b5-memo-bench", is_service_account=True))
    try:
        payload, n_msgs, _chars = payload_for(_input(), scope=WHOLE_NAMESPACE)
    finally:
        set_current_auth(None)
    data = json.loads(payload)
    assert n_msgs == 1 and data[ENQUEUED_BY_KEY] == "process:b5-memo-bench"
    assert "defer" not in data
    row = {"payload": payload}
    assert enqueued_actor(row) == "process:b5-memo-bench"
    rebuilt = queue.rebuild_input(row)
    assert rebuilt.defer is False and not hasattr(rebuilt, ENQUEUED_BY_KEY)
    assert queue.replay_actor(row) == "process:b5-memo-bench"
    # no token at enqueue time → no key → the worker's own principal
    set_current_auth(None)
    payload2, _, _ = payload_for(_input(), scope=WHOLE_NAMESPACE)
    assert ENQUEUED_BY_KEY not in json.loads(payload2)
    assert queue.replay_actor({"payload": payload2}) == "process:extraction-worker"
    assert queue.replay_actor({"payload": "not json"}) == "process:extraction-worker"
    assert (
        payload_for(_input(), scope=WHOLE_NAMESPACE, enqueued_by="human:op")[0].count(
            '"enqueued_by":"human:op"'
        )
        == 1
    )


@pytest.mark.asyncio
async def test_replay_runs_the_handler_under_the_enqueuers_principal(monkeypatch) -> None:
    from server.memory.app.extract import handler as extract_handler_mod

    seen: list[str] = []

    async def _fake_handler(inp):
        seen.append(writer_actor())
        return SimpleNamespace(written=[1, 2])

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)
    set_current_auth(None)
    payload, _, _ = payload_for(
        _input(), scope=WHOLE_NAMESPACE, enqueued_by="process:b5-memo-bench"
    )
    assert await queue.replay_row({"payload": payload}) == 2
    payload_old, _, _ = payload_for(_input(), scope=WHOLE_NAMESPACE, enqueued_by="")
    assert await queue.replay_row({"payload": payload_old}) == 2
    assert seen == ["process:b5-memo-bench", "process:extraction-worker"]
    assert writer_actor() == ""  # the stage's principal does not leak out


def test_the_cli_runs_every_command_as_its_own_principal(monkeypatch) -> None:
    from server import cli
    from server.cli import app, ops

    seen: list[str] = []

    def fake_cmd(ns):
        seen.append(writer_actor())
        return 0

    monkeypatch.setattr(app, "_cmd_integrity_repair", fake_cmd)
    monkeypatch.setattr(ops, "_cmd_sensitivity_sweep", fake_cmd)
    set_current_auth(None)
    assert cli.main(["integrity-repair", "--org", "o"]) == 0
    assert cli.main(["sensitivity-sweep", "--namespace", "n", "--apply"]) == 0
    assert seen == ["process:cli:integrity-repair", "process:cli:sensitivity-sweep"]


# ------------------------------------------------------------- (b) sweep


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.one: dict[str, Any] = {"n": 0}

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return list(self.rows)

    async def query_one(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return dict(self.one)

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))


def _mem(memory_id: str, value: str, ns: str = "demo-full") -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "org": "clickhouse",
        "namespace": ns,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": f"k{memory_id}",
        "value": value,
    }


@pytest.mark.asyncio
async def test_scan_labels_exactly_as_the_write_path_would() -> None:
    ch = _CH(
        [
            _mem("1", "OPENAI_API_KEY=" + "sk-" + "proj-" + "Ab3" * 12),
            _mem("2", "reach Bram at bram@example.com"),
            _mem("3", "call me on +1 415 555 0123"),
            _mem("4", "the secret of a good stew is time"),  # coarse hit, detector says ''
        ]
    )
    report = await sweep.scan(org="clickhouse", namespace="demo-full", since_days=2, ch=ch)
    assert report.scanned == 4 and len(report.candidates) == 3 and not report.truncated
    assert report.by_level == {"high": 1, "medium": 2}
    assert report.by_shape == {"credential": 1, "email": 1, "phone": 1}
    assert report.by_namespace()[("clickhouse", "demo-full")] == {"high": 1, "medium": 2}
    narrowed = await sweep.scan(only_shapes=["credential", "email"], ch=ch)
    assert narrowed.by_shape == {"credential": 1, "email": 1}  # the phone-only row is skipped
    sql, params = ch.calls[0]
    assert "sensitivity = ''" in sql and "match(value, {shape:String})" in sql
    assert params["shape"] == sweep.COARSE_SHAPE_RE and params["days"] == 2
    assert "PREWHERE" not in sql


@pytest.mark.asyncio
async def test_apply_relabels_tombstones_high_and_tags_medium(monkeypatch) -> None:
    from server.memory import store as memory_repo

    ch = _CH()
    written: list[tuple[str, dict[str, Any]]] = []
    tombstoned: list[str] = []
    existing = SimpleNamespace(tags=["t"])

    async def _write(org, ids, update, *, ch=None):
        written.append((ids[0], update(existing)))
        return 1

    async def _retire(items, *, ch=None):
        tombstoned.extend((key[5], version) for key, version in items)
        return 2

    async def _live(ch, **ident):
        return [
            {
                "memory_key": ident["memory_key"],
                "tags": ["t"],
                "embedding": [0.1],
                "embedding_dim": 1,
            }
        ]

    inserted: list[list[dict[str, Any]]] = []

    async def _ins(rows, *, ch=None):
        inserted.append(rows)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", _write)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "live_embedding_rows_for_key", _live)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    fenced: list[tuple[str, int]] = []

    async def _fence(items, *, model, ch=None):
        fenced.extend((key[5], version) for key, version in items)
        return len(items)

    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)

    async def _no_heads(keys, **_k):
        return {}  # a newer write replaced the relabel: no post-relabel retire

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _no_heads)
    ch.rows = [
        {
            "org": "clickhouse",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "memory_key": "khigh",
            "tombstoned_at": None,
        }
    ]
    cands = [
        sweep.SweepCandidate(
            "1", "clickhouse", "n", "a", "-", "-", "khigh", "high", ("credential",)
        ),
        sweep.SweepCandidate("2", "clickhouse", "n", "a", "-", "-", "kmed", "medium", ("email",)),
    ]
    res = await sweep.apply(cands, ch=ch)
    assert res.as_dict() == {
        "relabelled": 2,
        "high": 1,
        "medium": 1,
        "embeddings_tombstoned": 2,
        "embeddings_tagged": 1,
        "errors": 0,
    }
    assert written[0][1]["sensitivity"] == "high" and written[0][1]["tags"] == [
        "t",
        "sensitivity:high",
    ]
    assert written[1][1]["tags"] == ["t", "sensitivity:medium"]
    # One retire of both vector tables at the relabelled memory row's version, so a
    # late metadata copy loses and a newer write's entity vectors stay.
    assert tombstoned == [("khigh", written[0][1]["version"])]
    # and fences the key there: a plain write still embedding lands below it
    assert fenced == [("khigh", written[0][1]["version"])]
    assert inserted[0][0]["tags"] == ["t", "sensitivity:medium"] and inserted[0][0]["version"]


class _LocalCH:
    """Runs each query on clickhouse-local after the setup statements, with their params."""

    def __init__(self, setup: list[str], params: dict[str, Any]) -> None:
        self.setup = setup
        self.params = params
        self.returned: list[dict[str, Any]] = []  # every row a query answered

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        rows = run(self.setup, sql, self.params | (params or {}))
        self.returned.extend(rows)
        return rows

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


# (org, namespace, user_id, memory_id, memory_key, value, age seconds)
_Row = tuple[str, str, str, str, str, Any, int]


def _local_memory(rows: list[_Row]) -> _LocalCH:
    """loom.memory on clickhouse-local holding `rows`, all under agent `a`, session `-`."""
    # Each query reruns the insert, so the timestamps are fixed here rather than `now()`.
    now = int(time.time())
    values, params = [], {}
    for i, (org, ns, user, mid, key, value, age) in enumerate(rows):
        values.append(
            f"({{org{i}:String}}, {{ns{i}:String}}, 'a', {{user{i}:String}}, '-', "
            f"{{id{i}:UUID}}, {{key{i}:String}}, {{value{i}:String}}, toDateTime({{at{i}:UInt32}}))"
        )
        params |= {
            f"org{i}": org,
            f"ns{i}": ns,
            f"user{i}": user,
            f"id{i}": mid,
            f"key{i}": key,
            f"value{i}": json.dumps(value),
            f"at{i}": now - age,
        }
    insert = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        f"memory_key, value, created_at) VALUES {', '.join(values)}"
    )
    # the CLI's closing invariant reads memory_embeddings, whose vector column is quantized
    tables = ["SET enable_quantized_codec = 1", ddl("memory"), ddl("memory_embeddings")]
    return _LocalCH([*tables, insert], params)


def _spin_rows_then_a_pin(spins: int) -> list[_Row]:
    """`spins` selected rows the detector leaves unlabelled, then a row holding a PIN."""
    rows: list[_Row] = [
        ("o", "n", "-", _uuid(i), f"spin_{i}", {"spin": "4471"}, 600 - i) for i in range(spins)
    ]
    rows.append(("o", "n", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60))
    return rows


def _uuid(i: int) -> str:
    return f"00000000-0000-0000-0000-{i:012d}"


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_pages_past_selected_rows_it_leaves_unlabelled(monkeypatch) -> None:
    # More selected-but-unlabelled rows than one page sit ahead of a PIN row: the pass reads
    # on past them and labels the PIN row, instead of re-reading the same first page.
    from server.memory.workers import sensitivity_sweep as w
    from server.settings import settings

    ch = _local_memory(_spin_rows_then_a_pin(5))
    one_page = await sweep.scan(since_days=2, limit=2, ch=ch)  # type: ignore[arg-type]
    assert one_page.truncated and one_page.candidates == []
    applied: list[Any] = []

    async def _apply(cands, *, ch=None):
        applied.extend(cands)
        return sweep.ApplyResult(relabelled=len(cands), high=len(cands))

    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(settings, "sensitivity_sweep_window_days", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_shapes", "credential, email")
    out = await w.run_pass(ch=ch)  # type: ignore[arg-type]
    assert [c.memory_key for c in applied] == ["vault_door"] and applied[0].level == "high"
    assert out["scanned"] == 6 and not out["truncated"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_reads_at_most_its_page_cap() -> None:
    # A row behind more than the cap's worth of selected rows waits for the next pass's
    # window or the operator's run: the scan stops at the cap and says it was truncated.
    ch = _local_memory(_spin_rows_then_a_pin(5))
    capped = await sweep.scan(since_days=2, limit=2, pages=2, ch=ch)  # type: ignore[arg-type]
    assert capped.scanned == 4 and capped.truncated and capped.candidates == []


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_pages_cross_namespace_and_user_boundaries_without_a_gap() -> None:
    # Pages of two rows end inside one user, at a user boundary and at a namespace boundary:
    # each selected row comes back exactly once, and the scan reaches the PIN row at the end.
    spin = {"spin": "4471"}
    rows: list[_Row] = [
        ("o", "n", "-", _uuid(1), "spin_1", spin, 60),
        ("o", "n", "-", _uuid(2), "spin_2", spin, 60),
        ("o", "n", "-", _uuid(3), "spin_3", spin, 60),
        ("o", "n", "u2", _uuid(4), "spin_1", spin, 60),
        ("o", "n", "u2", _uuid(5), "spin_2", spin, 60),
        ("o", "n2", "-", _uuid(6), "spin_1", spin, 60),
        ("o", "n2", "u2", _uuid(7), "spin_1", spin, 60),
        ("o", "n2", "u2", _uuid(8), "vault_door", "badge PIN is 4471", 60),
    ]
    ch = _local_memory(rows)
    report = await sweep.scan(since_days=2, limit=2, pages=None, ch=ch)  # type: ignore[arg-type]
    returned = sorted(r["memory_id"] for r in ch.returned)
    assert returned == sorted(row[3] for row in rows)
    assert [c.memory_key for c in report.candidates] == ["vault_door"]
    assert report.scanned == 8


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_phone_shaped_rows_in_one_org_never_hide_a_credential_in_another(
    monkeypatch,
) -> None:
    # The first org holds more ten-digit runs (epoch seconds) than a pass's pages: the
    # detector reads them as phone numbers, which the scheduled pass leaves alone, so they
    # are not selected, and the token in the next org is labelled.
    from server.memory.workers import sensitivity_sweep as w
    from server.settings import settings

    rows: list[_Row] = [
        ("acme", "n", "-", _uuid(i), f"obs_{i}", {"ts": 1760000000 + i}, 60) for i in range(6)
    ]
    token = "gh" + "p_" + "Ab3" * 12
    rows.append(("zeta", "n", "-", _uuid(99), "deploy_token", f"push with {token}", 60))
    ch = _local_memory(rows)
    applied: list[Any] = []

    async def _apply(cands, *, ch=None):
        applied.extend(cands)
        return sweep.ApplyResult(relabelled=len(cands), high=len(cands))

    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(settings, "sensitivity_sweep_window_days", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_pages_per_pass", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_shapes", "credential,email")
    out = await w.run_pass(ch=ch)  # type: ignore[arg-type]
    assert [(c.org, c.memory_key) for c in applied] == [("zeta", "deploy_token")]
    assert out["scanned"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_cli_sweep_pages_through_its_whole_scope(capsys) -> None:
    # The operator's run reads past more than a page of selected-but-unlabelled rows and
    # reaches the PIN row behind them.
    import argparse

    from server.cli.ops import _sensitivity_sweep_run

    ch = _local_memory(_spin_rows_then_a_pin(5))
    args = argparse.Namespace(
        org="", namespace="", since_days=0, limit=2, only_shape=None, show=5, dry_run=True
    )
    rc = await _sensitivity_sweep_run(ch, args)
    out = capsys.readouterr().out
    assert "6 unlabelled row(s) matched" in out and "detector would label 1: high=1" in out
    assert rc == 1  # a dry run with candidates


@pytest.mark.asyncio
async def test_the_worker_sweeps_the_trailing_window_only(monkeypatch) -> None:
    from server.memory.workers import sensitivity_sweep as w
    from server.settings import settings

    seen: dict[str, Any] = {}

    async def _scan(**kw):
        seen.update(kw)
        return sweep.SweepReport(scanned=3)

    async def _apply(cands, *, ch=None):
        return sweep.ApplyResult()

    monkeypatch.setattr(sweep, "scan", _scan)
    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(settings, "sensitivity_sweep_window_days", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", 500)
    monkeypatch.setattr(settings, "sensitivity_sweep_pages_per_pass", 3)
    monkeypatch.setattr(settings, "sensitivity_sweep_shapes", "credential, email")
    out = await w.run_pass(ch=_CH())  # type: ignore[arg-type]
    assert seen["since_days"] == 2 and seen["limit"] == 500 and seen["pages"] == 3
    assert seen["only_shapes"] == ["credential", "email"]  # the phone shape is the operator's
    assert out["scanned"] == 3 and out["relabelled"] == 0


@pytest.mark.parametrize(
    "field", ["sensitivity_sweep_max_rows_per_pass", "sensitivity_sweep_pages_per_pass"]
)
def test_a_sweep_pass_bound_below_one_is_refused(field: str) -> None:
    from pydantic import ValidationError

    from server.settings.catalog import MemorySettings

    with pytest.raises(ValidationError):
        MemorySettings(**{field: 0})


def test_the_worker_is_registered_and_described() -> None:
    from server.workers.registry import worker_meta

    assert worker_meta()["sensitivity_sweep"]["interval_seconds"] == 86400


@pytest.mark.asyncio
async def test_the_missing_embedding_reconcile_skips_high_rows() -> None:
    """The embedding worker's reconcile must not re-embed a `high` row after
    the sweep tombstoned its vector (a high-sensitivity row has no embedding
    row). Both phases exclude the tier."""
    from server.memory import store as memory_repo

    ch = _CH(
        [
            {
                "org": "o",
                "namespace": "n",
                "agent": "a",
                "user_id": "-",
                "session_id": "-",
                "memory_key": "k",
            }
        ]
    )
    await memory_repo.find_memories_without_embedding_rows(batch_size=5, since_seconds=600, ch=ch)
    assert len(ch.calls) == 2
    for sql, _ in ch.calls:
        assert "m.sensitivity != 'high'" in sql and "m.tombstoned_at IS NULL" in sql
    # the stub takes the memory row's version, so a high write after this read fences it
    assert "m.version AS version" in ch.calls[1][0]


# ------------------------------------------------------------- (c) gauges


@pytest.mark.asyncio
async def test_integrity_counts_and_the_dim0_repair(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.store import integrity as repo

    ch = _CH()
    ch.one = {"dim0_with_vector": 156, "tombstone_status_drift": 1957, "active_past_valid_to": 23}
    assert await repo.integrity_counts(ch=ch) == {
        "dim0_with_vector": 156,
        "tombstone_status_drift": 1957,
        "active_past_valid_to": 23,
        "high_with_embedding": 0,
    }
    assert "embedding_dim = 0 AND length(embedding) > 0" in ch.calls[0][0]
    inserted: list[list[dict[str, Any]]] = []

    async def _ins(rows, *, ch=None):
        inserted.append(rows)

    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    rows = [
        {
            "org": "dev",
            "namespace": "td-1",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "embedding": [0.5] * 4,
            "embedding_dim": 0,
            "version": 7,
            "content_excerpt": "x",
            "content_excerpt_lc": "x",
            "created_at": None,
        },
        {
            "org": "dev",
            "namespace": "td-1",
            "memory_key": "empty",
            "embedding": [],
            "embedding_dim": 0,
        },
    ]
    assert await repo.repair_dim0_rows(rows, ch=ch, dry_run=True) == 1 and not inserted
    assert await repo.repair_dim0_rows(rows, ch=ch) == 1
    out = inserted[0][0]
    assert out["embedding_dim"] == 4 and out["embedding"] == [0.5] * 4
    assert out["version"] != 7 and "content_excerpt_lc" not in out  # MATERIALIZED, never inserted


def test_the_probe_reads_the_three_new_keys_from_the_shared_sql() -> None:
    from server.memory.store import integrity as repo
    from server.observability import otel_probes

    captured: list[str] = []

    class _Rows:
        # mutations_pending leads the census row.
        result_rows = [(0, 0, 0, 0, 0, 0, 0, 0, 156, 1957, 23, 1)]

    class _Sync:
        def query(self, sql: str) -> _Rows:
            captured.append(sql)
            return _Rows()

    import unittest.mock as um

    with um.patch.object(otel_probes, "_probe_client", lambda: _Sync()):
        out = otel_probes._probe_invariants()
    assert out["dim0_with_vector"] == 156 and out["tombstone_status_drift"] == 1957
    assert out["active_past_valid_to"] == 23 and out["high_with_embedding"] == 1
    # One pass computes all four; the integration equivalence test holds each
    # equal to its shared statement (`repo.*_SQL`, what `integrity-repair` runs).
    assert repo.GAUGES and all(f"AS {g}" in captured[0] for g in repo.GAUGES)
    assert "sensitivity = 'high'" in captured[0] and "embedding_dim = 0" in captured[0]


@pytest.mark.asyncio
async def test_integrity_repair_cli_dry_run_lists_and_apply_reemits(monkeypatch) -> None:
    from server.cli import ops as cli_ops
    from server.memory.store import integrity as repo

    calls: list[Any] = []
    counts = {
        "dim0_with_vector": 2,
        "tombstone_status_drift": 0,
        "active_past_valid_to": 0,
        "high_with_embedding": 0,
    }

    async def _counts(*, ch=None):
        return dict(counts)

    async def _rows(*, limit, org=None, namespace=None, ch=None):
        calls.append(("rows", org, namespace, limit))
        return [{"org": "dev", "namespace": "td-1"}, {"org": "dev", "namespace": "td-1"}]

    async def _drift(*, limit, org=None, namespace=None, ch=None):
        return []

    async def _repair(rows, *, ch=None, dry_run=False):
        calls.append(("repair", len(rows)))
        counts["dim0_with_vector"] = 0
        return len(rows)

    monkeypatch.setattr(repo, "integrity_counts", _counts)
    monkeypatch.setattr(repo, "dim0_with_vector_rows", _rows)
    monkeypatch.setattr(repo, "drift_sample", _drift)
    monkeypatch.setattr(repo, "repair_dim0_rows", _repair)
    args = SimpleNamespace(org="dev", namespace="td-1", limit=10, show=5, dry_run=True)
    assert await cli_ops._integrity_repair_run(object(), args) == 1  # dry run with rows → non-zero
    assert calls == [("rows", "dev", "td-1", 10)]
    args.dry_run = False
    assert await cli_ops._integrity_repair_run(object(), args) == 0
    assert calls[-1] == ("repair", 2)


@pytest.mark.asyncio
async def test_sensitivity_sweep_cli_gates_on_the_high_sensitivity_invariant(monkeypatch) -> None:
    from server.cli import ops as cli_ops

    state = {"high_live": 1}

    async def _scan(**kw):
        return sweep.SweepReport(
            scanned=1,
            candidates=[
                sweep.SweepCandidate("1", "o", "n", "a", "-", "-", "k", "high", ("credential",))
            ],
        )

    async def _apply(cands, *, ch=None):
        state["high_live"] = 0
        return sweep.ApplyResult(relabelled=1, high=1, embeddings_tombstoned=1)

    async def _high(*, org=None, namespace=None, ch=None):
        return state["high_live"]

    monkeypatch.setattr(sweep, "scan", _scan)
    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(sweep, "high_with_embedding_count", _high)
    args = SimpleNamespace(
        org="o", namespace="n", since_days=0, limit=100, show=5, dry_run=True, only_shape=None
    )
    assert (
        await cli_ops._sensitivity_sweep_run(object(), args) == 1
    )  # dry run, candidates, invariant red
    args.dry_run = False
    assert await cli_ops._sensitivity_sweep_run(object(), args) == 0  # applied, invariant 0


@pytest.mark.asyncio
async def test_a_failed_high_retire_leaves_the_row_unlabelled_for_the_next_pass(
    monkeypatch,
) -> None:
    """The vectors and chunks go before the label: the scan reads only unlabelled
    rows, so a relabel written ahead of a failed retire would never be retried."""
    from server.memory import store as memory_repo

    written: list[str] = []

    async def _write(org, ids, update, *, ch=None):
        written.extend(ids)
        return 1

    async def _retire(items, *, ch=None):
        raise RuntimeError("insert timed out")

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", _write)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    cand = sweep.SweepCandidate("1", "o", "n", "a", "-", "-", "k", "high", ("credential",))
    res = await sweep.apply([cand], ch=_CH())
    assert written == [] and res.relabelled == 0
    assert len(res.errors) == 1 and "insert timed out" in res.errors[0]


@pytest.mark.asyncio
async def test_a_high_relabel_retires_the_keys_chunks_again_after_the_label(
    monkeypatch,
) -> None:
    """A chunk writer re-checks the head after its insert: chunks that landed between
    the fence and the relabel saw an unlabelled head, so the sweep retires the key's
    chunks once more after the relabel is in."""
    from server.memory import store as memory_repo
    from server.memory.app import high_fence

    order: list[str] = []

    async def _write(org, ids, update, *, ch=None):
        order.append("relabel")
        return 1

    async def _retire(items, *, ch=None):
        order.append("retire")
        return 1

    async def _fence(items, *, model, ch=None):
        order.append("fence")
        return 1

    async def _chunks(**_k):
        order.append("chunks")
        return 0

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", _write)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _chunks)
    relabel_version: list[int] = []
    real_next = memory_repo.next_version

    def _next() -> int:
        v = real_next()
        relabel_version.append(v)
        return v

    async def _heads(keys, **_k):
        from types import SimpleNamespace

        return {keys[0]: SimpleNamespace(version=relabel_version[0])}  # the relabel is head

    monkeypatch.setattr(memory_repo, "next_version", _next)
    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _heads)
    cand = sweep.SweepCandidate("1", "o", "n", "a", "-", "-", "k", "high", ("credential",))
    res = await sweep.apply([cand], ch=_CH())
    assert not res.errors
    assert order == ["retire", "fence", "chunks", "relabel", "chunks", "retire"]


@pytest.mark.asyncio
async def test_the_medium_tag_copy_rechecks_its_head(monkeypatch) -> None:
    """The medium tag re-emit is a vector copy like touch and verify: after its insert
    it runs the post-copy head re-check, which retires it under a head made 'high'."""
    from server.memory import store as memory_repo

    checked: list[list[dict[str, Any]]] = []

    async def _live(ch, **ident):
        return [{**ident, "tags": ["t"], "embedding": [0.1], "embedding_dim": 1, "version": 3}]

    async def _ins(rows, *, ch=None):
        return None

    async def _check(rows, *, ch=None):
        checked.append(rows)
        return 0

    monkeypatch.setattr(memory_repo, "live_embedding_rows_for_key", _live)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    monkeypatch.setattr(memory_repo, "retire_copies_under_unembedded_heads", _check)
    cand = sweep.SweepCandidate("2", "o", "n", "a", "-", "-", "k", "medium", ("email",))
    assert await sweep._tag_embeddings(cand, ch=_CH()) == 1
    assert len(checked) == 1 and checked[0][0]["memory_key"] == "k"
