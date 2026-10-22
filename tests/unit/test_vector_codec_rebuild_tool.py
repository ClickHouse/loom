"""scripts/ops/vector_codec_rebuild.py against a fake ClickHouse: nothing
changes without --yes, the twin is the run's lock, a failed gate never swaps,
a swap leaves the rollback copy, and --rollback / --drop-pre-rebuild behave as
the sort-key tool's do."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from scripts.ops import vector_codec_rebuild as vcr
from server.core.schema import table_rebuild as tr
from server.core.schema.vector_codec_ddl import CODEC_SETTINGS_SQL
from tests.unit.codec_states import SENT, STATES, USABLE, codec_rows, settings

ARRAY = """CREATE TABLE loom.memory_chunks
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `embedding` Array(BFloat16) CODEC(NONE),
    `created_at` DateTime
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (org, namespace)
SETTINGS index_granularity = 256"""
DIM = 1536
DRAINED_READ = f"SELECT hostName(), ({vcr.REPLICA_SYNCED_SQL}), groupArray(tuple(*)) FROM ("
TWIN_CREATED, COPY_STARTED = 1_600_000_000, 1_700_000_000  # server Unix times


class _R:
    def __init__(self, rows: list[Any]) -> None:
        self.result_rows = rows


class _CH:
    def __init__(self, **tables: dict[str, Any]) -> None:
        self.t = tables
        self.commands: list[str] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []  # (statement, its settings)
        self.patches: dict[str, int] = {}
        self.flushed: list[int] = []  # rows each async-queue flush lands, in order
        self.refuse_system = False  # this user lacks the SYSTEM grants
        self.refuse_table = ""  # SYSTEM statements naming this table are refused
        self.uuids = iter(f"u{i}" for i in range(100))
        self.codec_rows = codec_rows("writable", "writable")
        self.delta = 0  # rows a delta finds above the destination's version
        self.widths: list[tuple[int, int]] = []  # (width, rows) the width preflight finds
        self.widths_first: list[list[tuple[int, int]]] = []  # earlier reads, before self.widths
        self.cons = (0, 0, 0)  # (mismatched, source_only, twin_only) the gate reads
        self.cons_first: list[tuple[int, int, int]] = []  # earlier reads, before self.cons
        self.newer = (0, 0)  # mismatched keys newer in the first table, in the second
        self.all_widths = [(DIM, 5)]  # (width, rows) of every real vector, most first
        self.mutations = 0
        self.mutations_first: list[int] = []
        self.tied = 0  # keys with more than one row at their newest version
        self.queries: list[str] = []
        self.sequence: list[str] = []  # every query and command, in order
        self.query_settings: list[tuple[str, dict[str, Any]]] = []
        self.params: list[dict[str, Any]] = []  # each query's parameters, in order
        self.behind: set[str] = set()  # tables the replica answering a read has not caught up

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> _R:
        self.queries.append(sql)
        self.sequence.append(sql)
        self.query_settings.append((sql, dict(settings or {})))
        self.params.append(dict(parameters or {}))
        return self._answer(sql, parameters or {})

    def _answer(self, sql: str, p: dict[str, Any]) -> _R:
        if sql.startswith(DRAINED_READ):
            inner = sql.removeprefix(DRAINED_READ).removesuffix(")")
            synced = not self.behind & set(p["tables"])
            return _R([("h2", synced, self._answer(inner, p).result_rows)])
        if sql == CODEC_SETTINGS_SQL:
            return _R(self.codec_rows)
        if "toUnixTimestamp(metadata_modification_time)" in sql:
            return _R([(TWIN_CREATED,)])  # a reused twin's creation
        if sql == "SELECT toUnixTimestamp(now())":
            return _R([(COPY_STARTED,)])
        if sql.startswith("SHOW CREATE TABLE loom."):
            return _R([(self.t[sql.split("loom.", 1)[1]]["ddl"],)])
        if "FROM system.tables" in sql:
            row = self.t.get(str(p.get("t")))
            return _R([("ReplacingMergeTree", row["uuid"], "2000-01-01")] if row else [])
        if "system.one" in sql:
            return _R([("h", "26.8.8.8")])
        if "system.replicas" in sql or "compression_codec FROM" in sql:
            return _R([])
        if "version(), hostName()" in sql:
            return _R([("26.8.8.8", "h")])
        if "FROM system.columns" in sql:
            return _R(
                [(c, "T", "", "", "") for c in ("org", "namespace", "embedding", "created_at")]
            )
        if "startsWith(name, 'patch')" in sql:
            name = re.search(r"table = '(\w+)'", sql).group(1)  # type: ignore[union-attr]
            return _R([(self.patches.get(name, 0),)])
        if "ORDER BY n DESC" in sql:
            return _R(self.all_widths)
        if "INNER JOIN" in sql:
            return _R([self.newer])
        if "n_at_max" in sql:
            return _R([(self.tied,)])
        if "FULL OUTER JOIN" in sql:
            return _R([self.cons_first.pop(0) if self.cons_first else self.cons])
        if "GROUP BY w" in sql:
            return _R(self.widths_first.pop(0) if self.widths_first else self.widths)
        if "system.mutations" in sql:
            return _R([(self.mutations_first.pop(0) if self.mutations_first else self.mutations,)])
        if "ORDER BY rand()" in sql:
            return _R([("o", "n", [0.5] * DIM)])
        if "round(cosineDistance" in sql:
            return _R([(0.1,)] * 20)
        if "INTERVAL 1 HOUR" in sql:
            return _R([(0,)])
        name = sql.split("FROM loom.", 1)[1].split()[0]
        rows = self.t[name]["rows"]
        if "max(" in sql:
            return _R([(rows,)])
        if "WHERE created_at >" in sql:
            return _R([(self.delta,)])
        return _R([(rows,)])

    def command(self, stmt: str, parameters: Any = None, settings: Any = None) -> None:
        s = " ".join(stmt.split())
        self.commands.append(s)
        self.sequence.append(s)
        self.sent.append((s, dict(settings or {})))
        refused = self.refuse_system or (self.refuse_table and self.refuse_table in s.split())
        if refused and s.startswith("SYSTEM"):
            raise RuntimeError("Not enough privileges")
        w = s.replace("loom.", "").split()
        if s.startswith("CREATE TABLE"):
            self.t[w[2]] = {"ddl": stmt, "rows": 0, "uuid": next(self.uuids)}
        elif s.startswith("DROP TABLE"):
            self.t.pop(w[2])
        elif s.startswith("EXCHANGE TABLES"):
            self.t[w[2]], self.t[w[4]] = self.t[w[4]], self.t[w[2]]
        elif s.startswith("SYSTEM FLUSH ASYNC INSERT QUEUE") and self.flushed:
            self.t[w[5]]["rows"] += self.flushed.pop(0)
        elif s.startswith("RENAME TABLE"):
            self.t[w[4]] = self.t.pop(w[2])
        elif s.startswith("INSERT INTO") and "WHERE" not in s:
            self.t[w[2]]["rows"] = self.t[s.split("FROM loom.", 1)[1].split()[0]]["rows"]


def _run(ch: _CH, *flags: str) -> int:
    return vcr.main(
        ["--table", "memory_chunks", "--columns", "embedding", "--settle-s", "0", *flags], ch=ch
    )


def _writes(ch: _CH) -> list[str]:
    return [c for c in ch.commands if not c.startswith("SYSTEM")]


def _last(ch: _CH, needle: str) -> int:
    """The index of the last recorded query that contains `needle`."""
    return max(i for i, q in enumerate(ch.queries) if needle in q)


def _live(rows: int = 5) -> dict[str, Any]:
    return {"ddl": ARRAY, "rows": rows, "uuid": "live"}


def test_the_dimension_comes_from_the_live_table() -> None:
    coded = ARRAY.replace("CODEC(NONE)", "CODEC(Quantized('rabitq', 768, 0))")
    assert vcr.vector_dim(coded, "embedding") == 768
    assert vcr.vector_dim(ARRAY, "embedding") is None
    job = vcr.VectorRebuild(
        _CH(memory_chunks=_live()),
        vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]),
    )
    assert job.find_dim() == DIM  # a plain column: the vectors' own length
    read = next(q for q in job.ch.queries if "ORDER BY n DESC" in q)
    assert "FROM loom.memory_chunks FINAL" in read  # the width of the rows Loom serves


def test_without_yes_nothing_changes_and_the_plan_names_the_dimension(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    assert _run(ch) == tr.UNCONFIRMED and _writes(ch) == []
    out = capsys.readouterr().out
    assert f"CODEC(Quantized('rabitq', {DIM}, 0))" in out and '"exit": 4' in out


def test_a_confirmed_rebuild_swaps_and_keeps_the_rollback_copy() -> None:
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    assert "Quantized('rabitq', 1536, 0)" in ch.t["memory_chunks"]["ddl"]
    assert ch.t["memory_chunks_pre_rebuild"]["ddl"] == ARRAY
    assert "memory_chunks_rebuild" not in ch.t
    kinds = [c.split()[0] for c in _writes(ch)]
    assert kinds[0] == "CREATE" and kinds.count("EXCHANGE") == 1 and "RENAME" in kinds
    # already on the target: a second run changes nothing
    ch.commands.clear()
    ch.t.pop("memory_chunks_pre_rebuild")
    assert _run(ch, "--yes") == tr.OK and _writes(ch) == []


def test_a_twin_left_behind_is_a_lock_until_reuse_or_restart(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild={"ddl": ARRAY, "rows": 5, "uuid": "x"})
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    assert "another run's lock" in capsys.readouterr().out
    assert _run(ch, "--yes", "--restart") == tr.OK
    assert _writes(ch)[0] == "DROP TABLE loom.memory_chunks_rebuild SYNC"


def test_a_twin_with_patch_parts_is_never_swapped() -> None:
    ch = _CH(memory_chunks=_live())
    ch.patches["memory_chunks_rebuild"] = 2
    assert _run(ch, "--yes", "--require-patch-free") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_rebuild" in ch.t


def test_a_twin_replaced_by_another_run_is_never_swapped(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(memory_chunks=_live())
    real = vcr.VectorRebuild.recall

    def recall(job: vcr.VectorRebuild, ddl: str) -> list[float]:
        ch.t["memory_chunks_rebuild"]["uuid"] = "someone-else"
        return real(job, ddl)

    monkeypatch.setattr(vcr.VectorRebuild, "recall", recall)
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)


def test_rollback_and_drop_need_yes_and_check_their_result() -> None:
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    assert _run(ch, "--rollback") == tr.UNCONFIRMED and _writes(ch) == []
    assert _run(ch, "--rollback", "--yes") == tr.OK
    assert ch.t["memory_chunks"]["ddl"] == ARRAY
    assert _run(ch, "--drop-pre-rebuild") == tr.UNCONFIRMED
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    assert "memory_chunks_pre_rebuild" not in ch.t


def test_an_existing_rollback_copy_refuses_a_new_rebuild(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live(), memory_chunks_pre_rebuild=_live())
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    out = capsys.readouterr().out
    assert "--drop-pre-rebuild" in out and "summary {" in out


def test_the_replica_checks_read_the_named_cluster_or_this_server() -> None:
    assert vcr.replicas_source("default")("one") == "clusterAllReplicas('default', system.one)"
    assert vcr.replicas_source("prod_c1")("replicas") == (
        "clusterAllReplicas('prod_c1', system.replicas)"
    )
    assert vcr.replicas_source("")("replicas") == "system.replicas"
    base = ["--table", "memory_chunks", "--columns", "embedding"]
    assert vcr.parse(base).cluster == "default"
    with pytest.raises(SystemExit):
        vcr.parse([*base, "--cluster", "x') , system.one --"])


def test_a_coded_table_at_another_granularity_is_not_on_target() -> None:
    """A table already coded still moves when the run asks for smaller granules: the
    exact rescore reads one granule per key, so the granule size is part of the layout."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    args = (["embedding"], None, "rabitq", DIM)
    assert vcr.on_target(coded, *args)
    assert vcr.on_target(coded, *args, 256)
    assert not vcr.on_target(coded, *args, 64)


