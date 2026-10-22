"""scripts/ops/vector_codec_rebuild.py against a fake ClickHouse: nothing
changes without --yes, the twin is the run's lock, a failed gate never swaps,
a swap leaves the rollback copy, and --rollback / --drop-pre-rebuild behave as
the sort-key tool's do."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.ops import vector_codec_rebuild as vcr
from server.core.schema import table_rebuild as tr
from server.core.schema.vector_codec_ddl import CODEC_SETTINGS_SQL
from server.settings import settings as loom_settings
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
        self.mutations = 0
        self.mutations_first: list[int] = []
        self.tied = 0  # keys with more than one row at their newest version
        self.queries: list[str] = []
        self.sequence: list[str] = []  # every query and command, in order
        self.query_settings: list[tuple[str, dict[str, Any]]] = []
        self.params: list[dict[str, Any]] = []  # each query's parameters, in order
        self.behind: set[str] = set()  # tables the replica answering a read has not caught up
        self.no_probes = False  # the probe read finds no full-length vector
        self.db_engine = "Atomic"  # the engine of the database the run's tables are in

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
            # a table this fake did not create (a reused twin) was created at TWIN_CREATED
            return _R([(self.t.get(str(p.get("t")), {}).get("created", TWIN_CREATED),)])
        if "FROM system.databases" in sql:
            return _R([(self.db_engine,)])
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
            name = sql.split("FROM loom.", 1)[1].split()[0]
            has_probe = self.t[name]["rows"] and not self.no_probes
            return _R([("o", "n", [0.5] * DIM)] if has_probe else [])
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
            self.t[w[2]] = {
                "ddl": stmt,
                "rows": 0,
                "uuid": next(self.uuids),
                "created": COPY_STARTED,
            }
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


def _twin(method: str = "rabitq") -> dict[str, Any]:
    """A twin an earlier run left, coded with `method`, created at TWIN_CREATED."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('{method}', {DIM}, 0))")
    return {"ddl": coded, "rows": 5, "uuid": "twin"}


@pytest.fixture(autouse=True)
def _run_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A plain column's target width is LOOM_EMBEDDING_DIM; each test keeps its own
    abort file and twin record."""
    monkeypatch.setattr(loom_settings, "embedding_dim", DIM)
    monkeypatch.chdir(tmp_path)


def test_the_dimension_comes_from_the_live_table() -> None:
    coded = ARRAY.replace("CODEC(NONE)", "CODEC(Quantized('rabitq', 768, 0))")
    assert vcr.vector_dim(coded, "embedding") == 768
    assert vcr.vector_dim(ARRAY, "embedding") is None
    job = vcr.VectorRebuild(
        _CH(memory_chunks=_live()),
        vcr.parse(["--table", "memory_chunks", "--columns", "embedding"]),
    )
    assert job.find_dim() == DIM  # a plain column: LOOM_EMBEDDING_DIM


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


@pytest.mark.parametrize(
    ("twin", "flags"),
    [
        (_twin("turboquant"), ()),
        (_twin(), ("--index-granularity", "64")),
        (_twin(), ("--chain", "LZ4")),
        (_live(), ()),
        (
            {**_twin(), "ddl": _twin()["ddl"].replace("ORDER BY (org, namespace)", "ORDER BY org")},
            (),
        ),
    ],
    ids=["method", "granularity", "chain", "plain", "sorting-key"],
)
def test_a_reused_twin_of_another_layout_refuses(
    twin: dict[str, Any], flags: tuple[str, ...], capsys: Any
) -> None:
    """The gates read the stored vectors and the codes, so neither sees a twin built with
    another method, granularity, chain or sorting key: --reuse refuses it and names
    --restart instead of swapping that layout in."""
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild=twin)
    assert _run(ch, "--yes", "--reuse", *flags) == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert ch.t["memory_chunks_rebuild"] is twin and _writes(ch) == []
    assert "--restart --yes" in capsys.readouterr().out


def test_a_reused_twin_of_this_layout_swaps() -> None:
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild=_twin())
    assert _run(ch, "--yes", "--reuse") == tr.OK
    assert not any(c.startswith("CREATE") for c in ch.commands)
    assert "Quantized('rabitq', 1536, 0)" in ch.t["memory_chunks"]["ddl"]


def test_a_twin_left_behind_is_a_lock_until_reuse_or_restart(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild={"ddl": ARRAY, "rows": 5, "uuid": "x"})
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    assert "another run's lock" in capsys.readouterr().out
    assert _run(ch, "--yes", "--restart") == tr.OK
    assert _writes(ch)[0] == "DROP TABLE loom.memory_chunks_rebuild SYNC"


@pytest.mark.parametrize("flag", ["--reuse", "--restart"])
def test_a_twin_older_than_the_live_table_is_the_old_table_and_is_kept(
    flag: str, capsys: Any
) -> None:
    """An exchange without its rename leaves the old table as the twin, older than the
    live table: the run refuses and prints the rename instead of dropping it."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    old = {"ddl": ARRAY, "rows": 5, "uuid": "old", "created": TWIN_CREATED}
    new = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    ch = _CH(memory_chunks=new, memory_chunks_rebuild=old)
    assert _run(ch, "--yes", "--require-patch-free", flag) == tr.REFUSED
    assert _writes(ch) == [] and "memory_chunks_rebuild" in ch.t
    assert "RENAME TABLE loom.memory_chunks_rebuild TO loom.memory_chunks_pre_rebuild" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "flags", [("--method", "turboquant"), ("--index-granularity", "64"), ("--chain", "LZ4")]
)
def test_an_older_plain_twin_is_kept_whatever_layout_the_rerun_names(
    flags: tuple[str, str], capsys: Any
) -> None:
    """After an exchange without its rename the live table is coded and the twin is the
    older plain table. A rerun that names another layout finds the live table off its
    target, and still refuses --restart and prints the rename."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    old = {"ddl": ARRAY, "rows": 5, "uuid": "old", "created": TWIN_CREATED}
    new = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    ch = _CH(memory_chunks=new, memory_chunks_rebuild=old)
    assert _run(ch, "--yes", "--restart", *flags) == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is old
    assert "RENAME TABLE loom.memory_chunks_rebuild TO loom.memory_chunks_pre_rebuild" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("flag", ["--restart", "--reuse", "--dry-run"])
def test_an_older_plain_twin_is_kept_whatever_columns_the_rerun_names(
    flag: str, capsys: Any
) -> None:
    """A rebuild of `embedding` alone exchanged without its rename: the live table has
    `embedding` coded and `key_embedding` plain, and the older plain twin is the old
    table. A rerun naming both columns still refuses, drops nothing and prints the rename."""
    two = ARRAY.replace(
        "    `created_at` DateTime",
        "    `key_embedding` Array(Float32),\n    `created_at` DateTime",
    )
    coded = two.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    old = {"ddl": two, "rows": 5, "uuid": "old", "created": TWIN_CREATED}
    new = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    ch = _CH(memory_chunks=new, memory_chunks_rebuild=old)
    code = vcr.main(
        [
            "--table",
            "memory_chunks",
            "--columns",
            "embedding,key_embedding",
            "--settle-s",
            "0",
            "--yes",
            flag,
        ],
        ch=ch,
    )
    assert code == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is old
    assert "RENAME TABLE loom.memory_chunks_rebuild TO loom.memory_chunks_pre_rebuild" in (
        capsys.readouterr().out
    )


def test_drop_pre_rebuild_without_the_copy_refuses_while_a_twin_exists(capsys: Any) -> None:
    """With no rollback copy and an older plain twin, --drop-pre-rebuild names the rename
    rather than reporting the copy gone, so no hand drop of the twin follows."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    old = {"ddl": ARRAY, "rows": 5, "uuid": "old", "created": TWIN_CREATED}
    new = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    ch = _CH(memory_chunks=new, memory_chunks_rebuild=old)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert _writes(ch) == [] and "memory_chunks_rebuild" in ch.t
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "RENAME TABLE loom.memory_chunks_rebuild TO loom.memory_chunks_pre_rebuild" in step


