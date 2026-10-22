"""The codec rebuild's refusal and consistency gates on a real ClickHouse."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC
from typing import Any

import pytest

from scripts.ops import vector_codec_rebuild as vcr
from server.core.ch_names import NamedClient
from server.core.schema import table_rebuild as tr
from server.settings import settings
from tests.integration._resources import ch_client_from_env


@pytest.fixture
def ch() -> Iterator[Any]:
    client = ch_client_from_env(settings={"session_timezone": "UTC"}, tz_source="server")
    client.server_tz = UTC
    try:
        yield NamedClient(client)
    finally:
        client.close()


@pytest.fixture
def table(ch: Any, request: pytest.FixtureRequest) -> Iterator[str]:
    name = f"vcr_it_{uuid.uuid4().hex[:8]}"
    engine = getattr(request, "param", "ReplacingMergeTree(version)")
    ch.command(
        f"CREATE TABLE loom.{name} (org String, namespace String, id UInt64, "
        "embedding Array(Float32), version UInt64, note String DEFAULT '') "
        f"ENGINE = {engine} ORDER BY (org, namespace, id)"
    )
    try:
        yield name
    finally:
        for suffix in ("", tr.TWIN_SUFFIX, tr.PRE_SUFFIX):
            ch.command(f"DROP TABLE IF EXISTS loom.{name}{suffix} SYNC")


@pytest.fixture
def replicated_ch(ch: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A client on a Replicated database the run takes as its schema: there a table's Keeper
    path holds its own UUID, so the twin created from SHOW CREATE gets a path of its own."""
    name = f"vcr_it_rdb_{uuid.uuid4().hex[:8]}"
    ch.command(
        f"CREATE DATABASE {name} ENGINE = Replicated('/clickhouse/databases/{name}', 's1', 'r1')"
    )
    client = ch_client_from_env(
        database=name, settings={"session_timezone": "UTC"}, tz_source="server"
    )
    client.server_tz = UTC
    monkeypatch.setattr(settings, "ch_database", name)
    try:
        yield NamedClient(client)
    finally:
        client.close()
        ch.command(f"DROP DATABASE IF EXISTS {name} SYNC")


def args(table: str) -> list[str]:
    return ["--table", table, "--columns", "embedding", "--cluster", "", "--settle-s", "0"]


def insert(ch: Any, table: str, ident: int, vector: list[float], version: int = 1) -> None:
    ch.command(
        f"INSERT INTO loom.{table} (org, namespace, id, embedding, version) "
        f"VALUES ('o', 'n', {ident}, {vector}, {version})"
    )


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_mixed_widths_refuse_before_creating_a_twin(ch: Any, table: str, mode: str) -> None:
    insert(ch, table, 1, [1.0] * 8)
    insert(ch, table, 2, [1.0] * 8)
    insert(ch, table, 3, [1.0] * 4)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    assert vcr.main([*args(table), mode], ch=ch) == tr.REFUSED
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.TWIN_SUFFIX}").result_rows == [(0,)]
    assert ch.query(f"SELECT id, length(embedding) FROM loom.{table} ORDER BY id").result_rows == [
        (1, 8),
        (2, 8),
        (3, 4),
    ]


@pytest.mark.parametrize("old_placeholders", [False, True])
def test_width_checks_use_the_current_replacing_versions(
    ch: Any, table: str, old_placeholders: bool
) -> None:
    insert(ch, table, 1, [1.0] * 4)
    insert(ch, table, 1, [1.0] * 8, version=2)
    if old_placeholders:
        for ident in range(2, 5):
            insert(ch, table, ident, [0.0] * 4)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    assert job.find_dim() == 8
    assert vcr.main([*args(table), "--dry-run"], ch=ch) == tr.OK


def test_a_row_erased_during_the_gates_stops_the_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(ch, table, 1, [1.0] * 8)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)

    def erase_during_recall(*_: Any) -> list[float]:
        ch.command(
            f"DELETE FROM loom.{table} WHERE id = 1", settings={"lightweight_deletes_sync": 2}
        )
        return [1.0]

    monkeypatch.setattr(vcr.VectorRebuild, "recall", erase_during_recall)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.GATE_FAILED
    assert ch.query(f"SELECT count() FROM loom.{table} FINAL").result_rows == [(0,)]
    assert ch.query(f"SELECT count() FROM loom.{table}{tr.TWIN_SUFFIX} FINAL").result_rows == [(1,)]
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]