def test_a_granularity_rebuild_reports_and_checks_the_granularity(capsys: Any) -> None:
    """The plan, the post-swap check and a second run all judge the granule size the
    run asked for, not only the codec."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(memory_chunks={"ddl": coded, "rows": 5, "uuid": "live"})
    job = vcr.VectorRebuild(
        ch,
        vcr.parse(
            ["--table", "memory_chunks", "--columns", "embedding", "--index-granularity", "64"]
        ),
    )
    assert job.target_state("memory_chunks") is False
    assert _run(ch, "--index-granularity", "64", "--yes") == tr.OK
    assert not any("GROUP BY w" in q for q in ch.queries)  # the codec holds one width
    assert "index_granularity = 64" in ch.t["memory_chunks"]["ddl"]
    assert job.target_state("memory_chunks") is True
    ch.commands.clear()
    ch.t.pop("memory_chunks_pre_rebuild")
    assert _run(ch, "--index-granularity", "64", "--yes") == tr.OK and _writes(ch) == []


def test_a_repair_names_the_diverged_keys_as_literals() -> None:
    """The keys come back to the client and each INSERT lists them: an `IN (subquery)`
    INSERT runs the subquery on other replicas, which a user without cluster-wide
    grants is refused."""
    import uuid

    assert vcr.key_literal(uuid.UUID(int=1)) == "toUUID('00000000-0000-0000-0000-000000000001')"
    assert vcr.key_literal("o'k") == "'o\\'k'" and vcr.key_literal(7) == "7"
    job = vcr.VectorRebuild(
        _CH(memory_chunks=_live()),
        vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]),
    )
    assert job.codec == settings()  # read before `q` below answers only keys
    cmds: list[str] = []
    job.q = lambda sql, **_: [("o", "n")] * (vcr.REPAIR_BATCH + 1)  # type: ignore[method-assign]
    job.cmd = lambda sql, **_: cmds.append(sql)  # type: ignore[method-assign]
    n = job.repair_into("SELECT *", "created_at", ["org", "namespace"])
    assert n == vcr.REPAIR_BATCH + 1 and len(cmds) == 2
    assert all("IN (('o', 'n')" in c and "SELECT" not in c.split(" IN ", 1)[1] for c in cmds)


@pytest.mark.parametrize("codec", STATES)
def test_the_rebuild_runs_only_where_the_codec_can_be_on(codec: str, capsys: Any) -> None:
    """Readonly at 0 or absent refuses before any write; readonly at 1 is on already
    and is sent with no statement, the copy and the deltas included."""
    ch = _CH(memory_chunks=_live())
    ch.codec_rows, ch.delta = codec_rows(codec, "writable"), 1
    if not USABLE[codec]:
        assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
        assert "enable_quantized_codec is absent (before 26.8) or held at 0" in (
            capsys.readouterr().out
        )
        return
    assert _run(ch, "--yes") == tr.OK
    create = next(st for s, st in ch.sent if s.startswith("CREATE"))
    assert create == ({"enable_quantized_codec": 1} if SENT[codec] else {})
    copies = [st for s, st in ch.sent if s.startswith("INSERT")]
    assert len(copies) > 1  # the copy and the deltas
    assert all(("enable_quantized_codec" in st) is SENT[codec] for st in copies)


@pytest.mark.parametrize("chain", STATES)
def test_a_chain_runs_only_where_suspicious_codecs_can_be_on(chain: str, capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    ch.codec_rows = codec_rows("on", chain)
    if not USABLE[chain]:
        assert _run(ch, "--chain", "LZ4", "--yes") == tr.REFUSED and _writes(ch) == []
        assert "--chain LZ4 needs allow_suspicious_codecs" in capsys.readouterr().out
        assert _run(ch, "--yes") == tr.OK  # the bare codec still moves
        return
    assert _run(ch, "--chain", "LZ4", "--yes") == tr.OK
    create = next(st for s, st in ch.sent if s.startswith("CREATE"))
    assert create == ({"allow_suspicious_codecs": 1} if SENT[chain] else {})
    assert f"CODEC(Quantized('rabitq', {DIM}, 0), LZ4)" in ch.t["memory_chunks"]["ddl"]


@pytest.mark.parametrize("codec", STATES)
def test_a_rollback_sends_the_codec_setting_only_where_it_is_writable(codec: str) -> None:
    """The way back to the plain layout works wherever the setting stands now, and its
    catch-up copy runs under the full copy's limit, not the gate reads' timeout."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    ch.codec_rows, ch.delta = codec_rows(codec, "off"), 2
    assert _run(ch, "--rollback", "--yes") == tr.OK
    copies = [st for s, st in ch.sent if s.startswith("INSERT")]
    assert copies and all(
        st
        == {
            "max_execution_time": vcr.COPY_EXECUTION_TIME_S,
            **({"enable_quantized_codec": 1} if SENT[codec] else {}),
        }
        for st in copies
    )


