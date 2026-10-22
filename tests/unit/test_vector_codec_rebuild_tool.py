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
# memory_chunks' sorting key ends in a FixedString column
FIXED_KEY = ARRAY.replace(
    "`created_at` DateTime\n", "`created_at` DateTime,\n    `content_sha256` FixedString(64)\n"
).replace("ORDER BY (org, namespace)", "ORDER BY (org, namespace, content_sha256)")


class _R:
    def __init__(self, rows: list[Any]) -> None:
        self.result_rows = rows


class _CH:
    def __init__(self, **tables: dict[str, Any]) -> None:
        self.t = tables
        self.commands: list[str] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []  # (statement, its settings)
        self.patches: dict[str, int] = {}
        self.uuids = iter(f"u{i}" for i in range(100))
        self.codec_rows = codec_rows("writable", "writable")
        self.delta = 0  # rows a delta finds above the destination's version
        # each read takes the next answer; the last one repeats
        self.widths: list[list[tuple[int, int]]] = [[]]  # (width, rows) the width check finds
        self.cons = [(0, 0, 0)]  # (mismatched, source_only, twin_only) the gate reads
        self.gone: list[list[tuple[Any, ...]]] = [[]]  # the keys only the twin holds
        # live rows (key and version) the rollback copy lacks, each with its part's
        # partition and highest block
        self.erased: list[tuple[tuple[Any, ...], str, int]] = []
        self.blocks = [("all", 3)]  # the twin's highest block per partition
        self.mutations = [0]  # mutations running on the source, one answer per read
        self.sync_writable = ["alter_sync", "mutations_sync", "lightweight_deletes_sync"]
        self.queries: list[str] = []
        self.reads_before_exchange = 0  # len(queries) when the EXCHANGE was sent
        self.reads_at: dict[str, int] = {}  # each command: len(queries) when it was sent

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> _R:
        p = parameters or {}
        self.queries.append(sql)
        if sql == CODEC_SETTINGS_SQL:
            return _R(self.codec_rows)
        if "FROM system.settings" in sql and "readonly = 0" in sql:
            return _R([(n,) for n in self.sync_writable])
        if sql == "SELECT 1":  # the wait level probe: 3 is accepted
            return _R([(1,)])
        if sql.startswith("SHOW CREATE TABLE loom."):
            return _R([(self.t[sql.split("loom.", 1)[1]]["ddl"],)])
        if "uuid = toUUID(" in sql:
            u = re.search(r"toUUID\('([^']*)'\)", sql).group(1)  # type: ignore[union-attr]
            return _R([(n,) for n, t in self.t.items() if t["uuid"] == u])
        if "FROM system.mutations" in sql:
            return _R([(_next(self.mutations),)])
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
        if "topK(1)" in sql:
            return _R([([DIM],)])
        if "max(max_block_number)" in sql:
            return _R(self.blocks)
        if "WHERE s.__present = 0" in sql and "_pre_rebuild" in sql:
            upto = dict(re.findall(r"_partition_id = '(\w+)' AND [^<]+ <= (\d+)", sql))
            return _R([k for k, p, b in self.erased if p in upto and b <= int(upto[p])])
        if "WHERE s.__present = 0" in sql:
            return _R(_next(self.gone))
        if "FULL OUTER JOIN" in sql:
            return _R([_next(self.cons)])
        if "GROUP BY w" in sql:
            return _R(_next(self.widths))
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
        self.reads_at.setdefault(s, len(self.queries))
        w = s.replace("loom.", "").split()
        if s.startswith("CREATE TABLE"):
            self.t[w[2]] = {"ddl": stmt, "rows": 0, "uuid": next(self.uuids)}
        elif s.startswith("DROP TABLE"):
            self.t.pop(w[2])
        elif s.startswith("EXCHANGE TABLES"):
            self.reads_before_exchange = len(self.queries)
            self.t[w[2]], self.t[w[4]] = self.t[w[4]], self.t[w[2]]
        elif s.startswith("RENAME TABLE"):
            self.t[w[4]] = self.t.pop(w[2])
        elif s.startswith("INSERT INTO") and "WHERE" not in s:
            self.t[w[2]]["rows"] = self.t[s.split("FROM loom.", 1)[1].split()[0]]["rows"]


def _next(answers: list[Any]) -> Any:
    return answers.pop(0) if len(answers) > 1 else answers[0]


def _run(ch: _CH, *flags: str) -> int:
    return vcr.main(
        ["--table", "memory_chunks", "--columns", "embedding", "--settle-s", "0", *flags], ch=ch
    )


def _writes(ch: _CH) -> list[str]:
    return [c for c in ch.commands if not c.startswith("SYSTEM")]


def _live(rows: int = 5) -> dict[str, Any]:
    return {"ddl": ARRAY, "rows": rows, "uuid": "live"}