def test_recall_filter_keeps_nonzero_vectors_with_a_zero_first_coordinate(ch: Any) -> None:
    rows = ch.query(
        "SELECT embedding FROM (SELECT arrayJoin([[0., 1.], [1., 0.], [0., 0.], [1.]]) "
        f"AS embedding) WHERE {vcr.vector_filter('embedding', dim=2)} ORDER BY embedding"
    ).result_rows
    assert [list(row[0]) for row in rows] == [[0.0, 1.0], [1.0, 0.0]]


@pytest.mark.parametrize("method", ["rabitq", "int8"])
def test_a_quiet_rebuild_passes_its_real_recall_gate(ch: Any, table: str, method: str) -> None:
    vectors = {ident: [float(i == ident) for i in range(8)] for ident in range(1, 4)}
    vectors[4] = [0.0] * 8
    for ident, vector in vectors.items():
        insert(ch, table, ident, vector)
    flags = [*args(table), "--method", method]
    job = vcr.VectorRebuild(ch, vcr.parse(flags))
    if why := job.codec_refusal():
        pytest.skip(why)
    assert vcr.main([*flags, "--yes"], ch=ch) == tr.OK
    rows = ch.query(
        f"SELECT org, namespace, id, embedding, version FROM loom.{table} FINAL ORDER BY id"
    ).result_rows
    assert rows == [("o", "n", ident, vector, 1) for ident, vector in vectors.items()]
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(1,)]
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.TWIN_SUFFIX}").result_rows == [(0,)]


@pytest.mark.parametrize("during_run", [False, True])
def test_unfinished_mutations_stop_copy_or_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch, during_run: bool
) -> None:
    insert(ch, table, 1, [1.0] * 8)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    ch.command(f"SYSTEM STOP MERGES loom.{table}")

    def start_mutation(*_: Any) -> list[float]:
        ch.command(
            f"ALTER TABLE loom.{table} UPDATE embedding = [2., 2., 2., 2., 2., 2., 2., 2.] "
            "WHERE id = 1",
            settings={"mutations_sync": 0},
        )
        return [1.0]

    try:
        if during_run:
            monkeypatch.setattr(vcr.VectorRebuild, "recall", start_mutation)
        else:
            start_mutation()
        expected = tr.GATE_FAILED if during_run else tr.REFUSED
        assert vcr.main([*args(table), "--yes"], ch=ch) == expected
        assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]
        assert ch.query(f"EXISTS TABLE loom.{table}{tr.TWIN_SUFFIX}").result_rows == [
            (int(during_run),)
        ]
    finally:
        ch.command(f"SYSTEM START MERGES loom.{table}")


@pytest.mark.parametrize("table", ["MergeTree"], indirect=True)
def test_removing_one_unversioned_row_under_a_shared_key_stops_the_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(ch, table, 1, [1.0] * 8)
    insert(ch, table, 1, [2.0] * 8, version=2)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)

    def erase_during_recall(*_: Any) -> list[float]:
        ch.command(
            f"DELETE FROM loom.{table} WHERE id = 1 AND version = 2",
            settings={"lightweight_deletes_sync": 2},
        )
        return [1.0]

    monkeypatch.setattr(vcr.VectorRebuild, "recall", erase_during_recall)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.GATE_FAILED
    assert ch.query(f"SELECT count() FROM loom.{table}").result_rows == [(1,)]
    assert ch.query(f"SELECT count() FROM loom.{table}{tr.TWIN_SUFFIX}").result_rows == [(2,)]
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]