def test_a_twin_older_than_a_plain_live_table_is_a_leftover() -> None:
    """An ALTER of the live table after an aborted run moves its metadata time past the
    twin's; with the live table still off the target layout no exchange ran, so
    --restart drops the twin and copies again."""
    altered = {**_live(), "created": COPY_STARTED}
    ch = _CH(memory_chunks=altered, memory_chunks_rebuild={"ddl": ARRAY, "rows": 5, "uuid": "x"})
    assert _run(ch, "--yes", "--restart") == tr.OK
    assert _writes(ch)[0] == "DROP TABLE loom.memory_chunks_rebuild SYNC"
    assert "Quantized('rabitq', 1536, 0)" in ch.t["memory_chunks"]["ddl"]


def test_two_coded_tables_with_an_older_twin_are_refused_untouched(capsys: Any) -> None:
    """A --require-patch-free run can leave a coded twin under a coded live table, so
    the layouts do not show whether an exchange ran: the run neither renames nor drops,
    and asks for the two tables to be compared."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    twin = {"ddl": coded, "rows": 3, "uuid": "partial", "created": TWIN_CREATED}
    live = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    ch = _CH(memory_chunks=live, memory_chunks_rebuild=twin)
    assert _run(ch, "--yes", "--require-patch-free", "--restart") == tr.REFUSED
    assert _writes(ch) == [] and "memory_chunks_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "compare" in out and "RENAME TABLE" not in out and "--restart --yes" not in out


@pytest.mark.parametrize("flag", ["--dry-run", "--drop-pre-rebuild"])
def test_a_twin_newer_than_a_coded_live_table_is_named_a_leftover(flag: str, capsys: Any) -> None:
    """A failed `--chain LZ4` run on a table coded without a chain leaves a newer chained
    twin. A rerun with the live table's flags names it a leftover, never the rename that
    would make it the rollback copy."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    chained = coded.replace(", 0))", ", 0), LZ4)")
    live = {"ddl": coded, "rows": 5, "uuid": "live", "created": TWIN_CREATED}
    twin = {"ddl": chained, "rows": 3, "uuid": "failed", "created": COPY_STARTED}
    ch = _CH(memory_chunks=live, memory_chunks_rebuild=twin)
    _run(ch, flag, "--yes")
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is twin
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "RENAME TABLE" not in step and "compare" not in step
    assert "leftover never exchanged in" in step and "--restart --yes" in step


