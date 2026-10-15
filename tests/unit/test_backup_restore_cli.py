"""dev/backup_loom.py and dev/restore_loom.py against a scripted client.

A backup records every table's file, row count, byte size and SHA-256 in a
manifest written last; a failed table leaves the manifest incomplete and the
exit code 1; an org backup never carries the org-less idempotency cache.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import backup_loom, restore_loom  # noqa: E402
from dev._ch_target import CHTarget  # noqa: E402

TARGET = CHTarget(host="ch.test", port=8123, user="loom", database="loom", secure=False)
# The tables the fake server lists (TABLES_SQL), with whether each has an `org` column.
SCHEMA = {
    "audit_log": True,
    "chunks": True,
    "idempotency": False,
    "memory": True,
    "namespaces": True,
}


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClient:
    """raw_stream answers from `tables`; a table in `fail` raises, a table in
    `poison` streams a row then a ClickHouse exception line."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        *,
        fail: frozenset[str] = frozenset(),
        poison: frozenset[str] = frozenset(),
    ) -> None:
        self.tables = tables
        self.fail = fail
        self.poison = poison
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False
        # (view, source, target): chunks feeds audit_log, a stand-in edge.
        self.views: list[tuple[str, str, str]] = []

    def raw_stream(
        self, sql: str, parameters: dict[str, Any] | None = None, settings: Any = None
    ) -> io.BytesIO:
        # String columns leave as raw bytes whatever the user's profile says.
        assert settings == {"output_format_json_validate_utf8": 0}
        self.queries.append((sql, parameters))
        table = sql.split(" FROM ", 1)[1].split()[0]
        if table in self.fail:
            raise RuntimeError(f"Code: 60. Unknown table {table}")
        rows = self.tables.get(table, [])
        if parameters and "org" in parameters:
            rows = [r for r in rows if r.get("org") == parameters["org"]]
        body = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
        if table in self.poison:
            body += b"Code: 241. DB::Exception: Memory limit exceeded\n"
        return io.BytesIO(body)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        assert "system.tables" in sql and parameters
        if sql == backup_loom.TABLES_SQL:
            return _Result(list(SCHEMA.items()))
        if sql == backup_loom.VIEW_SOURCES_SQL:
            return _Result([(src, view) for view, src, _tgt in self.views])
        if sql == backup_loom.VIEW_DDL_SQL:
            return _Result(
                [
                    (v, f"CREATE MATERIALIZED VIEW loom.{v} TO loom.{t} AS SELECT")
                    for v, _s, t in self.views
                ]
            )
        ddl = f"CREATE TABLE {parameters['db']}.{parameters['t']} (`org` String) ENGINE = MergeTree ORDER BY org"
        return _Result([(ddl,)])

    def close(self) -> None:
        self.closed = True


def _run(monkeypatch: pytest.MonkeyPatch, client: FakeClient, argv: list[str]) -> int:
    monkeypatch.setattr(backup_loom, "resolve_target", lambda _args: TARGET)
    monkeypatch.setattr(CHTarget, "client", lambda self, **_: client)
    return backup_loom.main(argv)


def _rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "memory": [{"org": "a", "memory_key": "k1"}, {"org": "b", "memory_key": "k2"}],
        "audit_log": [{"org": "a", "event_type": "memory.set"}],
        "idempotency": [{"idempotency_key": "i1", "response_body": "{}"}],
    }