def test_a_lightweight_update_of_the_vector_during_recall_stops_the_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The UPDATE keeps the key and its version, and the vector in the row hash shows the
    change. A merge applies the patch but keeps its patch part active, so a change with no
    patch part is the finished ALTER UPDATE below."""
    ch.command(
        f"ALTER TABLE loom.{table} MODIFY SETTING "
        "enable_block_number_column = 1, enable_block_offset_column = 1"
    )
    insert(ch, table, 1, [1.0] * 8)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    ch.command(f"SYSTEM STOP MERGES loom.{table}")

    def update_during_recall(*_: Any) -> list[float]:
        ch.command(
            f"UPDATE loom.{table} SET embedding = [2., 2., 2., 2., 2., 2., 2., 2.] WHERE id = 1"
        )
        return [1.0]

    try:
        monkeypatch.setattr(vcr.VectorRebuild, "recall", update_during_recall)
        assert vcr.main([*args(table), "--yes"], ch=ch) == tr.GATE_FAILED
        assert "the source changed during the gates" in capsys.readouterr().out
        assert ch.query(f"SELECT embedding FROM loom.{table} FINAL").result_rows == [([2.0] * 8,)]
        assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]
    finally:
        ch.command(f"SYSTEM START MERGES loom.{table}")


def _with_patch_parts(ch: Any, table: str) -> None:
    """Two rows, each with a patch part from a lightweight UPDATE made before the run."""
    ch.command(
        f"ALTER TABLE loom.{table} MODIFY SETTING "
        "enable_block_number_column = 1, enable_block_offset_column = 1"
    )
    insert(ch, table, 1, [1.0] * 8)
    insert(ch, table, 2, [1.0] * 8)
    for ident in (1, 2):
        ch.command(f"UPDATE loom.{table} SET note = 'quarantined' WHERE id = {ident}")


def test_a_merge_after_the_last_check_keeps_the_postcondition(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge between the last check and the swap moves parts and patch parts but no row,
    so a correct rebuild still passes."""
    _with_patch_parts(ch, table)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    swap_in = tr.swap_in

    def merge_then_swap(client: Any, name: str) -> None:
        ch.command(f"OPTIMIZE TABLE loom.{name} FINAL")
        swap_in(client, name)

    monkeypatch.setattr(tr, "swap_in", merge_then_swap)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.OK
    assert ch.query(f"SELECT count() FROM loom.{table} FINAL").result_rows == [(2,)]


def test_a_lightweight_update_after_the_last_check_fails_the_postcondition(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An UPDATE between the last check and the swap keeps the row count and the version;
    the rollback copy holds it and the new table does not. A same-version difference
    could as well be a change in the new table, which --rollback would drop, so the run
    keeps both tables for inspection instead of naming --rollback."""
    _with_patch_parts(ch, table)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    swap_in = tr.swap_in

    def update_then_swap(client: Any, name: str) -> None:
        ch.command(f"UPDATE loom.{name} SET note = 'late' WHERE id = 1")
        swap_in(client, name)

    monkeypatch.setattr(tr, "swap_in", update_then_swap)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.POSTCONDITION
    out = capsys.readouterr().out
    assert "1 key(s) differ at one version; either table may hold the change" in out
    assert "holds a change" not in out and "inspect the difference" in out
    assert "--rollback --yes" not in out.rsplit("summary ", 1)[1]
    pre = f"loom.{table}{tr.PRE_SUFFIX}"
    assert ch.query(f"SELECT note FROM {pre} FINAL WHERE id = 1").result_rows == [("late",)]


def test_a_finished_alter_update_during_recall_stops_the_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An erasure tombstone is an ALTER UPDATE that keeps the key and its version; one that
    finishes before the swap leaves no unfinished mutation, and the row hash shows it."""
    insert(ch, table, 1, [1.0] * 8)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)

    def tombstone_during_recall(*_: Any) -> list[float]:
        ch.command(
            f"ALTER TABLE loom.{table} UPDATE embedding = [2., 2., 2., 2., 2., 2., 2., 2.] "
            "WHERE id = 1",
            settings={"mutations_sync": 2},
        )
        return [1.0]

    monkeypatch.setattr(vcr.VectorRebuild, "recall", tombstone_during_recall)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.GATE_FAILED
    assert "the source changed during the gates" in capsys.readouterr().out
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]
    assert ch.query(f"SELECT embedding FROM loom.{table} FINAL").result_rows == [([2.0] * 8,)]