@pytest.mark.parametrize(
    ("db_engine", "twin_created"), [("Atomic", TWIN_CREATED), ("Shared", COPY_STARTED)]
)
def test_a_twin_not_shown_newer_is_never_named_a_leftover(
    db_engine: str, twin_created: int, capsys: Any
) -> None:
    """Equal metadata times, or times on a database engine the live tests do not cover,
    do not show that no exchange ran: a plain twin under a coded live table may be the
    old table, so the run names the rename and keeps it."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    live = {"ddl": coded, "rows": 5, "uuid": "live", "created": TWIN_CREATED}
    twin = {"ddl": ARRAY, "rows": 5, "uuid": "old", "created": twin_created}
    ch = _CH(memory_chunks=live, memory_chunks_rebuild=twin)
    ch.db_engine = db_engine
    assert _run(ch, "--yes", "--restart") == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is twin
    out = capsys.readouterr().out
    assert "leftover" not in out
    assert "RENAME TABLE loom.memory_chunks_rebuild TO loom.memory_chunks_pre_rebuild" in out


def test_a_twin_this_tool_created_is_a_leftover_on_any_database_engine(capsys: Any) -> None:
    """Metadata times show nothing on a Shared database, but the twin's UUID is the one
    the tool recorded when it created it, so no exchange ran: the run names it a
    leftover, and --restart drops it and copies again."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    live = {"ddl": coded, "rows": 5, "uuid": "live", "created": COPY_STARTED}
    twin = {"ddl": coded, "rows": 3, "uuid": "ours", "created": TWIN_CREATED}
    ch = _CH(memory_chunks=live, memory_chunks_rebuild=twin)
    ch.db_engine = "Shared"
    assert _run(ch, "--dry-run", "--require-patch-free") == tr.REFUSED
    assert "compare" in capsys.readouterr().out  # no record: either may be the old table
    Path("memory_chunks_rebuild.uuid").write_text("ours live")
    assert _run(ch, "--dry-run", "--require-patch-free") == tr.REFUSED
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "leftover never exchanged in" in step and "compare" not in step
    assert _run(ch, "--yes", "--require-patch-free", "--restart") == tr.OK
    assert _writes(ch)[0] == "DROP TABLE loom.memory_chunks_rebuild SYNC"


class _AlteredDuringGates(_CH):
    """An ALTER of the live table (ADD INDEX, MODIFY SETTING) right after the twin is
    created moves the live table's metadata time past the twin's."""

    def command(self, stmt: str, parameters: Any = None, settings: Any = None) -> None:
        super().command(stmt, parameters, settings)
        if stmt.lstrip().startswith("CREATE TABLE"):
            self.t["memory_chunks"]["created"] = COPY_STARTED + 60


def test_a_live_table_altered_after_the_twin_was_created_is_never_exchanged(
    capsys: Any,
) -> None:
    """A half swap would leave the old table, now newer, at the twin's name, where it would
    read as a leftover never exchanged in: the swap refuses and keeps the twin."""
    ch = _AlteredDuringGates(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_rebuild" in ch.t and "memory_chunks_pre_rebuild" not in ch.t
    assert "was altered after loom.memory_chunks_rebuild was created" in capsys.readouterr().out


def test_a_created_twin_is_recorded_by_its_uuid() -> None:
    ch = _CH(memory_chunks=_live())
    ch.cons = (1, 0, 0)  # the gate fails, so the twin stays
    assert _run(ch, "--yes") == tr.GATE_FAILED
    twin = ch.t["memory_chunks_rebuild"]["uuid"]
    assert Path("memory_chunks_rebuild.uuid").read_text() == f"{twin} live"


class _RenameLost(_CH):
    """A fake whose RENAME TABLE fails once `lose_rename` is set: a half swap."""

    lose_rename = False

    def command(self, stmt: str, parameters: Any = None, settings: Any = None) -> None:
        if self.lose_rename and stmt.startswith("RENAME TABLE"):
            raise RuntimeError("session lost")
        super().command(stmt, parameters, settings)


def test_a_swap_removes_the_record_so_a_later_half_swap_keeps_the_old_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A recorded twin that was exchanged in is the live table, and a later run's twin
    may be the old table. Run A's swap removes A's record, so after run B, from another
    directory, exchanges without its rename, a run from A's directory keeps B's twin."""
    ch = _RenameLost(memory_chunks=_live())
    ch.db_engine = "Shared"
    assert _run(ch, "--yes") == tr.OK
    assert not Path("memory_chunks_rebuild.uuid").exists()
    ch.t.pop("memory_chunks_pre_rebuild")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    ch.lose_rename = True
    _run(ch, "--yes", "--require-patch-free")
    assert not (other / "memory_chunks_rebuild.uuid").exists()
    monkeypatch.chdir(tmp_path)
    old = ch.t["memory_chunks_rebuild"]
    ch.commands.clear()
    capsys.readouterr()
    assert _run(ch, "--yes", "--require-patch-free", "--restart") == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is old
    out = capsys.readouterr().out
    assert "compare" in out and "leftover" not in out


def test_a_record_left_by_a_reused_swap_no_longer_names_the_twin_a_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A twin created from one directory and swapped in by a --reuse run from another
    keeps its record in the first. After a later half swap leaves it, now the old table,
    at the twin's name, the record no longer matches: the live table's UUID changed, so
    a run from the first directory keeps the twin and never drops it."""
    ch = _RenameLost(memory_chunks=_live())
    ch.db_engine = "Shared"
    ch.cons = (1, 0, 0)  # the gate fails, so the twin stays
    assert _run(ch, "--yes") == tr.GATE_FAILED
    created = ch.t["memory_chunks_rebuild"]
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    ch.cons = (0, 0, 0)
    assert _run(ch, "--yes", "--reuse") == tr.OK
    assert (tmp_path / "memory_chunks_rebuild.uuid").exists()
    ch.t.pop("memory_chunks_pre_rebuild")
    ch.lose_rename = True
    _run(ch, "--yes", "--require-patch-free")
    assert ch.t["memory_chunks_rebuild"] is created
    monkeypatch.chdir(tmp_path)
    ch.commands.clear()
    capsys.readouterr()
    _run(ch, "--dry-run", "--require-patch-free")
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "leftover" not in step and "compare" in step
    assert _run(ch, "--yes", "--require-patch-free", "--restart") == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks_rebuild"] is created
    assert "leftover" not in capsys.readouterr().out


def test_a_record_that_cannot_be_removed_refuses_the_swap() -> None:
    Path("memory_chunks_rebuild.uuid").mkdir()  # unlink() fails on a directory
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_rebuild" in ch.t


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


def test_a_second_rollback_refuses_and_names_the_drop(capsys: Any) -> None:
    """After a rollback the live table is the old layout again: another --rollback would
    exchange the new layout back in without the recall gate, so it refuses."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    assert _run(ch, "--rollback", "--yes") == tr.OK
    ch.commands.clear()
    capsys.readouterr()
    assert _run(ch, "--rollback", "--yes") == tr.REFUSED
    assert _writes(ch) == [] and ch.t["memory_chunks"]["ddl"] == ARRAY
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "already rolled back" in step and "--drop-pre-rebuild --yes" in step


def test_a_second_rollback_on_the_target_layout_refuses() -> None:
    """A --require-patch-free run leaves both tables on the target layout; after its
    rollback the live table is the older one, so another --rollback refuses."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(memory_chunks={"ddl": coded, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes", "--require-patch-free") == tr.OK
    assert _run(ch, "--rollback", "--yes") == tr.OK
    ch.commands.clear()
    assert _run(ch, "--rollback", "--yes") == tr.REFUSED
    assert _writes(ch) == []