def test_vectors_of_another_width_refuse_before_the_twin_dry_run_included(capsys: Any) -> None:
    """The copy turns a vector of another length into a zero vector, so the run refuses
    while the rows Loom serves hold one; an empty vector stays a placeholder."""
    ch = _CH(memory_chunks=_live())
    ch.widths = [(768, 3), (3072, 1)]
    assert _run(ch, "--dry-run") == tr.REFUSED and _writes(ch) == []
    out = capsys.readouterr().out
    assert f"embedding holds vectors of more than one width (target {DIM};" in out
    assert "LOOM_EMBEDDING_DIM" in out and "loom reembed --delete-old" in out
    assert "width 768: 3 row(s), width 3072: 1 row(s)" in out and "re-embed" in out
    preflight = [q for q in ch.queries if "GROUP BY w" in q]
    assert preflight and all(
        " FINAL " in q and f"length(embedding) != {DIM}" in q for q in preflight
    )
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    ch.widths = []
    assert _run(ch, "--yes") == tr.OK


def test_rows_only_the_twin_holds_fail_the_gate(capsys: Any) -> None:
    """A key erased from the source during the copy is still in the twin; the swap would
    bring it back, so the run stops before the EXCHANGE and leaves the twin. Such a key
    counts as twin-only and not as mismatched (the SQL is pinned in the runner tests)."""
    ch = _CH(memory_chunks=_live())
    ch.cons = (0, 0, 2)
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "2 key(s) only in the twin" in out
    assert "removed from the source after they were copied" in out and "--restart" in out


