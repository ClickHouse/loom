"""dev/backup_loom.py and dev/restore_loom.py against a scripted client.

A backup records every table's file, row count, byte size and SHA-256 in a
manifest written last; a failed table leaves the manifest incomplete and the
exit code 1; an org backup never carries the org-less idempotency cache.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import backup_loom, restore_loom  # noqa: E402
from dev._ch_target import CHTarget  # noqa: E402

TARGET = CHTarget(host="ch.test", port=8123, user="loom", database="loom", secure=False)


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

    def raw_stream(self, sql: str, parameters: dict[str, Any] | None = None) -> io.BytesIO:
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
    assert [e["name"] for e in manifest["tables"]] == list(backup_loom.TABLES)
    for entry in manifest["tables"]:
        data = (out / entry["file"]).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["rows"] == data.count(b"\n")
        assert entry["create_table_query"].startswith(f"CREATE TABLE loom.{entry['name']} ")
    assert client.closed
    # Unqualified table names: the client's database is the one read.
    assert all(" FROM loom." not in sql for sql, _ in client.queries)


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


def test_tables_subset_keeps_the_canonical_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    argv = ["--out-dir", str(out), "--tables", "audit_log,memory"]
    assert _run(monkeypatch, FakeClient(_rows()), argv) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert [e["name"] for e in manifest["tables"]] == ["memory", "audit_log"]


def test_drill_table_list_matches_the_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts/ops/backup_drill.sh checks one file per table it lists."""
    drill = (ROOT / "scripts" / "ops" / "backup_drill.sh").read_text()
    listed = drill.split("TABLES=(", 1)[1].split(")", 1)[0].split()
    assert tuple(listed) == backup_loom.TABLES


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

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "system.databases" in sql:
            return _Result([(int(parameters["db"] in self.databases),)])
        if "system.tables" in sql:
            return _Result([(t,) for t in self.databases.get(parameters["db"], {})])
        db, table = sql.split(" FROM ", 1)[1].split()[0].replace("`", "").split(".")
        return _Result([(self.databases[db][table],)])

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def raw_insert(
        self, *, table: str, insert_block: Any, fmt: str, settings: dict[str, Any]
    ) -> None:
        assert fmt == "JSONEachRow" and settings == {"max_partitions_per_insert_block": 0}
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


def _empty(db: str = "drill") -> dict[str, dict[str, int]]:
    return {db: {t: 0 for t in backup_loom.TABLES}}


def test_restore_inserts_into_the_named_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient(_empty())
    assert _restore(monkeypatch, client, ["--in-dir", str(b), "--skip-idempotency"]) == 0
    assert set(client.inserted) == {"`drill`.`memory`", "`drill`.`audit_log`"}
    assert client.inserted["`drill`.`memory`"] == (b / "memory.jsonl").read_bytes()
    assert client.commands == []  # database and tables existed
    assert client.closed


def test_restore_creates_the_database_and_tables_from_the_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _backup(monkeypatch, tmp_path)
    client = RestoreClient({})
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 0
    assert client.commands[0] == "CREATE DATABASE IF NOT EXISTS `drill`"
    creates = client.commands[1:]
    assert len(creates) == len(backup_loom.TABLES)
    assert creates[0].startswith("CREATE TABLE IF NOT EXISTS `drill`.`namespaces` (")
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
    client = RestoreClient(_empty(), fail_on="audit_log")
    assert _restore(monkeypatch, client, ["--in-dir", str(b)]) == 1
    assert set(client.inserted) == {"`drill`.`memory`"}  # idempotency comes after audit_log
    err = capsys.readouterr().err
    assert "partial restore" in err and "memory" in err


def test_retarget_ddl_rewrites_only_the_head() -> None:
    ddl = (
        "CREATE TABLE loom.memory (`v` String DEFAULT 'loom.memory') ENGINE = MergeTree ORDER BY v"
    )
    out = restore_loom.retarget_ddl(ddl, "drill", "memory")
    assert out.startswith("CREATE TABLE IF NOT EXISTS `drill`.`memory` (")
    assert "DEFAULT 'loom.memory'" in out
    with pytest.raises(restore_loom.Refused):
        restore_loom.retarget_ddl("CREATE VIEW loom.v AS SELECT 1", "drill", "v")


def test_a_row_with_non_utf8_bytes_is_a_complete_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class RawClient(FakeClient):
        def raw_stream(self, sql: str, parameters: dict[str, Any] | None = None) -> io.BytesIO:
            return io.BytesIO(b'{"org":"a","row_hash":"\xcd\xf3j"}\n')

    out = tmp_path / "b"
    assert _run(monkeypatch, RawClient({}), ["--out-dir", str(out), "--tables", "audit_log"]) == 0
