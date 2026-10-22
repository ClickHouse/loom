"""scripts/ops/vector_codec_rebuild.py against a fake ClickHouse: nothing
changes without --yes, the twin is the run's lock, a failed gate never swaps,
a swap leaves the rollback copy, and --rollback / --drop-pre-rebuild behave as
the sort-key tool's do."""

from __future__ import annotations

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


class _R:
    def __init__(self, rows: list[Any]) -> None:
        self.result_rows = rows


class _CH:
    def __init__(self, **tables: dict[str, Any]) -> None:
        self.t = tables
        self.commands: list[str] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []  # (statement, its settings)
        self.patches: dict[str, int] = {}
        self.patches_first: dict[str, list[int]] = {}  # earlier reads, before self.patches
        # the live table's patch parts: partition id -> highest max_block_number
        self.patch_blocks: dict[str, int] = {}
        self.patch_blocks_first: list[
            dict[str, int]
        ] = []  # earlier reads, before self.patch_blocks
        self.uuids = iter(f"u{i}" for i in range(100))
        self.codec_rows = codec_rows("writable", "writable")
        self.delta = 0  # rows a delta finds above the destination's version
        self.widths: list[tuple[int, int]] = []  # (width, rows) the width preflight finds
        self.widths_first: list[list[tuple[int, int]]] = []  # earlier reads, before self.widths
        self.cons = (0, 0, 0)  # (mismatched, source_only, twin_only) the gate reads
        self.cons_first: list[tuple[int, int, int]] = []  # earlier reads, before self.cons
        self.mutations = 0
        self.mutations_first: list[int] = []
        self.queries: list[str] = []
        self.query_settings: list[tuple[str, dict[str, Any]]] = []

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> _R:
        p = parameters or {}
        self.queries.append(sql)
        self.query_settings.append((sql, dict(settings or {})))
        if sql == CODEC_SETTINGS_SQL:
            return _R(self.codec_rows)
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
        if sql.startswith("SELECT partition_id, max(max_block_number)"):
            blocks = (
                self.patch_blocks_first.pop(0) if self.patch_blocks_first else self.patch_blocks
            )
            return _R(list(blocks.items()))
        if "startsWith(name, 'patch')" in sql:
            name = re.search(r"table = '(\w+)'", sql).group(1)  # type: ignore[union-attr]
            first = self.patches_first.get(name)
            return _R([(first.pop(0) if first else self.patches.get(name, 0),)])
        if "topK(1)" in sql:
            return _R([([DIM],)])
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
        self.sent.append((s, dict(settings or {})))
        w = s.replace("loom.", "").split()
        if s.startswith("CREATE TABLE"):
            self.t[w[2]] = {"ddl": stmt, "rows": 0, "uuid": next(self.uuids)}
        elif s.startswith("DROP TABLE"):
            self.t.pop(w[2])
        elif s.startswith("EXCHANGE TABLES"):
            self.t[w[2]], self.t[w[4]] = self.t[w[4]], self.t[w[2]]
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
    read = next(q for q in job.ch.queries if "topK(1)" in q)
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
    n = job.repair_into("t_rebuild", "t", "SELECT *", "created_at", ["org", "namespace"])
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
    """The way back to the plain layout works wherever the setting stands now."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    ch.codec_rows, ch.delta = codec_rows(codec, "off"), 2
    assert _run(ch, "--rollback", "--yes") == tr.OK
    copies = [st for s, st in ch.sent if s.startswith("INSERT")]
    assert copies and all(
        st == {"max_execution_time": 1800, **({"enable_quantized_codec": 1} if SENT[codec] else {})}
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
        "topK(1)",
        "round(cosineDistance",
        "ORDER BY rand()",
        "SELECT max(created_at)",
        "max(max_block_number)",
    ):
        reads = [st for q, st in ch.query_settings if needle in q]
        assert reads and all(st["max_execution_time"] == 60 for st in reads), needle
    for bad in ("0", "-1", "nan", "inf", str(vcr.COPY_RECEIVE_TIMEOUT_S + 1)):
        with pytest.raises(SystemExit):
            vcr.parse(["--table", "t", "--columns", "embedding", "--statement-timeout-s", bad])


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # a new UPDATE takes a block above every recorded one, merged or not
        ({"patch-h1-all": 5}, {"patch-h1-all": 7}),
        # an UPDATE of other columns writes patch parts in a partition not recorded
        ({"patch-h1-all": 5}, {"patch-h1-all": 5, "patch-h2-all": 7}),
        ({}, {"patch-h1-all": 7}),
    ],
)
def test_a_lightweight_update_during_the_run_stops_the_swap(
    before: dict[str, int], after: dict[str, int], capsys: Any
) -> None:
    """A quarantine keeps the key and its version, so the consistency reads match; the
    patch part it writes on the source is what shows it, and the run stops."""
    ch = _CH(memory_chunks=_live())
    ch.patch_blocks_first = [before]
    ch.patch_blocks = after
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "a lightweight UPDATE landed on the source during the run" in out
    assert "--restart" in out


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


@pytest.mark.parametrize(
    "after",
    [
        # patch-a (block 2) and patch-b (block 5) merged into one part: it keeps block 5
        {"patch-h1-all": 5},
        # applied and dropped during the run
        {},
    ],
)
def test_patch_parts_that_were_there_before_the_copy_do_not_stop_the_swap(
    after: dict[str, int],
) -> None:
    ch = _CH(memory_chunks=_live())
    ch.patch_blocks_first = [{"patch-h1-all": 5}]
    ch.patch_blocks = after
    assert _run(ch, "--yes") == tr.OK
    assert any(c.startswith("EXCHANGE") for c in ch.commands)


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
    read = next(q for q in job.ch.queries if "topK(1)" in q)
    assert "WHERE length(embedding) > 0 AND arrayExists(x -> x != 0, embedding)" in read


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


@pytest.mark.parametrize("during_run", [False, True])
def test_unfinished_mutations_stop_the_rebuild(during_run: bool, capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    ch.mutations_first = [0] if during_run else []
    ch.mutations = 1
    assert _run(ch, "--yes") == (tr.GATE_FAILED if during_run else tr.REFUSED)
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "unfinished mutations" in capsys.readouterr().out


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