def test_a_delete_during_the_gates_stops_the_swap(capsys: Any) -> None:
    """The twin-only check runs again after the last delta: a row erased from the source
    while the recall gate ran fails the gate instead of coming back at the swap."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(0, 0, 0)]
    ch.cons = (0, 0, 1)
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "1 key(s) only in the twin" in capsys.readouterr().out
    delta = _last(ch, "SELECT count() FROM loom.memory_chunks WHERE created_at >")
    assert delta < _last(ch, "FULL OUTER JOIN") and delta < _last(ch, "GROUP BY w")


def test_a_key_that_changed_during_the_gates_stops_the_swap(capsys: Any) -> None:
    """A delete of a key's newest version leaves the source serving an older one while
    the twin keeps the deleted row: the key reads as mismatched before the swap, and
    the run stops instead of serving it."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(0, 0, 0)]
    ch.cons = (1, 0, 0)
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "the source changed during the gates" in out and "--restart" in out


def test_the_statement_timeout_caps_the_gate_reads_and_stays_within_the_read_timeout() -> None:
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(1, 0, 0)]
    assert _run(ch, "--statement-timeout-s", "60", "--yes") == tr.OK
    repairs = [st for s, st in ch.sent if "WHERE (org, namespace) IN" in s]
    assert repairs and all(st["max_execution_time"] == 60 for st in repairs)
    for needle in (
        "system.mutations",
        "GROUP BY w",
        "FULL OUTER JOIN",
        "LEFT JOIN",
        "ORDER BY n DESC",
        "round(cosineDistance",
        "ORDER BY rand()",
        "SELECT max(created_at)",
    ):
        reads = [st for q, st in ch.query_settings if needle in q]
        assert reads and all(st["max_execution_time"] == 60 for st in reads), needle
    for bad in ("0", "-1", "nan", "inf", str(vcr.COPY_RECEIVE_TIMEOUT_S)):
        with pytest.raises(SystemExit):
            vcr.parse(["--table", "t", "--columns", "embedding", "--statement-timeout-s", bad])