@pytest.mark.parametrize(
    ("mutations", "cons"),
    [(1, (0, 0, 0)), (0, (0, 0, 1)), (0, (1, 0, 0))],
    ids=["mutation-on-the-new-table", "key-only-in-the-new-table", "same-version-change"],
)
def test_a_rollback_that_would_lose_a_change_in_the_new_table_refuses(
    mutations: int, cons: tuple[int, int, int], capsys: Any
) -> None:
    """--rollback copies back only newer versions and absent keys, so an erasure tombstone,
    a lightweight UPDATE or a write on the new table after the swap would be lost: it runs
    the checks --drop-pre-rebuild runs and exchanges nothing when they find such a change."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    ch.mutations, ch.cons = mutations, cons
    assert _run(ch, "--rollback", "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert ch.t["memory_chunks"]["ddl"] == coded
    out = capsys.readouterr().out
    assert "nothing is exchanged" in out and "inspect the difference by hand" in out


def test_a_mutation_on_the_copy_after_a_rollback_keeps_it(capsys: Any) -> None:
    """An ALTER UPDATE on the new table after the swap, such as an erasure tombstone, is
    not copied back by --rollback and does not show under FINAL until it finishes, so
    --drop-pre-rebuild counts the copy's mutations and keeps it."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    assert _run(ch, "--rollback", "--yes") == tr.OK
    capsys.readouterr()
    ch.mutations = 1
    ch.cons = (0, 0, 0)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    assert (
        "1 mutation(s) on loom.memory_chunks_pre_rebuild since the copy" in capsys.readouterr().out
    )


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
    catch-up copy runs under the full copy's limit, not the gate reads' timeout, with the
    sequential reads of the checks that run before it."""
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
            **tr.CONSISTENT,
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
    assert f"embedding holds vectors of a width other than the target {DIM} (" in out
    assert "LOOM_EMBEDDING_DIM" in out
    assert "`loom reembed` does not cover loom.memory_chunks; its own writer must" in out
    assert "width 768: 3 row(s), width 3072: 1 row(s)" in out and "re-embed" in out
    preflight = [q for q in ch.queries if "GROUP BY w" in q]
    assert preflight and all(
        " FINAL " in q and f"length(embedding) != {DIM}" in q for q in preflight
    )
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    ch.widths = []
    assert _run(ch, "--yes") == tr.OK


def test_a_width_refusal_names_loom_reembed_only_for_the_tables_it_re_embeds(
    capsys: Any,
) -> None:
    ch = _CH(memory_embeddings=_live())
    ch.widths = [(768, 3)]
    flags = ["--table", "memory_embeddings", "--columns", "embedding", "--settle-s", "0"]
    assert vcr.main([*flags, "--dry-run"], ch=ch) == tr.REFUSED
    out = capsys.readouterr().out
    assert "re-embed to LOOM_EMBEDDING_DIM (`loom reembed --delete-old` re-embeds" in out
    assert "a width change with the same model goes through Settings -> Models & keys" in out
    assert "set LOOM_EMBEDDING_DIM to 768 and rerun" in out  # one other width: the model's


def test_an_empty_source_is_rebuilt_without_probes(capsys: Any) -> None:
    """A table never written is rebuilt at LOOM_EMBEDDING_DIM: the recall gate has nothing
    to probe, and a source that holds rows but no probe still fails it."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 0, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    out = capsys.readouterr().out
    assert "recall gate: loom.memory_chunks holds no rows, nothing to probe" in out
    assert any(c.startswith("EXCHANGE") for c in ch.commands)