def test_the_dimension_comes_from_the_live_table() -> None:
    coded = ARRAY.replace("CODEC(NONE)", "CODEC(Quantized('rabitq', 768, 0))")
    assert vcr.vector_dim(coded, "embedding") == 768
    assert vcr.vector_dim(ARRAY, "embedding") is None
    ch = _CH(memory_chunks=_live())
    job = vcr.VectorRebuild(ch, vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]))
    assert job.find_dim() == DIM  # a plain column: the vectors' own length
    # under FINAL: a re-embedded ReplacingMergeTree does not report its old width
    reads = [q for q in ch.queries if "topK(1)" in q]
    assert reads and all("loom.memory_chunks FINAL " in q for q in reads)


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

    def recall(job: vcr.VectorRebuild, ddl: str, fin: str) -> list[float]:
        ch.t["memory_chunks_rebuild"]["uuid"] = "someone-else"
        return real(job, ddl, fin)

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
    n = job.repair_into("t_rebuild", "t", "SELECT *", " FINAL", "created_at", ["org", "namespace"])
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
        st == ({"enable_quantized_codec": 1} if SENT[codec] else {}) for st in copies
    )


def test_vectors_of_another_width_refuse_before_the_twin_dry_run_included(capsys: Any) -> None:
    """The copy turns a vector of another length into a zero vector, so the run refuses
    while any row holds one, tombstoned rows included; an empty vector stays a placeholder."""
    ch = _CH(memory_chunks=_live())
    ch.widths = [[(768, 3), (3072, 1)]]
    assert _run(ch, "--dry-run") == tr.REFUSED and _writes(ch) == []
    out = capsys.readouterr().out
    assert f"embedding holds vectors of another width than {DIM}" in out
    assert "width 768: 3 row(s), width 3072: 1 row(s)" in out and "re-embed" in out
    preflight = [q for q in ch.queries if "GROUP BY w" in q]
    assert preflight and all(" FINAL " in q and f"NOT IN (0, {DIM})" in q for q in preflight)
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    ch.widths = [[]]
    assert _run(ch, "--yes") == tr.OK


def test_rows_only_the_twin_holds_are_deleted_from_it_before_the_swap() -> None:
    """A key erased from the source during the copy is still in the twin, and the swap
    would bring it back: the run deletes exactly those keys from the twin, by literal."""
    ch = _CH(memory_chunks=_live())
    ch.cons = [(0, 0, 1), (0, 0, 0)]
    ch.gone = [[("o", "n")], []]
    assert _run(ch, "--yes") == tr.OK
    deletes = [(c, st) for c, st in ch.sent if c.startswith("DELETE")]
    assert deletes == [
        (
            "DELETE FROM loom.memory_chunks_rebuild WHERE (org, namespace) IN (('o', 'n'))",
            {"lightweight_deletes_sync": 3, "max_execution_time": 1800},
        )
    ]


def test_a_fixed_string_key_is_deleted_by_its_hex() -> None:
    """A FixedString key such as content_sha256 reads as bytes; its literal is unhex()."""
    ch = _CH(memory_chunks={"ddl": FIXED_KEY, "rows": 5, "uuid": "live"})
    ch.cons = [(0, 0, 1), (0, 0, 0)]
    ch.gone = [[("o", "n", b"3fa9")], []]
    assert _run(ch, "--yes") == tr.OK
    deletes = [c for c in ch.commands if c.startswith("DELETE")]
    assert deletes == [
        "DELETE FROM loom.memory_chunks_rebuild WHERE (org, namespace, content_sha256) IN "
        "(('o', 'n', unhex('33666139')))"
    ]


def test_the_twin_delete_waits_only_where_this_user_may_say_so() -> None:
    """A wait setting held readonly is not sent: the DELETE goes out without it."""
    ch = _CH(memory_chunks=_live())
    ch.cons = [(0, 0, 1), (0, 0, 0)]
    ch.gone = [[("o", "n")], []]
    ch.sync_writable = []
    assert _run(ch, "--yes") == tr.OK
    deletes = [st for c, st in ch.sent if c.startswith("DELETE")]
    assert deletes == [{"max_execution_time": 1800}]


def test_rows_that_stay_in_the_twin_only_fail_the_gate(capsys: Any) -> None:
    """Rows still removed from the source as the run deletes them: no swap, the twin is left."""
    ch = _CH(memory_chunks=_live())
    ch.cons = [(0, 0, 2)]
    ch.gone = [[("o", "n"), ("o", "m")]]
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "2 keys the source no longer holds remain in the twin" in out
    assert "Pause erasures" in out