def test_an_off_width_write_during_the_run_stops_the_swap(capsys: Any) -> None:
    """The width check runs again before the swap: a row of another width written after
    the preflight was zeroed in the twin, so the gate fails instead of serving it."""
    ch = _CH(memory_chunks=_live())
    ch.widths_first = [[]]
    ch.widths = [(768, 2)]
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "GATE FAILED: embedding holds vectors of more than one width" in out
    assert "width 768: 2 row(s)" in out
    delta = _last(ch, "SELECT count() FROM loom.memory_chunks WHERE created_at >")
    assert delta < _last(ch, "GROUP BY w")


def test_an_update_of_a_rebuilt_vector_stops_the_swap(capsys: Any) -> None:
    """An UPDATE of the vector keeps the key and its version, and a merge can apply its
    patch; the hash covers the vector as the twin stores it, so the key reads as
    mismatched and the run stops."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(0, 0, 0)]
    ch.cons = (1, 0, 0)  # the key and its version match; the hash does not
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "the source changed during the gates" in capsys.readouterr().out
    reads = [q for q in ch.queries if "FULL OUTER JOIN" in q]
    assert reads and all("s.__hash != t.__hash" in q for q in reads)
    assert all(vcr.stored_vector("embedding", dim=DIM) in q for q in reads)


def test_rows_flushed_into_the_source_after_the_checks_fail_the_postcondition(
    capsys: Any,
) -> None:
    """The swap's own async-queue flush lands a row after every check: the rollback copy
    then holds a key the new table lacks, and the run says so and names --rollback."""
    ch = _CH(memory_chunks=_live())
    ch.flushed = [0, 0, 1]  # the preflight's drain, the drain before the last delta, the swap's
    ch.cons_first = [(0, 0, 0), (0, 0, 0)]  # the two gate reads before the swap
    ch.cons = (0, 1, 0)  # the flushed key, only in the rollback copy
    assert _run(ch, "--yes") == tr.POSTCONDITION
    assert any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "the source changed after the last check" in out
    assert "--rollback --yes" in out
    assert not any(c.startswith("INSERT INTO loom.memory_chunks ") for c in ch.commands)


def test_the_queue_and_the_replica_are_drained_before_the_last_delta() -> None:
    ddl = ARRAY.replace(
        "ReplacingMergeTree(created_at)",
        "ReplicatedReplacingMergeTree('/clickhouse/tables/{uuid}', '{replica}', created_at)",
    )
    ch = _CH(memory_chunks={"ddl": ddl, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    seq = ch.sequence
    recall = max(i for i, q in enumerate(seq) if "round(cosineDistance" in q)
    flush = seq.index("SYSTEM FLUSH ASYNC INSERT QUEUE loom.memory_chunks", recall)
    sync = seq.index("SYSTEM SYNC REPLICA loom.memory_chunks", recall)
    delta = max(i for i, q in enumerate(seq) if "FROM loom.memory_chunks WHERE created_at >" in q)
    assert recall < flush < sync < delta


def test_the_reads_after_the_drain_are_sequentially_consistent() -> None:
    """Enable sequential consistency for gate reads and delta inserts after the drain."""
    ch = _CH(memory_chunks=_live())
    ch.delta = 1
    assert _run(ch, "--yes") == tr.OK
    recall = max(i for i, (q, _) in enumerate(ch.query_settings) if "round(cosineDistance" in q)
    before, after = ch.query_settings[:recall], ch.query_settings[recall + 1 :]
    for needle in ("FULL OUTER JOIN", "GROUP BY w", "system.mutations"):
        reads = [st for q, st in before if needle in q]
        assert reads and all("select_sequential_consistency" not in st for st in reads), needle
    for needle in ("FULL OUTER JOIN", "GROUP BY w", "system.mutations"):
        reads = [st for q, st in after if needle in q]
        assert reads and all(st["select_sequential_consistency"] == 1 for st in reads), needle
    delta = [st for s, st in ch.sent if s.startswith("INSERT") and "created_at >" in s][-1]
    assert delta["select_sequential_consistency"] == 1


def test_a_user_without_the_drain_grants_is_refused_before_the_copy(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    ch.refuse_system = True
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    out = capsys.readouterr().out
    assert "SYSTEM FLUSH ASYNC INSERT QUEUE and SYSTEM SYNC REPLICA" in out


def test_an_in_place_change_after_the_checks_keeps_both_tables_without_a_rollback(
    capsys: Any,
) -> None:
    """A lightweight UPDATE after the last check keeps the row count and the version; the
    row hash of the rollback copy against the new table shows it, whether or not a merge
    has moved its patch part. Either table may hold it, and --rollback copies back only
    newer versions and absent keys, so the next step is inspection, not --rollback."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(0, 0, 0), (0, 0, 0)]  # the two gate reads before the swap
    ch.cons = (1, 0, 0)
    assert _run(ch, "--yes") == tr.POSTCONDITION
    out = capsys.readouterr().out
    assert "1 with other content at one version" in out and "inspect the difference" in out
    assert "--rollback --yes" not in json.loads(out.rsplit("summary ", 1)[1])["next"]
    last = ch.queries[_last(ch, "FULL OUTER JOIN")]
    src, twin = last.split("FULL OUTER JOIN")
    assert "FROM loom.memory_chunks_pre_rebuild FINAL" in src
    assert "FROM loom.memory_chunks FINAL" in twin
    assert vcr.stored_vector("embedding", dim=DIM) in src