def test_a_source_with_rows_but_no_probe_fails_the_gate(capsys: Any) -> None:
    """Rows whose vectors are all placeholders or another width give the recall gate
    nothing to probe; unlike an empty source the run stops before the swap."""
    ch = _CH(memory_chunks=_live())
    ch.no_probes = True
    assert _run(ch, "--yes") == tr.GATE_FAILED
    out = capsys.readouterr().out
    assert "GATE FAILED: no probe rows with a full-length vector" in out
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    step = json.loads(out.rsplit("summary ", 1)[1])["next"]
    assert "DROP TABLE loom.memory_chunks_rebuild SYNC" in step
    assert "skip its line" in step
    assert "--restart" not in step


def test_a_rollback_names_the_drop_as_its_next_step(capsys: Any) -> None:
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"}, memory_chunks_pre_rebuild=_live()
    )
    assert _run(ch, "--rollback", "--yes") == tr.OK
    step = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])["next"]
    assert "--drop-pre-rebuild --yes" in step and "keep writers and erasure stopped" in step


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


@pytest.mark.parametrize(
    ("flag", "bad"),
    [("--probes", "0"), ("--probes", "-1"), ("--gate-fetch-multiplier", "0")]
    + [("--gate-fetch-multiplier", v) for v in ("-1", "nan", "inf")],
)
def test_the_gate_flags_are_bounded(flag: str, bad: str) -> None:
    """No probe would leave a table with real vectors named as holding none."""
    with pytest.raises(SystemExit):
        vcr.parse(["--table", "t", "--columns", "embedding", flag, bad])


def test_the_printed_command_names_an_abort_file_outside_the_default() -> None:
    """The twin record lives beside the abort file, so a printed step that reads it names
    the run's abort file when it is not the default one."""
    base = ["--table", "memory_chunks", "--columns", "embedding"]
    job = vcr.VectorRebuild(_CH(memory_chunks=_live()), vcr.parse(base))
    assert "--abort-file" not in job.run_line
    job = vcr.VectorRebuild(
        _CH(memory_chunks=_live()), vcr.parse([*base, "--abort-file", "/x y/ABORT_REBUILD"])
    )
    assert job.run_line.endswith(" --abort-file '/x y/ABORT_REBUILD'")


def test_an_off_width_write_during_the_run_stops_the_swap(capsys: Any) -> None:
    """The width check runs again before the swap: a row of another width written after
    the preflight was zeroed in the twin, so the gate fails instead of serving it."""
    ch = _CH(memory_chunks=_live())
    ch.widths_first = [[]]
    ch.widths = [(768, 2)]
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "GATE FAILED: embedding holds vectors of a width other than the target" in out
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
    sync = seq.index("SYSTEM SYNC REPLICA loom.memory_chunks LIGHTWEIGHT", recall)
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
    assert "1 key(s) differ at one version; either table may hold the change" in out
    assert "holds a change" not in out and "inspect the difference" in out
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
    assert "POSTCONDITION FAILED: embedding holds vectors of a width other than the target" in out
    assert "--rollback --yes" in out
    assert "FROM loom.memory_chunks_pre_rebuild" in ch.queries[_last(ch, "GROUP BY w")]


def test_a_mutation_after_the_checks_fails_the_postcondition(capsys: Any) -> None:
    """A mutation created after the gate's last mutation check follows the rename onto the
    rollback copy; the new table may not hold it."""
    ch = _CH(memory_chunks=_live())
    # the preflight and the gate before the swap, then the new table after it
    ch.mutations_first = [0, 0, 0]
    ch.mutations = 1
    assert _run(ch, "--yes") == tr.POSTCONDITION
    assert "1 mutation(s) on loom.memory_chunks_pre_rebuild" in capsys.readouterr().out
    last = _last(ch, "system.mutations")
    assert "table = {t:String}" in ch.queries[last]
    assert ch.params[last]["t"] == "memory_chunks_pre_rebuild"


def test_a_mutation_rolled_back_shows_as_a_same_version_difference_in_the_drop(
    capsys: Any,
) -> None:
    """The swap stops the rollback copy's merges, so a mutation created after the last
    check applies only once --rollback starts them again. The postcondition says so, and
    the --drop-pre-rebuild that follows reports the touched keys as differing at one
    version without naming the new-layout copy as the one that holds the change."""
    ch = _CH(memory_chunks=_live())
    # the preflight and the gate before the swap, then the new table after it
    ch.mutations_first = [0, 0, 0]
    ch.mutations = 1
    assert _run(ch, "--yes") == tr.POSTCONDITION
    out = capsys.readouterr().out
    assert "A mutation applies after the rollback" in out
    assert "--rollback --yes" in json.loads(out.rsplit("summary ", 1)[1])["next"]
    ch.mutations_first = [0]  # the rollback's check: none on the new table, one on the copy
    assert _run(ch, "--rollback", "--yes") == tr.OK
    assert "SYSTEM START MERGES loom.memory_chunks" in ch.commands
    capsys.readouterr()
    ch.mutations = 0  # the tombstone finished and applied to the restored table
    ch.cons = (1, 0, 0)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "1 key(s) differ at one version; either table may hold the change" in out
    assert "a mutation the rollback carried back" in out
    assert "holds a change" not in out