def test_manifest_records_size_rows_and_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient(_rows())
    out = tmp_path / "b"
    assert _run(monkeypatch, client, ["--out-dir", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["backup_version"] == backup_loom.BACKUP_VERSION
    assert manifest["complete"] is True
    assert manifest["source"] == TARGET.describe()
    assert manifest["source_database"] == "loom"
    assert [e["name"] for e in manifest["tables"]] == list(SCHEMA)
    assert {e["name"]: e["scoped"] for e in manifest["tables"]} == SCHEMA
    for entry in manifest["tables"]:
        data = (out / entry["file"]).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["rows"] == data.count(b"\n")
        assert entry["create_table_query"].startswith(f"CREATE TABLE loom.{entry['name']} ")
    assert client.closed
    # Unqualified table names: the client's database is the one read.
    assert all(" FROM loom." not in sql for sql, _ in client.queries)


def test_the_table_list_leaves_out_runner_tables_leases_and_views() -> None:
    sql = backup_loom.TABLES_SQL
    assert "engine LIKE '%MergeTree'" in sql
    assert "NOT startsWith(t.name, '_')" in sql and "NOT startsWith(t.name, '.')" in sql


def test_a_failed_table_makes_the_backup_incomplete_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient(_rows(), fail=frozenset({"chunks"}), poison=frozenset({"memory"}))
    out = tmp_path / "b"
    assert _run(monkeypatch, client, ["--out-dir", str(out)]) == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["complete"] is False
    errors = {e["name"]: e["error"] for e in manifest["tables"] if "error" in e}
    assert set(errors) == {"chunks", "memory"}
    assert "JSON row" in errors["memory"]
    assert not (out / "memory.jsonl").exists()  # a half-written file is not left behind
    assert "backup incomplete" in capsys.readouterr().err


def test_org_backup_filters_and_skips_the_org_less_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient(_rows())
    out = tmp_path / "b"
    assert _run(monkeypatch, client, ["--out-dir", str(out), "--org", "a"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    by_name = {e["name"]: e for e in manifest["tables"]}
    assert "skipped" in by_name["idempotency"]
    assert not (out / "idempotency.jsonl").exists()
    assert by_name["memory"]["rows"] == 1
    assert all(p == {"org": "a"} for _, p in client.queries)


def test_existing_backup_and_unknown_tables_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    out.mkdir()
    (out / "manifest.json").write_text("{}")
    assert _run(monkeypatch, FakeClient({}), ["--out-dir", str(out)]) == 2
    client = FakeClient({})
    assert _run(monkeypatch, client, ["--out-dir", str(tmp_path / "c"), "--tables", "x"]) == 2
    assert client.queries == []


def test_tables_subset_keeps_the_server_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    argv = ["--out-dir", str(out), "--tables", "memory,audit_log"]
    assert _run(monkeypatch, FakeClient(_rows()), argv) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert [e["name"] for e in manifest["tables"]] == ["audit_log", "memory"]


def test_exclude_leaves_tables_out_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    assert (
        _run(monkeypatch, FakeClient(_rows()), ["--out-dir", str(out), "--exclude", "chunks"]) == 0
    )
    manifest = json.loads((out / "manifest.json").read_text())
    by_name = {e["name"]: e for e in manifest["tables"]}
    assert by_name["chunks"]["skipped"] == "excluded (--exclude)"
    assert not (out / "chunks.jsonl").exists() and "rows" in by_name["memory"]


def test_drill_lists_the_tables_the_backup_does() -> None:
    """scripts/ops/backup_drill.sh checks one file per table the server lists,
    with the backup's own filter."""
    drill = (ROOT / "scripts" / "ops" / "backup_drill.sh").read_text()
    for clause in (
        "engine LIKE '%MergeTree'",
        "NOT startsWith(name, '_')",
        "NOT startsWith(name, '.')",
    ):
        assert clause in drill


# ---- restore ---------------------------------------------------------------


class RestoreClient:
    """A scripted target server: `databases` maps each database to its
    tables and their row counts. Records commands and inserts."""

    def __init__(self, databases: dict[str, dict[str, int]], *, fail_on: str = "") -> None:
        self.databases = databases
        self.fail_on = fail_on
        self.commands: list[str] = []
        self.inserted: dict[str, bytes] = {}
        self.closed = False
        # table -> (database engine, CREATE statement) for the staging copy.
        self.engines: dict[str, tuple[str, str]] = {}

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "create_table_query" in sql:
            got = self.engines.get(parameters["t"])
            return _Result([got] if got else [])
        if "system.databases" in sql:
            return _Result([(int(parameters["db"] in self.databases),)])
        if "system.tables" in sql:
            return _Result([(t,) for t in self.databases.get(parameters["db"], {})])
        db, table = sql.split(" FROM ", 1)[1].split()[0].replace("`", "").split(".")
        return _Result([(self.databases[db][table],)])

    def command(self, sql: str, settings: Any = None) -> None:
        self.commands.append(sql)

    def raw_insert(
        self, *, table: str, insert_block: Any, fmt: str, settings: dict[str, Any]
    ) -> None:
        # optimize_on_insert = 0: a ReplacingMergeTree file restores every row.
        assert fmt == "JSONEachRow" and settings == {
            "max_partitions_per_insert_block": 0,
            "optimize_on_insert": 0,
        }
        assert "`_restore_" in table  # every file loads into a staging table
        table = re.sub(r"`_restore_[0-9a-f]{8}_", "`", table)
        if table.endswith(f"`{self.fail_on}`"):
            raise RuntimeError("Code: 27. Cannot parse input")
        self.inserted[table] = insert_block.read()

    def close(self) -> None:
        self.closed = True


def _backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    out = tmp_path / "b"
    assert _run(monkeypatch, FakeClient(_rows()), ["--out-dir", str(out)]) == 0
    return out


def _restore(
    monkeypatch: pytest.MonkeyPatch, client: RestoreClient, argv: list[str], *, db: str = "drill"
) -> int:
    target = CHTarget(host="ch.test", port=8123, user="loom", database=db, secure=False)
    monkeypatch.setattr(restore_loom, "resolve_target", lambda _args: target)
    monkeypatch.setattr(restore_loom, "live_databases", lambda: {"loom"})
    monkeypatch.setattr(CHTarget, "client", lambda self, **_: client)
    return restore_loom.main(argv)


LOCK = "`default`.`_loom_restore_lock_drill`"


def _empty(db: str = "drill") -> dict[str, dict[str, int]]:
    return {db: dict.fromkeys(SCHEMA, 0)}


def test_restore_inserts_into_the_named_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--skip-idempotency"]) == 0
    assert set(client.inserted) == {"`drill`.`memory`", "`drill`.`audit_log`"}
    assert client.inserted["`drill`.`memory`"] == (b / "memory.jsonl").read_bytes()
    assert not any(c.startswith("CREATE DATABASE") for c in client.commands)  # it existed
    assert client.closed


def test_restore_attaches_each_file_from_a_staging_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An attach fires no materialized view: a restored memory row is not
    copied into memory_versions on top of the restored history, and no view is
    detached (which a Replicated database refuses). The staging table goes on
    every path."""
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient(_empty(), fail_on="memory")
    monkeypatch.setattr(restore_loom.secrets, "token_hex", lambda n: "0a1b2c3d")
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 1
    audit = [c for c in client.commands if "audit_log" in c]
    staging = "`drill`.`_restore_0a1b2c3d_audit_log`"
    assert audit == [
        f"CREATE TABLE {staging} AS `drill`.`audit_log`",
        f"SYSTEM STOP MERGES {staging}",
        f"ALTER TABLE `drill`.`audit_log` ATTACH PARTITION ALL FROM {staging}",
        f"DROP TABLE IF EXISTS {staging} SYNC",
    ]
    assert client.commands[-2:] == [
        "DROP TABLE IF EXISTS `drill`.`_restore_0a1b2c3d_memory` SYNC",
        f"DROP TABLE IF EXISTS {LOCK} SYNC",  # the lock goes on a failed run too
    ]
    assert not any(c.startswith(("DETACH", "ATTACH TABLE")) for c in client.commands)


def test_restore_creates_the_database_and_tables_from_the_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient({})
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 0
    assert client.commands[0].startswith(f"CREATE TABLE {LOCK} ")
    assert client.commands[1] == "CREATE DATABASE IF NOT EXISTS `drill`"
    creates = [c for c in client.commands[2:] if c.startswith("CREATE TABLE IF NOT EXISTS")]
    assert len(creates) == len(SCHEMA)
    assert creates[0].startswith("CREATE TABLE IF NOT EXISTS `drill`.`audit_log` (")
    assert all("loom." not in c for c in creates)
    assert set(client.inserted) == {
        "`drill`.`memory`",
        "`drill`.`audit_log`",
        "`drill`.`idempotency`",
    }


def test_dry_run_plans_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient({})
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--dry-run"]) == 0
    assert client.commands == [] and client.inserted == {}
    out = capsys.readouterr().out
    assert "create database drill" in out and "create table" in out


def test_the_live_database_needs_into_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient(_empty("loom"))
    assert _restore(monkeypatch, client, ["--in-dir", str(b)], db="loom") == 2
    assert client.commands == [] and client.inserted == {}
    assert "--into-live" in capsys.readouterr().err
    argv = ["--in-dir", str(b), "--into-live"]
    assert _restore(monkeypatch, client, argv, db="loom") == 0
    assert "`loom`.`memory`" in client.inserted


def test_a_table_with_rows_in_scope_needs_into_nonempty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = _backup(monkeypatch, tmp_path)
    dbs = _empty()
    dbs["drill"]["audit_log"] = 5
    client = RestoreClient(dbs)
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 2
    assert client.inserted == {}
    assert "audit_log" in capsys.readouterr().err
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--into-nonempty"]) == 0
    assert "`drill`.`audit_log`" in client.inserted


@pytest.mark.parametrize(
    "tamper",
    [
        lambda b: (b / "memory.jsonl").write_bytes(b'{"org": "evil"}\n{"org": "b"}\n'),
        lambda b: (b / "memory.jsonl").unlink(),
        lambda b: (b / "manifest.json").unlink(),
        lambda b: (b / "manifest.json").write_text(
            json.dumps({**json.loads((b / "manifest.json").read_text()), "backup_version": 1})
        ),
    ],
    ids=["content-changed", "file-missing", "no-manifest", "old-version"],
)
def test_a_backup_that_fails_verification_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: Any
) -> None:
    b = _backup(monkeypatch, tmp_path)
    tamper(b)
    client = RestoreClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 2
    assert client.inserted == {} and client.commands == []


def test_incomplete_backup_is_refused_unless_the_good_tables_are_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    failing = FakeClient(_rows(), fail=frozenset({"chunks"}))
    assert _run(monkeypatch, failing, ["--out-dir", str(out)]) == 1
    client = RestoreClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(out)]) == 2
    assert client.inserted == {}
    assert _restore(monkeypatch, client, ["--in-dir", str(out), "--tables", "memory"]) == 0
    assert set(client.inserted) == {"`drill`.`memory`"}


def test_an_odd_database_name_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient({})
    assert _restore(monkeypatch, client, ["--in-dir", str(b)], db="x`; DROP") == 2
    assert client.commands == []


def test_failed_insert_stops_and_reports_what_landed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient(_empty(), fail_on="idempotency")
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 1
    assert set(client.inserted) == {"`drill`.`audit_log`"}  # memory comes after idempotency
    err = capsys.readouterr().err
    assert "partial restore" in err and "audit_log" in err


def test_retarget_ddl_rewrites_only_the_head() -> None:
    ddl = (
        "CREATE TABLE loom.memory (`v` String DEFAULT 'loom.memory') ENGINE = MergeTree ORDER BY v"
    )
    out = restore_loom.retarget_ddl(ddl, "drill", "memory")
    assert out.startswith("CREATE TABLE IF NOT EXISTS `drill`.`memory` (")
    assert "DEFAULT 'loom.memory'" in out
    with pytest.raises(restore_loom.Refused):
        restore_loom.retarget_ddl("CREATE VIEW loom.v AS SELECT 1", "drill", "v")


def test_a_replicated_tables_staging_copy_is_local() -> None:
    """A copy of a self-managed replicated table would claim its Keeper path
    again (REPLICA_ALREADY_EXISTS): the staging copy takes the local engine
    with the same parameters."""
    ddl = (
        "CREATE TABLE drill.t (`k` UInt32, `ver` UInt64) ENGINE = "
        "ReplicatedReplacingMergeTree('/clickhouse/tables/t', 'r1', ver) ORDER BY k"
    )
    out = restore_loom.staging_engine_ddl(ddl, "`drill`.`_restore_t`")
    assert out == (
        "CREATE TABLE `drill`.`_restore_t` (`k` UInt32, `ver` UInt64) ENGINE = "
        "ReplacingMergeTree(ver) ORDER BY k"
    )
    plain = "ReplicatedMergeTree('/p/{uuid}', '{replica}') ORDER BY k"
    assert restore_loom.staging_engine_ddl(f"CREATE TABLE d.t (k UInt8) ENGINE = {plain}", "s") == (
        "CREATE TABLE s (k UInt8) ENGINE = MergeTree() ORDER BY k"
    )
    assert restore_loom.staging_engine_ddl(ddl.replace("Replicated", ""), "s") is None


def test_the_restore_stages_a_replicated_table_locally_unless_the_database_is_replicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _backup(monkeypatch, tmp_path)
    monkeypatch.setattr(restore_loom.secrets, "token_hex", lambda n: "0a1b2c3d")
    ddl = (
        "CREATE TABLE drill.memory (`v` String) ENGINE = ReplicatedMergeTree('/m', 'r') ORDER BY v"
    )
    staging = "`drill`.`_restore_0a1b2c3d_memory`"
    for db_engine, want in (
        ("Atomic", f"CREATE TABLE {staging} (`v` String) ENGINE = MergeTree()"),
        ("Replicated", f"CREATE TABLE {staging} AS `drill`.`memory`"),
    ):
        client = RestoreClient(_empty())
        client.engines = {"memory": (db_engine, ddl)}
        argv = ["--in-dir", str(b), "--tables", "memory"]
        assert _restore(monkeypatch, client, argv) == 0
        assert any(c.startswith(want) for c in client.commands), client.commands


def test_documents_and_their_originals_restore_together() -> None:
    """No view joins them, but documents without document_blobs 410 every
    original, and originals without their documents are what the sweep deletes."""
    plan = [restore_loom.TablePlan(name="documents", path=Path("x"), rows=1, ddl="")]
    with pytest.raises(restore_loom.Refused, match="documents without document_blobs"):
        restore_loom.check_view_split(plan, [])
    plan = [restore_loom.TablePlan(name="document_blobs", path=Path("x"), rows=1, ddl="")]
    with pytest.raises(restore_loom.Refused, match="document_blobs without documents"):
        restore_loom.check_view_split(plan, [])
    both = [
        restore_loom.TablePlan(name=n, path=Path("x"), rows=1, ddl="")
        for n in ("documents", "document_blobs")
    ]
    restore_loom.check_view_split(both, [])


def test_documents_are_dumped_and_restored_before_their_originals() -> None:
    """With workers running on a live database, the blob sweep deletes an
    original no document names; restored first, the documents name them."""
    available = {"audit_log": True, "document_blobs": True, "documents": True, "memory": True}
    tables, _ = backup_loom.plan_tables(available, None, None)
    assert tables == ["documents", "audit_log", "document_blobs", "memory"]
    # A backup written before that order lists the tables alphabetically.
    manifest = {"tables": [{"name": n} for n in sorted(available)]}
    order = [e["name"] for e in restore_loom.select_tables(manifest, None, False)]
    assert order == ["documents", "audit_log", "document_blobs", "memory"]


def test_a_row_with_non_utf8_bytes_is_a_complete_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class RawClient(FakeClient):
        def raw_stream(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
            return io.BytesIO(b'{"org":"a","row_hash":"\xcd\xf3j"}\n')

    out = tmp_path / "b"
    assert _run(monkeypatch, RawClient({}), ["--out-dir", str(out), "--tables", "audit_log"]) == 0


# ---- materialized views ------------------------------------------------------


def _with_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> Path:
    """A backup whose server has one insert view: memory → audit_log."""
    client = FakeClient(_rows())
    client.views = [("memory_mv", "memory", "audit_log")]
    out = tmp_path / "b"
    assert _run(monkeypatch, client, ["--out-dir", str(out), *argv]) == 0
    return out


def test_the_manifest_records_each_views_source_and_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _with_view(monkeypatch, tmp_path)
    manifest = json.loads((b / "manifest.json").read_text())
    assert manifest["views"] == [{"view": "memory_mv", "source": "memory", "target": "audit_log"}]


def test_excluding_a_source_excludes_what_its_views_fill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rebuilding an excluded source refills its views' targets, so a restored
    copy of them would double."""
    b = _with_view(monkeypatch, tmp_path, "--exclude", "memory")
    by_name = {e["name"]: e for e in json.loads((b / "manifest.json").read_text())["tables"]}
    assert by_name["audit_log"]["skipped"] == "filled by a view from excluded memory"
    assert not (b / "audit_log.jsonl").exists()


@pytest.mark.parametrize(
    "tables, says",
    [
        ("memory", "memory without audit_log: memory_mv copies no restored memory row"),
        ("audit_log", "audit_log without memory: memory_mv fills audit_log again"),
    ],
)
def test_a_selection_that_splits_a_view_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tables: str,
    says: str,
) -> None:
    """An attach fires no view: memory restored alone leaves memory_versions
    without its history, and a derived table restored alone doubles when its
    source is rebuilt."""
    b = _with_view(monkeypatch, tmp_path)
    client = RestoreClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--tables", tables]) == 2
    assert says in capsys.readouterr().err and client.inserted == {}
    argv = ["--in-dir", str(b), "--tables", tables, "--allow-view-split"]
    assert _restore(monkeypatch, client, argv) == 0
    both = ["--in-dir", str(b), "--tables", "memory,audit_log"]
    assert _restore(monkeypatch, RestoreClient(_empty()), both) == 0


def test_restore_exclude_leaves_tables_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _with_view(monkeypatch, tmp_path)
    client = RestoreClient(_empty())
    argv = ["--in-dir", str(b), "--exclude", "memory,audit_log", "--skip-idempotency"]
    assert _restore(monkeypatch, client, argv) == 0
    assert client.inserted == {}
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--exclude", "nope"]) == 2


def test_an_older_manifest_reads_the_views_of_its_source_database() -> None:
    class _Views:
        def query(self, sql: str, parameters: Any = None) -> _Result:
            if sql == backup_loom.VIEW_SOURCES_SQL:
                return _Result([("memory", "memory_versions_mv")])
            return _Result(
                [
                    (
                        "memory_versions_mv",
                        "CREATE MATERIALIZED VIEW loom.memory_versions_mv "
                        "TO loom.memory_versions (`org` String) AS SELECT",
                    )
                ]
            )

    edges = restore_loom.view_edges(_Views(), {"source_database": "loom"})
    assert edges == [
        {"view": "memory_versions_mv", "source": "memory", "target": "memory_versions"}
    ]
    assert restore_loom.view_edges(_Views(), {"views": []}) == []


def test_each_restore_stages_under_its_own_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two restores into one database never share a staging table, and each
    drops only its own; the printed run id removes a crashed run's leftovers."""
    b = _backup(monkeypatch, tmp_path)
    first, second = RestoreClient(_empty()), RestoreClient(_empty())
    assert _restore(monkeypatch, first, ["--in-dir", str(b)]) == 0
    assert _restore(monkeypatch, second, ["--in-dir", str(b)]) == 0
    staged = [
        set(re.findall(r"_restore_[0-9a-f]{8}_\w+", " ".join(cl.commands)))
        for cl in (first, second)
    ]
    assert staged[0] and not staged[0] & staged[1]
    ids = re.findall(r"Run ([0-9a-f]{8}):", capsys.readouterr().out)
    for run_id, tables in zip(ids, staged, strict=True):
        assert all(t.startswith(f"_restore_{run_id}_") for t in tables)

    leftovers = RestoreClient(_empty())
    leftovers.query = lambda sql, parameters=None: _Result(  # type: ignore[method-assign]
        [("_restore_0a1b2c3d_memory",)]
        if parameters == {"db": "drill", "prefix": "_restore_0a1b2c3d_"}
        else []
    )
    assert _restore(monkeypatch, leftovers, ["--drop-staging", "0a1b2c3d"]) == 0
    assert leftovers.commands == ["DROP TABLE IF EXISTS `drill`.`_restore_0a1b2c3d_memory` SYNC"]
    assert _restore(monkeypatch, leftovers, ["--drop-staging", "memory"]) == 2


class _RacedClient(RestoreClient):
    """Another run attaches `audit_log` rows in scope between this run's first
    look at the target and its lock: its locked look sees them."""

    def command(self, sql: str, settings: Any = None) -> None:
        super().command(sql, settings)
        if sql.startswith(f"CREATE TABLE {LOCK} "):
            self.databases["drill"]["audit_log"] = 5


def test_a_restore_judges_the_target_again_under_its_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two restores of one scope both saw an empty target: the second must
    refuse, not attach rows a ReplacingMergeTree folds into the first's."""
    b = _backup(monkeypatch, tmp_path)
    client = _RacedClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 2
    assert client.inserted == {}
    assert "audit_log" in capsys.readouterr().err
    assert client.commands[-1] == f"DROP TABLE IF EXISTS {LOCK} SYNC"


class _HeldLock(RestoreClient):
    def __init__(self, *args: Any, busy: int, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.busy = busy

    def command(self, sql: str, settings: Any = None) -> None:
        if sql.startswith(f"CREATE TABLE {LOCK} ") and self.busy:
            self.busy -= 1
            raise RuntimeError(
                "Code: 57. DB::Exception: Table already exists. (TABLE_ALREADY_EXISTS)"
            )
        super().command(sql, settings)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "SELECT comment" in sql:
            return _Result([("0a1b2c3d",)] if self.busy else [])
        return super().query(sql, parameters)


def test_a_restore_waits_for_the_run_that_holds_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = _backup(monkeypatch, tmp_path)
    monkeypatch.setattr(restore_loom.time, "sleep", lambda _s: None)
    client = _HeldLock(_empty(), busy=3)
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 0
    assert client.busy == 0 and "`drill`.`memory`" in client.inserted

    monkeypatch.setattr(restore_loom, "_LOCK_WAIT_S", 0.0)
    stuck = _HeldLock(_empty(), busy=10**6)
    assert _restore(monkeypatch, stuck, ["--in-dir", str(b)]) == 2
    assert stuck.inserted == {}
    assert "--drop-staging 0a1b2c3d" in capsys.readouterr().err


def test_a_coded_vector_table_is_created_with_the_codec_setting() -> None:
    """ClickHouse 26.8+ refuses a Quantized column unless the CREATE enables the
    codec; a staging `AS` copy carries the source's codecs, so it needs it too."""
    from types import SimpleNamespace

    coded = "CREATE TABLE t (v Array(Float32) CODEC(Quantized(8, 'b1'))) ENGINE = MergeTree"
    assert restore_loom.create_settings(coded) == {"enable_quantized_codec": 1}
    assert restore_loom.create_settings("CREATE TABLE t (x UInt8) ENGINE = MergeTree") == {}

    class _Client:
        def query(self, sql: str, parameters: Any = None) -> Any:
            return SimpleNamespace(result_rows=[("Replicated", coded)])

    ddl, settings = restore_loom.staging_ddl(_Client(), "db", "t", "`db`.`_restore_x_t`")
    assert ddl.endswith("AS `db`.`t`") and settings == {"enable_quantized_codec": 1}


# A replicated table restored somewhere new must not take the source's Keeper path.
_REPLICATED_DDL = (
    "CREATE TABLE loom.facts (`a` UInt8, `v` UInt64) ENGINE = "
    "ReplicatedReplacingMergeTree('/review/loom/facts', 'r1', v) ORDER BY a"
)


class _ReplicaCH:
    def __init__(
        self,
        *,
        db_engine: str | None = "Atomic",
        macros: dict[str, str] | None = None,
        replicas: int = 0,
    ) -> None:
        self.db_engine = db_engine
        self.macros = {"shard": "01", "replica": "r1"} if macros is None else macros
        self.replicas = replicas

    def query(self, sql: str, parameters: Any = None) -> Any:
        class _R:
            def __init__(self, rows: list[tuple[Any, ...]]) -> None:
                self.result_rows = rows

        if "system.macros" in sql:
            return _R(list(self.macros.items()))
        if "system.databases" in sql:
            return _R([(self.db_engine,)] if self.db_engine else [])
        if "system.server_settings" in sql:
            return _R(
                [
                    ("default_replica_path", "/clickhouse/tables/{uuid}/{shard}"),
                    ("default_replica_name", "{replica}"),
                ]
            )
        if "system.settings" in sql:
            return _R([("Atomic",)])
        if "system.zookeeper" in sql:
            return _R([(self.replicas,)])
        raise AssertionError(sql)


def _replica_plan() -> tuple[list[Any], Any]:
    plan = restore_loom.TablePlan(
        name="facts", path=Path("facts.native"), rows=1, ddl=_REPLICATED_DDL
    )
    state = restore_loom.TargetState(database_exists=True, missing=("facts",), rows_in_scope={})
    return [plan], state


def test_a_replicated_table_restored_into_another_database_gets_its_own_keeper_path() -> None:
    plans, state = _replica_plan()
    identity = restore_loom.replica_identity(_ReplicaCH(), "scratch", "loom", plans, state)
    ddl = restore_loom.target_ddl(_REPLICATED_DDL, "scratch", "facts", identity)
    assert "/review/loom/facts" not in ddl
    assert (
        "ReplicatedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', v)" in ddl
    )
    assert re.search(r"`scratch`\.`facts` UUID '[0-9a-f-]{36}' \(", ddl)


def test_a_replicated_database_names_the_restored_tables_path_itself() -> None:
    plans, state = _replica_plan()
    identity = restore_loom.replica_identity(
        _ReplicaCH(db_engine="Replicated"), "scratch", "loom", plans, state
    )
    ddl = restore_loom.target_ddl(_REPLICATED_DDL, "scratch", "facts", identity)
    assert "ENGINE = ReplicatedReplacingMergeTree(v)" in ddl


def test_the_backups_own_database_keeps_the_recorded_path_unless_replicas_remain() -> None:
    plans, state = _replica_plan()
    identity = restore_loom.replica_identity(_ReplicaCH(), "loom", "loom", plans, state)
    ddl = restore_loom.target_ddl(_REPLICATED_DDL, "loom", "facts", identity)
    assert "('/review/loom/facts', 'r1', v)" in ddl
    with pytest.raises(restore_loom.Refused, match="still has live replicas"):
        restore_loom.replica_identity(_ReplicaCH(replicas=2), "loom", "loom", plans, state)


def test_no_independent_path_without_the_default_macros_refuses() -> None:
    plans, state = _replica_plan()
    with pytest.raises(restore_loom.Refused, match="macros it does not define"):
        restore_loom.replica_identity(_ReplicaCH(macros={}), "scratch", "loom", plans, state)