def test_an_off_width_update_after_the_checks_fails_the_postcondition(capsys: Any) -> None:
    """An UPDATE that turns a zero placeholder into a vector of another width hashes as
    zeros on both sides; the width read of the rollback copy shows it."""
    ch = _CH(memory_chunks=_live())
    ch.widths_first = [[], []]  # the preflight and the gate before the swap
    ch.widths = [(768, 1)]
    assert _run(ch, "--yes") == tr.POSTCONDITION
    out = capsys.readouterr().out
    assert "POSTCONDITION FAILED: embedding holds vectors of more than one width" in out
    assert "--rollback --yes" in out
    assert "FROM loom.memory_chunks_pre_rebuild" in ch.queries[_last(ch, "GROUP BY w")]


def test_a_mutation_after_the_checks_fails_the_postcondition(capsys: Any) -> None:
    """A mutation created after the gate's last mutation check follows the rename onto the
    rollback copy; the new table may not hold it."""
    ch = _CH(memory_chunks=_live())
    ch.mutations_first = [0, 0]  # the preflight and the gate before the swap
    ch.mutations = 1
    assert _run(ch, "--yes") == tr.POSTCONDITION
    assert "1 mutation(s) on loom.memory_chunks_pre_rebuild" in capsys.readouterr().out
    last = _last(ch, "system.mutations")
    assert "table = {t:String}" in ch.queries[last]
    assert ch.params[last]["t"] == "memory_chunks_pre_rebuild"


def test_a_duplicate_key_insert_on_a_plain_merge_tree_stops_the_swap(capsys: Any) -> None:
    """Without a version column a re-ingest adds rows under keys the twin already holds,
    so the delta skips them and only the row count per key shows the change."""
    plain = ARRAY.replace("ReplacingMergeTree(created_at)", "MergeTree")
    ch = _CH(memory_chunks={"ddl": plain, "rows": 5, "uuid": "live"})
    ch.cons_first = [(0, 0, 0)]
    ch.cons = (1, 0, 0)  # the source holds one more row under a key than the twin
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "the source changed during the gates" in capsys.readouterr().out
    reads = [q for q in ch.queries if "FULL OUTER JOIN" in q]
    assert reads and all("count() AS n" in q and "s.n != t.n" in q for q in reads)
    assert all("GROUP BY org, namespace" in q and "FINAL" not in q for q in reads)


def test_the_target_width_comes_from_real_vectors_only() -> None:
    job = vcr.VectorRebuild(
        _CH(memory_chunks=_live()),
        vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]),
    )
    job.find_dim()
    read = next(q for q in job.ch.queries if "ORDER BY n DESC" in q)
    assert "WHERE arrayExists(x -> x != 0, embedding)" in read


def test_the_target_width_counts_every_row_and_prefers_the_configured_width() -> None:
    """A FINAL read streams in sort-key order, so a sample is the first orgs, not the
    table: the width comes from a count of every real vector. LOOM_EMBEDDING_DIM wins
    when the column holds it; otherwise the most frequent width does."""
    from server.settings import settings

    configured = int(settings.embedding_dim)
    ch = _CH(memory_chunks=_live())
    job = vcr.VectorRebuild(ch, vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]))
    ch.all_widths = [(DIM, 90), (configured, 10)]  # mostly old width, partly re-embedded
    assert job.find_dim() == configured
    ch.all_widths = [(768, 90), (DIM, 10)]  # the first org's width (DIM) is the minority
    assert job.find_dim() == 768
    ch.all_widths = []
    assert job.find_dim() == configured
    read = next(q for q in ch.queries if "ORDER BY n DESC" in q)
    assert "GROUP BY w" in read and "LIMIT" not in read


def test_the_recall_reads_rank_real_vectors_only() -> None:
    """A zero placeholder's cosine distance is NaN and sorts last: left in, a namespace
    with fewer than 20 real vectors would read recall 0."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    reads = [q for q in ch.queries if "round(cosineDistance" in q]
    assert len(reads) == 2  # the exact read on the source and the twin read
    assert all(
        f"length(embedding) = {DIM} AND arrayExists(x -> x != 0, embedding) ORDER BY" in q
        for q in reads
    )


def test_unfinished_mutations_refuse_the_rebuild(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    ch.mutations = 1
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "a mutation still running or one that finished under a minute ago" in (
        capsys.readouterr().out
    )


def test_a_mutation_finished_under_a_minute_ago_refuses_before_the_twin(capsys: Any) -> None:
    """The gate before the swap counts every mutation since a minute before the preflight;
    the preflight counts the same ones, so a recent finished mutation refuses in seconds
    instead of failing the gate after the whole copy."""
    ch = _CH(memory_chunks=_live())
    ch.mutations = 1  # finished, created 30 seconds before the run
    assert _run(ch, "--yes") == tr.REFUSED
    assert "memory_chunks_rebuild" not in ch.t and _writes(ch) == []
    assert "rerun in a minute" in capsys.readouterr().out
    read = next(q for q in ch.queries if "system.mutations" in q)
    margin = COPY_STARTED - vcr.CLOCK_SKEW_MARGIN_S
    assert f"(NOT is_done OR create_time >= toDateTime({margin}))" in read


@pytest.mark.parametrize(("flags", "since"), [((), COPY_STARTED), (("--reuse",), TWIN_CREATED)])
def test_a_mutation_since_the_copy_stops_the_swap_even_when_done(
    flags: tuple[str, ...], since: int, capsys: Any
) -> None:
    """An erasure tombstone is an ALTER UPDATE that keeps the key and its version and may
    finish before the swap; any mutation created since the copy (or since a reused twin
    was created) fails the gate, done or not."""
    twin = {"memory_chunks_rebuild": _live()} if flags else {}
    ch = _CH(memory_chunks=_live(), **twin)
    ch.mutations_first = [0]  # the preflight finds nothing unfinished
    ch.mutations = 1
    assert _run(ch, "--yes", *flags) == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "1 mutation(s) on loom.memory_chunks since the copy started" in out
    assert "--restart" in out
    last = ch.queries[_last(ch, "system.mutations")]
    margin = since - vcr.CLOCK_SKEW_MARGIN_S
    assert f"(NOT is_done OR create_time >= toDateTime({margin}))" in last


def test_the_mutation_baseline_is_read_before_the_preflight_mutation_check() -> None:
    """A mutation submitted after the preflight and before the copy started must still
    count, so the time the pre-swap check counts from is read first."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    now = ch.queries.index("SELECT toUnixTimestamp(now())")
    assert now < min(i for i, q in enumerate(ch.queries) if "system.mutations" in q)