def test_a_signal_during_the_postcondition_exits_130_and_keeps_the_copy(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM raises Interrupted where the run is; the checks after the swap pass it on
    rather than reporting checks that could not run."""

    def interrupted(*_: Any) -> Any:
        raise tr.Interrupted("signal 15")

    monkeypatch.setattr(vcr.VectorRebuild, "rollback_copy_change", interrupted)
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.INTERRUPTED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "INTERRUPTED: signal 15" in out and "the checks could not run" not in out


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


def test_a_plain_column_of_another_width_refuses_rather_than_coding_it(capsys: Any) -> None:
    """Every stored vector is 768 wide and LOOM_EMBEDDING_DIM is DIM: coding 768 would make
    the column reject every write Loom makes, so the run codes DIM and refuses."""
    ch = _CH(memory_chunks=_live())
    ch.widths = [(768, 90)]  # every real vector is of another width
    assert _run(ch, "--dry-run") == tr.REFUSED
    assert not any("_rebuild" in c for c in ch.commands)
    out = capsys.readouterr().out
    assert f"target {DIM} (width 768: 90 row(s)" in out
    assert f"LOOM_EMBEDDING_DIM {DIM}" in out


def test_the_recall_reads_rank_real_vectors_only() -> None:
    """A zero placeholder's cosine distance is NaN and sorts last: left in, a namespace
    with fewer than 20 real vectors would read recall 0, and a placeholder picked as a
    probe would too."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    reads = [q for q in ch.queries if "round(cosineDistance" in q]
    assert len(reads) == 2  # the exact read on the source and the twin read
    assert all(
        f"length(embedding) = {DIM} AND arrayExists(x -> x != 0, embedding) ORDER BY" in q
        for q in reads
    )
    probe = next(q for q in ch.queries if "ORDER BY rand()" in q)
    assert vcr.vector_filter("embedding", dim=DIM) in probe
    # an older version of a row whose newest one is a placeholder is never a probe
    assert "FROM loom.memory_chunks FINAL WHERE" in probe


def test_unfinished_mutations_refuse_the_rebuild(capsys: Any) -> None:
    ch = _CH(memory_chunks=_live())
    ch.mutations = 1
    assert _run(ch, "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "a mutation still running or one created under a minute ago (the clock-skew margin)" in (
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


def test_the_preflight_counts_mutations_since_a_reused_twin_was_created(capsys: Any) -> None:
    """With --reuse the gate before the swap counts from the twin's creation; the preflight
    counts from the same time, so a mutation finished since then refuses before the delta."""
    ch = _CH(memory_chunks=_live(), memory_chunks_rebuild=_twin())
    ch.mutations = 1
    assert _run(ch, "--yes", "--reuse") == tr.REFUSED
    out = capsys.readouterr().out
    assert _writes(ch) == [] and "rerun in a minute" not in out
    assert "makes the twin stale; rerun with --restart --yes" in out
    read = next(q for q in ch.queries if "system.mutations" in q)
    margin = TWIN_CREATED - vcr.CLOCK_SKEW_MARGIN_S
    assert f"(NOT is_done OR create_time >= toDateTime({margin}))" in read


@pytest.mark.parametrize(("flags", "since"), [((), COPY_STARTED), (("--reuse",), TWIN_CREATED)])
def test_a_mutation_since_the_copy_stops_the_swap_even_when_done(
    flags: tuple[str, ...], since: int, capsys: Any
) -> None:
    """An erasure tombstone is an ALTER UPDATE that keeps the key and its version and may
    finish before the swap; any mutation created since the copy (or since a reused twin
    was created) fails the gate, done or not."""
    twin = {"memory_chunks_rebuild": _twin()} if flags else {}
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
    out = capsys.readouterr().out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert "--rollback --yes" in summary["next"] and "--drop-pre-rebuild" not in summary["next"]
    assert "writers and erasure stopped" in summary["next"]
    assert "Keep writers and erasure stopped" in out.rsplit("summary ", 1)[0]
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    assert not any(c.startswith("DROP TABLE") for c in ch.commands)
    out = capsys.readouterr().out
    assert "loom.memory_chunks_pre_rebuild is kept" in out and "--rollback --yes" in out
    ch.cons = (0, 0, 0)  # the two tables match: the copy can go
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    assert "memory_chunks_pre_rebuild" not in ch.t


def test_a_swap_names_its_next_step_with_the_run_s_layout_flags_in_the_summary(
    capsys: Any,
) -> None:
    """The printed --drop-pre-rebuild runs as written: without the run's --method it would
    check the tables against the default layout and refuse. A float flag prints as the
    value passed, not rounded to six digits."""
    ch = _CH(memory_chunks=_live())
    flags = ("--method", "int8", "--index-granularity", "512", "--cluster", "")
    gates = ("--gate-fetch-multiplier", "10", "--probes", "40", "--require-patch-free")
    assert _run(ch, *flags, *gates, "--statement-timeout-s", "1234.5678", "--yes") == tr.OK
    summary = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])
    assert summary["exit"] == tr.OK and "writers and erasure stopped" in summary["next"]
    assert (
        "--method int8 --index-granularity 512 --gate-fetch-multiplier 10.0 --probes 40 "
        "--require-patch-free --cluster '' --statement-timeout-s 1234.5678 "
        "--drop-pre-rebuild --yes"
    ) in summary["next"]
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    summary = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])
    assert "--gate-fetch-multiplier" not in summary["next"] and "--probes" not in summary["next"]
    assert "--require-patch-free" not in summary["next"]


