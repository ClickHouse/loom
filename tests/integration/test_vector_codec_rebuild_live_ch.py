"""scripts/ops/vector_codec_rebuild.py on a real ClickHouse, over a small table
whose sorting key ends in a FixedString: an erasure's tombstone during the
run, a row erased between the last twin-only read and the EXCHANGE, a delete
on the live table during the post-swap passes, a write after the swap with a
version below every copied row's, and a merge forced after the swap."""

from __future__ import annotations

import contextlib
import hashlib
import time
import uuid
from collections.abc import Iterator
from datetime import UTC
from typing import Any

import pytest

from scripts.ops import vector_codec_rebuild as vcr
from server.core.ch_names import NamedClient
from server.core.schema import table_rebuild as tr
from tests.integration._resources import ch_client_from_env

DIM = 8
KEYS = ("erased", "deleted-live", "tombstoned", "kept-1", "kept-2", "kept-3")


def sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def ch() -> Iterator[Any]:
    """The tool's own client shape: UTC session, `loom.` names the configured database."""
    client = ch_client_from_env(settings={"session_timezone": "UTC"}, tz_source="server")
    client.server_tz = UTC
    try:
        yield NamedClient(client)
    finally:
        client.close()


@pytest.fixture
def table(ch: Any) -> Iterator[str]:
    name = f"vcr_it_{uuid.uuid4().hex[:8]}"
    ch.command(
        f"CREATE TABLE loom.{name} (org String, namespace String, "
        "content_sha256 FixedString(64), embedding Array(Float32), created_at DateTime, "
        "tombstoned_at Nullable(DateTime)) "
        "ENGINE = ReplacingMergeTree(created_at) ORDER BY (org, namespace, content_sha256)"
    )
    try:
        yield name
    finally:
        for t in (name, name + tr.TWIN_SUFFIX, name + tr.PRE_SUFFIX):
            ch.command(f"DROP TABLE IF EXISTS loom.{t} SYNC")


def args(table: str) -> list[str]:
    return ["--table", table, "--columns", "embedding", "--cluster", "", "--settle-s", "0.5"]


def insert(ch: Any, table: str, name: str, created_at: str, settings: Any = None) -> None:
    vector = ", ".join(f"{(i + len(name)) % 7 + 1}.0" for i in range(DIM))
    ch.command(
        f"INSERT INTO loom.{table} (org, namespace, content_sha256, embedding, created_at) "
        f"VALUES ('o', 'n', '{sha(name)}', [{vector}], toDateTime('{created_at}'))",
        settings=settings,
    )


def wait_for_mutations(ch: Any, table: str) -> None:
    for _ in range(120):
        if not ch.query(vcr.mutations_running_sql(table)).result_rows[0][0]:
            return
        time.sleep(0.5)
    pytest.fail(f"mutations on loom.{table} did not finish")


def test_erasures_across_the_run_stay_erased_and_a_late_write_is_kept(
    ch: Any, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = tuple(int(x) for x in ch.query("SELECT version()").result_rows[0][0].split(".")[:2])
    if version < (26, 7):
        pytest.skip("the Quantized codec needs ClickHouse 26.7+")
    job = vcr.VectorRebuild(ch, vcr.parse(args(table)))
    if why := job.codec_refusal():
        pytest.skip(why)
    for i, name in enumerate(KEYS):
        insert(ch, table, name, f"2026-01-01 00:00:0{i}")

    consistent = vcr.VectorRebuild.consistent
    tombstoned: list[bool] = []

    def tombstone_during_the_run(self: Any, *a: Any) -> Any:
        if not tombstoned:  # an erasure's ALTER UPDATE, after the copy read the row
            ch.command(
                f"ALTER TABLE loom.{table} UPDATE tombstoned_at = now() "
                f"WHERE content_sha256 = '{sha('tombstoned')}'",
                settings={"mutations_sync": 2},
            )
            tombstoned.append(True)
        return consistent(self, *a)

    last_blocks_sql = vcr.last_blocks_sql

    def erase_before_the_exchange(name: str) -> str:
        if name == table + tr.TWIN_SUFFIX:  # after the last twin-only read
            ch.command(f"DELETE FROM loom.{table} WHERE content_sha256 = '{sha('erased')}'")
        return last_blocks_sql(name)

    swap_in = tr.swap_in

    def write_delete_and_merge_after_the_swap(client: Any, name: str) -> None:
        swap_in(client, name)
        insert(ch, table, "late", "2000-01-01 00:00:00", job.codec_on())
        ch.command(
            f"DELETE FROM loom.{table} WHERE content_sha256 = '{sha('deleted-live')}'",
            settings={"lightweight_deletes_sync": 0},
        )
        # folds the copied parts into the late write's, unless merges are stopped
        with contextlib.suppress(Exception):  # a refused merge is what the run arranges
            ch.command(f"OPTIMIZE TABLE loom.{table} FINAL")

    monkeypatch.setattr(vcr.VectorRebuild, "consistent", tombstone_during_the_run)
    monkeypatch.setattr(vcr.VectorRebuild, "recall", lambda *_: [1.0])  # not under test
    monkeypatch.setattr(vcr, "last_blocks_sql", erase_before_the_exchange)
    monkeypatch.setattr(tr, "swap_in", write_delete_and_merge_after_the_swap)

    assert vcr.main([*args(table), "--yes"], ch=ch) == tr.OK
    wait_for_mutations(ch, table)

    rows = ch.query(
        f"SELECT toString(content_sha256), tombstoned_at FROM loom.{table} FINAL"
    ).result_rows
    live = {key: tomb for key, tomb in rows}
    assert sha("erased") not in live
    assert sha("deleted-live") not in live
    assert sha("late") in live
    assert live[sha("tombstoned")] is not None
    assert all(sha(k) in live for k in ("kept-1", "kept-2", "kept-3"))
    codec = ch.query(
        "SELECT compression_codec FROM system.columns WHERE database = currentDatabase() "
        f"AND table = '{table}' AND name = 'embedding'"
    ).result_rows[0][0]
    assert "Quantized" in codec