@pytest.mark.parametrize("first", [True, False])
def test_keys_tied_at_their_newest_version_name_the_optimize_remedy(
    first: bool, capsys: Any
) -> None:
    """Two rows of one key at one version: FINAL keeps the last inserted, the parallel copy
    may insert them in another order, and a rerun can match no better; the message names
    OPTIMIZE ... FINAL instead of a plain --restart."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [] if first else [(0, 0, 0)]
    ch.cons = (1, 0, 0)
    ch.tied = 2
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "2 key(s) of loom.memory_chunks hold more than one row at their newest version" in out
    assert "OPTIMIZE TABLE loom.memory_chunks FINAL, then rerun with --restart" in out


def test_the_rebuild_writes_only_to_the_twin() -> None:
    ch = _CH(memory_chunks=_live())
    ch.delta = 1
    assert _run(ch, "--yes") == tr.OK
    inserts = [c for c in ch.commands if c.startswith("INSERT")]
    assert len(inserts) >= 3
    assert all(c.startswith("INSERT INTO loom.memory_chunks_rebuild ") for c in inserts)


def test_an_existing_twin_lock_refuses_before_width_scans() -> None:
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild=_live())
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any("GROUP BY w" in q for q in ch.queries)


def test_a_failed_postcondition_names_only_the_rollback_and_keeps_the_copy(capsys: Any) -> None:
    """After exit 7 the rollback copy is the only copy of the change: the next step names
    --rollback alone, and --drop-pre-rebuild refuses while the copy and the live table differ."""
    ch = _CH(memory_chunks=_live())
    ch.cons_first = [(0, 0, 0), (0, 0, 0)]  # the two gate reads before the swap
    ch.cons = (0, 1, 0)  # a key only the rollback copy holds
    assert _run(ch, "--yes") == tr.POSTCONDITION
    summary = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])
    assert "--rollback --yes" in summary["next"] and "--drop-pre-rebuild" not in summary["next"]
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    assert not any(c.startswith("DROP TABLE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "loom.memory_chunks_pre_rebuild is kept" in out and "--rollback --yes" in out
    ch.cons = (0, 0, 0)  # the two tables match: the copy can go
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    assert "memory_chunks_pre_rebuild" not in ch.t


REPLICATED = ARRAY.replace(
    "ReplacingMergeTree(created_at)",
    "ReplicatedReplacingMergeTree('/clickhouse/tables/{uuid}', '{replica}', created_at)",
)


def test_a_gate_read_on_a_replica_that_has_not_fetched_every_part_stops_the_swap(
    capsys: Any,
) -> None:
    """SYSTEM SYNC REPLICA syncs only the replica that answers it, and a load balancer can
    send the next read elsewhere: each consistency read checks, in the same statement,
    that its own replica has fetched every part, and fails the gate when it has not."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.behind = {"memory_chunks_rebuild"}
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "ran on h2, which has not fetched every part of" in out
    assert "connect directly to one replica" in out


def test_a_preflight_read_on_another_replica_than_the_sync_refuses(capsys: Any) -> None:
    """The sync lands on one replica and the width read on another that has not caught
    up; the host names alone would not tell, the read's own replica state does."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.behind = {"memory_chunks"}
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    assert "has not fetched every part of loom.memory_chunks" in capsys.readouterr().out


def test_each_read_after_the_drain_checks_its_own_replica_in_the_same_statement() -> None:
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    assert "SELECT hostName()" not in ch.queries  # no host is recorded apart from a read
    for needle, tables in (
        ("FULL OUTER JOIN", {"memory_chunks", "memory_chunks_rebuild"}),
        ("GROUP BY w ORDER BY w", {"memory_chunks"}),
    ):
        reads = [(q, p) for q, p in zip(ch.queries, ch.params, strict=True) if needle in q]
        assert reads and all(q.startswith(DRAINED_READ) for q, _ in reads), needle
        assert tables <= {t for _, p in reads for t in p["tables"]}, needle
    assert "log_pointer > log_max_index" in vcr.REPLICA_SYNCED_SQL
    assert "system.replication_queue" in vcr.REPLICA_SYNCED_SQL


def test_a_postcondition_read_on_a_lagging_replica_keeps_the_copy_without_a_rollback(
    capsys: Any,
) -> None:
    """A read that cannot prove its replica is caught up says nothing about the tables:
    the copy is kept and the next step is to rerun the checks, not to undo the rebuild."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.behind = {"memory_chunks_pre_rebuild"}
    assert _run(ch, "--yes") == tr.POSTCONDITION
    out = capsys.readouterr().out
    assert "POSTCONDITION FAILED: the checks could not run (a read after the drain" in out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert "--rollback" not in summary["next"] and "--drop-pre-rebuild" in summary["next"]


