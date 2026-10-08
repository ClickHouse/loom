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
        self.patches: dict[str, int] = {}
        self.uuids = iter(f"u{i}" for i in range(100))

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> _R:
        p = parameters or {}
        if sql.startswith("SHOW CREATE TABLE loom."):
            return _R([(self.t[sql.split("loom.", 1)[1]]["ddl"],)])
        if "FROM system.tables" in sql:
            row = self.t.get(str(p.get("t")))
            return _R([("ReplacingMergeTree", row["uuid"], "2000-01-01")] if row else [])
        if "system.one" in sql:
            return _R([("h", "26.8.8.8")])
        if sql == "SELECT 1" or "system.replicas" in sql or "compression_codec FROM" in sql:
            return _R([] if sql != "SELECT 1" else [(1,)])
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
        if "FULL OUTER JOIN" in sql:
            return _R([(0, 0, 0)])
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
            return _R([(0,)])
        return _R([(rows,)])

    def command(self, stmt: str, parameters: Any = None, settings: Any = None) -> None:
        s = " ".join(stmt.split())
        self.commands.append(s)
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
    cmds: list[str] = []
    job.q = lambda sql, **_: [("o", "n")] * (vcr.REPAIR_BATCH + 1)  # type: ignore[method-assign]
    job.cmd = lambda sql, **_: cmds.append(sql)  # type: ignore[method-assign]
    n = job.repair_into("t_rebuild", "t", "SELECT *", " FINAL", "created_at", ["org", "namespace"])
    assert n == vcr.REPAIR_BATCH + 1 and len(cmds) == 2
    assert all("IN (('o', 'n')" in c and "SELECT" not in c.split(" IN ", 1)[1] for c in cmds)