def test_columns_that_differ_by_name_after_the_swap_keep_both_tables(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column added to either table after the swap shows the same way, so the run names
    no rollback, which would drop a column added to the new table."""
    columns = tr.columns

    def added_to_one(ch: Any, table: str) -> dict[str, tuple[Any, ...]]:
        cols = columns(ch, table)
        return {**cols, "added": ("String", "", "", "")} if table.endswith("_pre_rebuild") else cols

    monkeypatch.setattr(tr, "columns", added_to_one)
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.POSTCONDITION
    summary = json.loads(capsys.readouterr().out.rsplit("summary ", 1)[1])
    assert "inspect the difference" in summary["next"] and "--rollback --yes" not in summary["next"]


def test_drop_pre_rebuild_keeps_both_tables_when_their_columns_differ_by_name(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column added to either table after the swap is refused as a difference, never as
    checks that could not run, and the drop names no rollback."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    capsys.readouterr()
    columns = tr.columns

    def added_to_one(ch: Any, table: str) -> dict[str, tuple[Any, ...]]:
        cols = columns(ch, table)
        return {**cols, "added": ("String", "", "", "")} if table.endswith("_pre_rebuild") else cols

    monkeypatch.setattr(tr, "columns", added_to_one)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "fix the grant" not in out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert vcr.KEEP_BOTH in summary["next"] and "--rollback --yes" not in summary["next"]


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
    assert (
        "has({empty:Array(String)}, table) AND log_pointer = 0 AND queue_size = 0"
        in vcr.REPLICA_SYNCED_SQL
    )
    assert "system.replication_queue" in vcr.REPLICA_SYNCED_SQL


def test_only_a_log_keeper_lists_empty_passes_on_zero_log_pointers() -> None:
    """A replica that has not pulled a log of one entry reads log_pointer = log_max_index
    = 0, as an empty log does; Keeper's list of the log's entries tells them apart."""
    entries = {"/t/a/log": 0, "/t/b/log": 1}

    class _Job:
        def q(self, sql: str, parameters: Any = None, **st: Any) -> list[Any]:
            if "zookeeper_path" in sql:
                return [("a", "/t/a/"), ("b", "/t/b")]
            return [(entries[parameters["p"]],)]

    job: Any = _Job()
    assert vcr.VectorRebuild.empty_logs(job, ["a", "b"]) == ["a"]


def test_a_user_who_cannot_read_system_zookeeper_is_refused(capsys: Any) -> None:
    """The drained reads on a ReplicatedMergeTree read the replication log in
    system.zookeeper; without that grant the preflight refuses (exit 3) and names it."""

    class _NoKeeper(_CH):
        def _answer(self, sql: str, p: dict[str, Any]) -> _R:
            if "zookeeper_path" in sql:
                return _R([("memory_chunks", "/t/memory_chunks")])
            if "system.zookeeper" in sql:
                raise RuntimeError("Not enough privileges")
            return super()._answer(sql, p)

    ch = _NoKeeper(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    assert "the run needs SELECT on system.zookeeper" in capsys.readouterr().out


def test_a_user_who_cannot_read_system_replication_queue_is_refused(capsys: Any) -> None:
    """The drained reads on a ReplicatedMergeTree read system.replication_queue; without
    that grant the preflight refuses (exit 3) and names it."""

    class _NoQueue(_CH):
        def _answer(self, sql: str, p: dict[str, Any]) -> _R:
            if "system.replication_queue" in sql:
                raise RuntimeError("Not enough privileges")
            return super()._answer(sql, p)

    ch = _NoQueue(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.REFUSED and _writes(ch) == []
    assert "the run needs SELECT on system.replication_queue" in capsys.readouterr().out


def test_each_gate_read_of_a_replicated_twin_follows_its_sync() -> None:
    """The copy, a delta or a repair writes log entries to the twin this replica may not
    have pulled yet: each consistency read before the swap syncs the twin first."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.cons_first = [(0, 1, 0)]  # the first read finds a key to repair
    assert _run(ch, "--yes") == tr.OK
    seq = ch.sequence
    swap = seq.index("EXCHANGE TABLES loom.memory_chunks AND loom.memory_chunks_rebuild")
    reads = [i for i, q in enumerate(seq[:swap]) if "FULL OUTER JOIN" in q]
    assert len(reads) == 3
    sync = "SYSTEM SYNC REPLICA loom.memory_chunks_rebuild LIGHTWEIGHT"
    assert any(
        q.startswith("INSERT INTO loom.memory_chunks_rebuild") for q in seq[reads[0] : reads[1]]
    )
    for i in reads:
        last = max(j for j in range(i) if seq[j] == sync)
        assert not any(q.startswith("INSERT INTO loom.memory_chunks_rebuild") for q in seq[last:i])


def test_a_refused_twin_sync_fails_the_gate(capsys: Any) -> None:
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    ch.refuse_table = "loom.memory_chunks_rebuild"
    assert _run(ch, "--yes") == tr.GATE_FAILED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "one cause is a missing grant on the twin" in capsys.readouterr().out


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


def test_a_rollback_whose_rollback_copy_sync_is_refused_names_the_rerun(capsys: Any) -> None:
    """Checks that could not run found no difference: the rollback keeps both tables and
    names the grant fix and a rerun of --rollback, not an inspection by hand."""
    coded = REPLICATED.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(
        memory_chunks={"ddl": coded, "rows": 5, "uuid": "a"},
        memory_chunks_pre_rebuild={"ddl": REPLICATED, "rows": 5, "uuid": "live"},
    )
    ch.refuse_table = "loom.memory_chunks_pre_rebuild"
    assert _run(ch, "--rollback", "--yes") == tr.REFUSED
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "the checks could not run (Not enough privileges)" in out
    summary = json.loads(out.rsplit("summary ", 1)[1])
    assert "fix the grant" in summary["next"] and "--rollback --yes" in summary["next"]
    assert "by hand" not in summary["next"]


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


def test_a_delete_in_the_new_table_after_the_swap_never_names_the_rollback(
    capsys: Any,
) -> None:
    """A delete in the new table after the swap leaves keys only in the rollback copy, as a
    write to the old table would; the new table's mutation tells them apart, and
    --rollback would bring the deleted rows back."""
    ch = _CH(memory_chunks=_live())
    assert _run(ch, "--yes") == tr.OK
    capsys.readouterr()
    ch.cons = (0, 1, 0)
    ch.mutations_first = [1]  # the new table's lightweight DELETE
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "1 mutation(s) on loom.memory_chunks after the copy started" in out
    assert "--rollback --yes" not in out
    assert vcr.KEEP_BOTH in json.loads(out.rsplit("summary ", 1)[1])["next"]
    assert ch.params[_last(ch, "system.mutations")]["t"] == "memory_chunks"


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


def test_drop_pre_rebuild_after_a_rebuild_on_the_target_layout_keeps_a_change_only_the_copy_holds(
    capsys: Any,
) -> None:
    """A --require-patch-free run rebuilds a table already on the target layout, so both
    tables hold it: --drop-pre-rebuild still compares them, after the swap and after a
    rollback, and keeps the copy while it holds a change the live table lacks."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(memory_chunks={"ddl": coded, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes", "--require-patch-free") == tr.OK
    capsys.readouterr()
    ch.cons = (0, 1, 0)  # a key only the rollback copy holds
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    assert "--rollback --yes" in capsys.readouterr().out
    ch.cons = (0, 0, 0)
    assert _run(ch, "--rollback", "--yes") == tr.OK
    ch.cons = (0, 0, 1)  # a key only the new-layout copy holds after the rollback
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    out = capsys.readouterr().out
    assert "loom.memory_chunks_pre_rebuild holds a change loom.memory_chunks lacks" in out
    assert not any(c.startswith("DROP TABLE") for c in ch.commands)


def test_drop_pre_rebuild_on_the_target_layout_keeps_both_tables_created_in_the_same_second(
    capsys: Any,
) -> None:
    """Metadata times have one second's precision, so equal times do not show whether the
    tables were swapped or rolled back: --drop-pre-rebuild keeps the copy."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(memory_chunks={"ddl": coded, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes", "--require-patch-free") == tr.OK
    capsys.readouterr()
    ch.t["memory_chunks"]["created"] = ch.t["memory_chunks_pre_rebuild"]["created"] = COPY_STARTED
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "memory_chunks_pre_rebuild" in ch.t
    assert "created in the same second" in capsys.readouterr().out
    assert not any(c.startswith("DROP TABLE") for c in ch.commands)