def test_the_recall_reads_rank_real_vectors_only() -> None:
    """A zero placeholder's cosine distance is NaN and sorts last: left in, a namespace
    with fewer than 20 real vectors would read recall 0."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    reads = [q for q in ch.queries if "round(cosineDistance" in q]
    assert len(reads) == 2  # the exact read on the source and the twin read
    assert all(f"length(embedding) = {DIM} AND embedding[1] != 0 ORDER BY" in q for q in reads)


def test_a_delete_during_the_gates_stops_the_swap(capsys: Any) -> None:
    """The last delta only adds rows, so a key erased from the source after the first
    consistency read is still in the twin: the check runs again before the EXCHANGE."""
    ch = _CH(memory_chunks=_live())
    ch.cons = [(0, 0, 0), (0, 0, 1)]
    ch.gone = [[("o", "n")]]
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "1 keys the source no longer holds remain" in capsys.readouterr().out


def test_the_twin_only_check_is_the_last_read_before_the_exchange() -> None:
    """The width scans read only the source and run first; a delete during them is
    caught by the twin-only read that follows, and the EXCHANGE comes right after it
    and the read of the twin's highest blocks. Only when that read deleted keys does a
    consistency read recheck them."""
    mark = vcr.last_blocks_sql("memory_chunks_rebuild")
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    before = ch.queries[: ch.reads_before_exchange]
    assert before[-1] == mark and "WHERE s.__present = 0" in before[-2]
    assert max(i for i, q in enumerate(before) if "GROUP BY w" in q) < len(before) - 2

    ch = _CH(memory_chunks=_live())
    ch.gone = [[("o", "n")], []]
    assert _run(ch, "--yes") == tr.OK
    before = ch.queries[: ch.reads_before_exchange]
    assert before[-1] == mark and "FULL OUTER JOIN" in before[-2]
    assert "WHERE s.__present = 0" in before[-3]


PRE_SWAP = "((_partition_id = 'all' AND toUInt64(splitByChar('_', _part)[3]) <= 3))"


def _deletes_after_exchange(ch: _CH) -> list[str]:
    exchange = next(i for i, c in enumerate(ch.commands) if c.startswith("EXCHANGE"))
    return [c for c in ch.commands[exchange:] if c.startswith("DELETE")]


def test_a_row_erased_across_the_swap_is_deleted_from_the_live_table() -> None:
    """An erasure after the last twin-only read reaches only the old table. A live row in
    a part the twin held before the swap was copied before it, so the rollback copy's
    lacking it means its writer deleted it: the run deletes every version of it read
    there, by key and version, since a merge may rename the parts once they start."""
    ch = _CH(memory_chunks=_live())
    ch.erased = [(("o", "n", 7), "all", 2), (("o", "n", 5), "all", 1)]
    assert _run(ch, "--yes") == tr.OK
    assert _deletes_after_exchange(ch) == [
        "DELETE FROM loom.memory_chunks WHERE (org, namespace, created_at) IN "
        "(('o', 'n', 7), ('o', 'n', 5))"
    ]
    read = next(q for q in ch.queries if "_pre_rebuild" in q and "s.__present = 0" in q)
    assert read.startswith("SELECT org, namespace, created_at FROM (SELECT org, namespace, ")
    assert f"FROM loom.memory_chunks WHERE {PRE_SWAP})" in read  # every version, no FINAL


def test_a_plain_merge_tree_deletes_a_row_erased_across_the_swap_by_key() -> None:
    ddl = ARRAY.replace("ReplacingMergeTree(created_at)", "MergeTree")
    ch = _CH(memory_chunks={"ddl": ddl, "rows": 5, "uuid": "live"})
    ch.erased = [(("o", "n"), "all", 2)]
    assert _run(ch, "--yes") == tr.OK
    assert _deletes_after_exchange(ch) == [
        "DELETE FROM loom.memory_chunks WHERE (org, namespace) IN (('o', 'n'))"
    ]


def test_the_twin_merges_stop_from_its_block_read_to_the_erased_read() -> None:
    """A merge of a copied part with a part written after the swap would hide its rows
    from the erased read, and a delete on the live table would show the post-swap
    passes a missing key to copy back: the twin's merges stop before its blocks are
    read and start again after the erased rows are read, before they are deleted."""
    ch = _CH(memory_chunks=_live())
    ch.erased = [(("o", "n", 7), "all", 2)]
    assert _run(ch, "--yes") == tr.OK
    stop = "SYSTEM STOP MERGES loom.memory_chunks_rebuild"
    start = "SYSTEM START MERGES loom.memory_chunks"
    exchange = next(c for c in ch.commands if c.startswith("EXCHANGE"))
    delete = next(c for c in ch.commands if c.startswith("DELETE"))
    order = [ch.commands.index(c) for c in (stop, exchange, start, delete)]
    assert order == sorted(order)
    blocks = ch.queries.index(vcr.last_blocks_sql("memory_chunks_rebuild"))
    erased = max(
        i for i, q in enumerate(ch.queries) if "_pre_rebuild" in q and "s.__present = 0" in q
    )
    assert ch.reads_at[stop] <= blocks < ch.reads_at[exchange] <= erased < ch.reads_at[start]


def test_a_failed_swap_starts_the_twin_merges_again(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(memory_chunks=_live())

    def fail(*_: Any) -> None:
        raise RuntimeError("exchange refused")

    monkeypatch.setattr(vcr.tr, "swap_in", fail)
    with pytest.raises(RuntimeError, match="exchange refused"):
        _run(ch, "--yes")
    assert ch.commands[-1] == "SYSTEM START MERGES loom.memory_chunks_rebuild"


@pytest.mark.parametrize("engine", ["ReplacingMergeTree(created_at)", "MergeTree"])
def test_a_write_after_the_swap_is_kept_whatever_its_version(engine: str) -> None:
    """A row written after the EXCHANGE, even with a version below every copied row's
    (a turn's embedding stamped when its append started, a compaction summary at its
    first turn's time), lands in a new part: it is kept, and the run succeeds."""
    ddl = ARRAY.replace("ReplacingMergeTree(created_at)", engine)
    ch = _CH(memory_chunks={"ddl": ddl, "rows": 5, "uuid": "live"})
    ch.erased = [(("o", "new", 1), "all", 4), (("o", "fresh-partition", 1), "202610", 1)]
    assert _run(ch, "--yes") == tr.OK
    assert _deletes_after_exchange(ch) == []


def test_a_write_of_another_width_during_the_run_stops_the_swap(capsys: Any) -> None:
    """A writer still on another model during the copy has its rows zeroed in the twin,
    so the width check runs again before the EXCHANGE."""
    ch = _CH(memory_chunks=_live())
    ch.widths = [[], [(768, 2)]]
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "width 768: 2 row(s)" in capsys.readouterr().out


def test_a_key_gone_from_the_source_during_the_repair_is_deleted_before_the_recheck() -> None:
    """The first read shows only a mismatch; an erasure during the repair leaves a key only
    the twin holds. The delete pass runs before the recheck, so a failure that names it
    is true."""
    ch = _CH(memory_chunks=_live())
    ch.cons = [(1, 0, 0), (0, 0, 0)]
    ch.gone = [[("o", "n")], []]
    assert _run(ch, "--yes") == tr.OK
    deletes = [c for c in ch.commands if c.startswith("DELETE")]
    assert deletes == [
        "DELETE FROM loom.memory_chunks_rebuild WHERE (org, namespace) IN (('o', 'n'))"
    ]


TOMBSTONED = ARRAY.replace(
    "`created_at` DateTime\n", "`created_at` DateTime,\n    `tombstoned_at` Nullable(DateTime)\n"
)


def test_a_tombstone_set_in_place_during_the_run_is_compared_and_repaired() -> None:
    """An erasure tombstones a row with an ALTER UPDATE that keeps its version: the
    gate compares `tombstoned_at` too, and the repair copies a newer one."""
    ch = _CH(memory_chunks={"ddl": TOMBSTONED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    tomb = vcr.tombstone
    gate = next(q for q in ch.queries if "FULL OUTER JOIN" in q)
    assert f"countIf(s.created_at != t.created_at OR {tomb('s')} != {tomb('t')})" in gate
    assert "SELECT org, namespace, created_at, tombstoned_at, 1 AS __present" in gate
    repair = next(q for q in ch.queries if "t.__present = 0" in q and "LEFT JOIN" in q)
    assert f"OR t.__present = 0 OR {tomb('s')} > {tomb('t')}" in repair
    assert tomb("s") == "ifNull(toUInt32(s.tombstoned_at), 0)"


def test_a_table_without_tombstones_compares_versions_only() -> None:
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    assert not any("tombstoned_at" in q for q in ch.queries)


def test_a_mutation_still_running_on_the_source_stops_the_swap(capsys: Any) -> None:
    """A tombstone or delete still being applied would reach only the old table, whose
    merges and mutations stop at the swap: the run waits for it, then fails."""
    ch = _CH(memory_chunks=_live())
    ch.mutations = [1]
    assert _run(ch, "--yes", "--statement-timeout-s", "0.01") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "1 mutations on loom.memory_chunks still running" in capsys.readouterr().out
    reads = [q for q in ch.queries if "FROM system.mutations" in q]
    assert reads[0] == vcr.mutations_running_sql("memory_chunks")


def test_the_run_waits_for_a_mutation_on_the_source_to_finish() -> None:
    ch = _CH(memory_chunks=_live())
    ch.mutations = [2, 1, 0]
    assert _run(ch, "--yes") == tr.OK
    assert sum("FROM system.mutations" in q for q in ch.queries) == 3