def test_an_update_of_another_column_during_recall_stops_the_swap(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A quarantine changes a row's other columns and keeps its key and version: the row's
    content hash shows it, whether or not its patch part is still there."""
    ch.command(
        f"ALTER TABLE loom.{table} MODIFY SETTING "
        "enable_block_number_column = 1, enable_block_offset_column = 1"
    )
    insert(ch, table, 1, [1.0] * 8)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)

    def quarantine_during_recall(*_: Any) -> list[float]:
        ch.command(f"UPDATE loom.{table} SET note = 'quarantined' WHERE id = 1")
        return [1.0]

    monkeypatch.setattr(vcr.VectorRebuild, "recall", quarantine_during_recall)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.GATE_FAILED
    assert "the source changed during the gates" in capsys.readouterr().out
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]


def test_rows_sharing_a_key_and_its_version_are_counted_until_optimized(
    ch: Any, table: str
) -> None:
    """FINAL keeps the last inserted of such rows, and the copy may insert them in another
    order; the gate counts them so its message can name OPTIMIZE ... FINAL."""
    ch.command(f"SYSTEM STOP MERGES loom.{table}")
    try:
        for note in ("first", "second"):
            ch.command(
                f"INSERT INTO loom.{table} (org, namespace, id, embedding, version, note) "
                f"VALUES ('o', 'n', 1, {[1.0] * 8}, 1, '{note}')"
            )
        insert(ch, table, 2, [1.0] * 8)
        key = ["org", "namespace", "id"]
        assert ch.query(vcr.tied_keys_sql(table, "version", key)).result_rows == [(1,)]
    finally:
        ch.command(f"SYSTEM START MERGES loom.{table}")
    ch.command(f"OPTIMIZE TABLE loom.{table} FINAL")
    assert ch.query(vcr.tied_keys_sql(table, "version", key)).result_rows == [(0,)]


def test_a_quiet_rebuild_of_a_replicated_table_checks_each_read_s_replica(
    replicated_ch: Any,
) -> None:
    """On ReplicatedMergeTree every read after the drain also reads whether its replica has
    fetched every part, and the checks after the swap sync the rollback copy LIGHTWEIGHT;
    on a quiet table each of them passes."""
    ch = replicated_ch
    table = f"vcr_it_{uuid.uuid4().hex[:8]}"
    ch.command(
        f"CREATE TABLE loom.{table} (org String, namespace String, id UInt64, "
        "embedding Array(Float32), version UInt64, note String DEFAULT '') "
        "ENGINE = ReplicatedReplacingMergeTree(version) ORDER BY (org, namespace, id)"
    )
    vectors = {ident: [float(i == ident) for i in range(8)] for ident in range(1, 4)}
    for ident, vector in vectors.items():
        insert(ch, table, ident, vector)
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.OK
    assert ch.query(f"SELECT count() FROM loom.{table} FINAL").result_rows == [(3,)]
    assert vcr.main([*args(table), "--drop-pre-rebuild", "--yes"], ch=ch) == tr.OK
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.PRE_SUFFIX}").result_rows == [(0,)]


def test_an_empty_replicated_table_refuses_as_empty(
    replicated_ch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ReplicatedMergeTree never written has an empty replication log: the run names
    the empty table rather than a replica that has not caught up."""
    ch = replicated_ch
    table = f"vcr_it_{uuid.uuid4().hex[:8]}"
    ch.command(
        f"CREATE TABLE loom.{table} (org String, namespace String, id UInt64, "
        "embedding Array(Float32), version UInt64, note String DEFAULT '') "
        "ENGINE = ReplicatedReplacingMergeTree(version) ORDER BY (org, namespace, id)"
    )
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.REFUSED
    out = capsys.readouterr().out
    assert f"loom.{table} holds no rows: nothing to copy" in out
    assert "has not fetched every part" not in out
    assert ch.query(f"EXISTS TABLE loom.{table}{tr.TWIN_SUFFIX}").result_rows == [(0,)]


def test_a_read_on_a_replica_that_has_not_fetched_a_part_refuses_the_run(
    ch: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SYSTEM SYNC REPLICA syncs only the replica that answers it. Here the drain is left
    out, as when its sync reached another replica, and the replica the run reads has its
    fetches stopped while the other replica writes a part: the width read's own replica
    check refuses the run."""
    table = f"vcr_it_{uuid.uuid4().hex[:8]}"
    other = f"{table}_r2"
    for name, replica in ((table, "r1"), (other, "r2")):
        ch.command(
            f"CREATE TABLE loom.{name} (org String, namespace String, id UInt64, "
            "embedding Array(Float32), version UInt64, note String DEFAULT '') "
            f"ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{table}', "
            f"'{replica}', version) ORDER BY (org, namespace, id)"
        )
    try:
        insert(ch, table, 1, [1.0] * 8)
        job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
        if why := job.codec_refusal():
            pytest.skip(why)
        ch.command(f"SYSTEM STOP FETCHES loom.{table}")
        insert(ch, other, 2, [1.0] * 8)
        monkeypatch.setattr(vcr.VectorRebuild, "drain", lambda self, engine: None)
        assert vcr.main([*args(table), "--yes"], ch=ch) == tr.REFUSED
        assert "has not fetched every part" in capsys.readouterr().out
        assert ch.query(f"EXISTS TABLE loom.{table}{tr.TWIN_SUFFIX}").result_rows == [(0,)]
    finally:
        ch.command(f"SYSTEM START FETCHES loom.{table}")
        for name in (table, other):
            ch.command(f"DROP TABLE IF EXISTS loom.{name} SYNC")