def test_a_refused_rollback_copy_sync_after_the_swap_keeps_the_copy_without_a_rollback(
    capsys: Any,
) -> None:
    """The preflight cannot check SYSTEM SYNC REPLICA on the rollback copy, which does not
    exist yet: a refusal after the swap fails the postcondition as checks that could not
    run, not as an exception with the generic next step."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.refuse_table = "loom.memory_chunks_pre_rebuild"
    assert _run(ch, "--yes") == tr.POSTCONDITION
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "the checks could not run (Not enough privileges)" in out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert "--rollback" not in summary["next"] and "--drop-pre-rebuild" in summary["next"]


def test_a_write_into_the_new_table_after_the_swap_never_names_the_rollback(
    capsys: Any,
) -> None:
    """Writers resume after the exit-0 swap and a quarantine UPDATE lands in the new table
    at the key's version. --rollback would drop it, so --drop-pre-rebuild refuses without
    naming --rollback and keeps both tables."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    out = capsys.readouterr().out
    assert "next: keep writers and erasure stopped" in out and "--drop-pre-rebuild --yes" in out
    for cons, newer in (((1, 0, 0), (0, 0)), ((1, 0, 0), (0, 1)), ((0, 0, 1), (0, 0))):
        ch.cons, ch.newer = cons, newer
        assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
        assert "memory_chunks_pre_rebuild" in ch.t
        out = capsys.readouterr().out
        assert "loom.memory_chunks holds a change loom.memory_chunks_pre_rebuild lacks" in out
        assert "inspect the difference" in json.loads(out.rsplit("summary ", 1)[1])["next"]
        assert "--rollback --yes" not in out
    ch.cons, ch.newer = (1, 0, 0), (1, 0)  # the rollback copy holds the newer version
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "--rollback --yes" in capsys.readouterr().out


def test_drop_pre_rebuild_after_a_rollback_keeps_a_change_only_the_new_layout_copy_holds(
    capsys: Any,
) -> None:
    """A rollback copies back only newer versions and absent keys: a same-version change
    made in the new table stays only in the new-layout copy, which is then not dropped."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    assert _run(ch, "--rollback", "--yes") == tr.OK
    ch.cons = (1, 0, 0)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "loom.memory_chunks_pre_rebuild holds a change loom.memory_chunks lacks" in out
    last = ch.queries[_last(ch, "FULL OUTER JOIN")]
    assert "FROM loom.memory_chunks FINAL" in last.split("FULL OUTER JOIN")[0]
    ch.cons = (0, 2, 0)  # writes after the rollback reached only the live table
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    assert "memory_chunks_pre_rebuild" not in ch.t


def _synced_before_the_last_read(ch: _CH, start: int) -> None:
    """The rollback copy's own replication queue is synchronized, LIGHTWEIGHT, after
    `start` and before the last comparison of the copy with the new table."""
    seq = ch.sequence[start:]
    sync = seq.index("SYSTEM SYNC REPLICA loom.memory_chunks_pre_rebuild LIGHTWEIGHT")
    last = max(i for i, q in enumerate(seq) if "FULL OUTER JOIN" in q)
    assert sync < last
    assert "FROM loom.memory_chunks_pre_rebuild" in seq[last].split("FULL OUTER JOIN")[0]


def test_the_postcondition_syncs_the_rollback_copy_before_reading_it() -> None:
    """A write that reached another replica before the swap belongs to the rollback copy
    after the rename; the drained replica fetches it before the postcondition reads."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    swap = ch.sequence.index("EXCHANGE TABLES loom.memory_chunks AND loom.memory_chunks_rebuild")
    _synced_before_the_last_read(ch, swap)


def test_drop_pre_rebuild_syncs_the_rollback_copy_before_reading_it() -> None:
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    start = len(ch.sequence)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    _synced_before_the_last_read(ch, start)
    assert "memory_chunks_pre_rebuild" not in ch.t


def test_drop_pre_rebuild_checks_that_cannot_run_keep_the_copy_without_a_rollback(
    capsys: Any,
) -> None:
    """A missing grant or a lost connection says nothing about the tables: the copy is
    kept and the next step is to fix the cause and rerun, not to undo the rebuild."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    capsys.readouterr()
    ch.refuse_system = True
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "the checks could not run (Not enough privileges)" in out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert "--rollback" not in summary["next"] and "--drop-pre-rebuild" in summary["next"]
