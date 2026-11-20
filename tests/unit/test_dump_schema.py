"""scripts/ops/dump_schema.py: what `make schema-dump` writes and what
`make schema-check` compares, without a ClickHouse."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.ops import dump_schema as ds


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class _Client:
    """Answers the three statements the dump issues."""

    def __init__(self, objects: dict[str, tuple[str, str]], version: str = "26.5.1.882") -> None:
        self.objects = objects
        self.version = version

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if sql == "SELECT version()":
            return _Result([(self.version,)])
        if sql.startswith("SELECT database, name, engine FROM system.tables"):
            assert parameters == {"dbs": ["loom", "loom_telemetry"]}
            dbs = parameters["dbs"]
            rows = sorted(
                (k.partition(".") + (e,) for k, (e, _) in self.objects.items()),
                key=lambda r: (dbs.index(r[0]), r[2]),
            )
            return _Result([(d, n, e) for d, _, n, e in rows])
        db, table = sql.removeprefix("SHOW CREATE TABLE `").rstrip("`").split("`.`", 1)
        return _Result([(self.objects[f"{db}.{table}"][1],)])


OBJECTS = {
    "loom.b": (
        "MergeTree",
        "CREATE TABLE loom.b\n(\n    `x` UInt8\n)\nENGINE = MergeTree\nORDER BY x",
    ),
    "loom.a": ("ReplacingMergeTree", "CREATE TABLE loom.a\n(\n    `k` String\n)\nENGINE = RMT"),
    "loom.a_mv": ("MaterializedView", "CREATE MATERIALIZED VIEW loom.a_mv TO loom.b AS SELECT 1"),
    "loom.a_v": ("View", "CREATE VIEW loom.a_v\n(\n    `x` UInt8\n)\nAS SELECT x FROM loom.b"),
    "loom_telemetry.t": (
        "MergeTree",
        "CREATE TABLE loom_telemetry.t\n(\n    `x` UInt8\n)\nENGINE = MergeTree\nORDER BY x",
    ),
}


def test_dump_is_sorted_idempotent_and_version_free() -> None:
    text = ds.dump(_Client(OBJECTS))
    assert text == ds.dump(_Client(OBJECTS, version="26.8.8.8"))
    assert "Counts: 3 tables + 1 views + 1 materialized views." in text
    assert (
        text.index("-- loom.a\n") < text.index("-- loom.b\n") < text.index("-- loom_telemetry.t\n")
    )
    assert text.index("-- loom_telemetry.t\n") < text.index("-- loom.a_v\n")
    assert text.index("-- loom.a_v\n") < text.index("-- loom.a_mv\n")
    assert "CREATE VIEW IF NOT EXISTS loom.a_v\n" in text
    assert "CREATE TABLE IF NOT EXISTS loom.a\n" in text
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.a_mv " in text
    assert ds.dump(_Client(OBJECTS)) == text


def test_a_hand_added_vector_index_is_not_dumped() -> None:
    ddl = (
        "CREATE TABLE loom.v\n(\n    `e` Array(BFloat16),\n"
        "    INDEX hnsw e TYPE vector_similarity('hnsw', 'cosineDistance', 3072)\n)\n"
        "ENGINE = MergeTree\nORDER BY tuple()"
    )
    out = ds.strip_runtime_indexes(ddl)
    assert "vector_similarity" not in out
    assert "`e` Array(BFloat16)\n)" in out


def test_a_table_without_ddl_fails_the_dump() -> None:
    client = _Client(OBJECTS)
    client.query = lambda sql, parameters=None: (  # type: ignore[method-assign]
        _Result([]) if sql.startswith("SHOW CREATE") else _Client.query(client, sql, parameters)
    )
    with pytest.raises(RuntimeError, match="no DDL for loom"):
        ds.dump(client)


def _run(monkeypatch: pytest.MonkeyPatch, out: Path, *args: str, text: str | None) -> int:
    def build() -> tuple[str, str]:
        if text is None:
            raise ConnectionError("refused")
        return text, "26.9.18.11"

    monkeypatch.setattr(ds, "build", build)
    return ds.main(["--out", str(out), *args])


def test_a_failed_dump_leaves_the_file_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "schema.sql"
    out.write_text("old\n")
    assert _run(monkeypatch, out, text=None) == 1
    assert out.read_text() == "old\n"


def test_check_never_writes_and_prints_the_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "schema.sql"
    out.write_text("old\n")
    assert _run(monkeypatch, out, "--check", text="new\n") == 1
    assert out.read_text() == "old\n"
    err = capsys.readouterr().err
    assert "-old" in err and "+new" in err and "dumped from ClickHouse 26.9.18.11" in err
    assert _run(monkeypatch, out, "--check", text="old\n") == 0


def test_an_unchanged_dump_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "schema.sql"
    out.write_text("same\n")
    before = out.stat().st_mtime_ns
    assert _run(monkeypatch, out, text="same\n") == 0
    assert out.stat().st_mtime_ns == before
    assert _run(monkeypatch, out, text="changed\n") == 0
    assert out.read_text() == "changed\n"