def test_rollback_on_the_target_layout_keeps_both_tables_on_a_shared_database(
    capsys: Any,
) -> None:
    """EXCHANGE TABLES may not keep each table's own metadata time on a Shared database,
    so the times do not show which table is the new one: --rollback and
    --drop-pre-rebuild keep both tables."""
    coded = ARRAY.replace("CODEC(NONE)", f"CODEC(Quantized('rabitq', {DIM}, 0))")
    ch = _CH(memory_chunks={"ddl": coded, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes", "--require-patch-free") == tr.OK
    capsys.readouterr()
    ch.db_engine = "Shared"
    swap_commands = len(ch.commands)
    assert _run(ch, "--rollback", "--yes") == tr.REFUSED
    assert "other than Atomic or Replicated" in capsys.readouterr().out
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.REFUSED
    assert "other than Atomic or Replicated" in capsys.readouterr().out
    assert "memory_chunks_pre_rebuild" in ch.t
    after_swap = ch.commands[swap_commands:]
    assert not any(c.startswith(("DROP TABLE", "EXCHANGE")) for c in after_swap)


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


def test_a_replicated_live_table_is_synced_lightweight_in_every_drain() -> None:
    """A full sync waits on the merges still queued on the live table; the reads need only
    the fetched parts, so the preflight, the drain before the swap and --drop-pre-rebuild
    sync it LIGHTWEIGHT."""
    ch = _CH(memory_chunks={"ddl": REPLICATED, "rows": 5, "uuid": "live"})
    assert _run(ch, "--yes") == tr.OK
    seq = ch.sequence
    sync = "SYSTEM SYNC REPLICA loom.memory_chunks LIGHTWEIGHT"
    copy = next(
        i for i, q in enumerate(seq) if q.startswith("INSERT INTO loom.memory_chunks_rebuild")
    )
    swap = seq.index("EXCHANGE TABLES loom.memory_chunks AND loom.memory_chunks_rebuild")
    assert sync in seq[:copy] and sync in seq[copy:swap]
    assert "SYSTEM SYNC REPLICA loom.memory_chunks" not in seq
    start = len(seq)
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tr.OK
    assert sync in ch.sequence[start:]


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
