"""Backup and restore of document originals and of the tables materialized views
fill, against a running server and its ClickHouse.

- A binary original round-trips byte for byte through `backup_loom` and a full
  `restore_loom` into a scratch database, and every table holds the manifest's
  row count (the restore keeps duplicates of a ReplacingMergeTree key).
- Into a database whose views exist, a restore of `memory` with
  `memory_versions` does not double the history (an attach fires no view), and
  the views keep running for later writes.
- A selection that splits a view is refused in both directions, and a backup
  `--exclude` takes the tables a view fills from the excluded one with it.
- The sweep racing an upload of the bytes of an old, deleted document's
  original never costs the upload its original.
- The originals sweep finds an old original no live document names, and no
  other.

Every restore writes to a scratch database dropped at the end, never `loom`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

pytestmark = [pytest.mark.usefixtures("ch_target_required"), pytest.mark.recovery]

BACKUP = ROOT / "dev" / "backup_loom.py"
RESTORE = ROOT / "dev" / "restore_loom.py"
# Bytes that are not UTF-8, plus every byte JSON escapes.
RAW = bytes(range(256)) * 8 + b'\x00\xff"\\\n\xc3('


def _run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def scratch_db(ch_client):
    name = f"loom_bkt_{uuid.uuid4().hex[:8]}"
    yield name
    ch_client.command(f"DROP DATABASE IF EXISTS `{name}` SYNC")


async def _add(c: httpx.AsyncClient, org: str, ns: str, uri: str, raw: bytes) -> None:
    r = await c.post(
        "/v1/knowledge.add",
        json={
            "org": org,
            "namespace": ns,
            "source_uri": uri,
            "content": f"text of {uri}",
            "title": uri,
            "original_bytes_b64": base64.b64encode(raw).decode(),
            "original_filename": f"{uri}.bin",
            "blob_content_type": "application/octet-stream",
        },
    )
    assert r.status_code == 200, r.text


def _count(ch_client, db: str, table: str, org: str) -> int:
    return int(
        ch_client.query(
            f"SELECT count() FROM `{db}`.`{table}` WHERE org = {{org:String}}",
            parameters={"org": org},
        ).result_rows[0][0]
    )


@pytest.mark.asyncio
async def test_a_binary_original_round_trips_through_a_full_restore(
    tmp_path: Path, control_plane_url: str, ch_client, scratch_db: str
) -> None:
    org, ns = f"bkt-{uuid.uuid4().hex[:6]}", "blobs"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _add(c, org, ns, "doc-a", RAW)
        await _add(c, org, ns, "doc-b", RAW[::-1])
        r = await c.post(
            "/v1/memory.set",
            json={"org": org, "namespace": ns, "agent": "w", "memory_key": "k", "value": {"v": 1}},
        )
        assert r.status_code == 200, r.text

    # Two versions of one original in one part, as a re-upload leaves them until
    # a merge: a restore keeps both (optimize_on_insert = 0), so the counts below
    # match. A merge in the source may collapse them before the dump; the
    # restore's settings are pinned by the unit tests.
    sha = hashlib.sha256(RAW).hexdigest()
    ch_client.insert(
        "loom.document_blobs",
        [[org, ns, sha, RAW, len(RAW), datetime(2026, 1, d)] for d in (1, 2)],
        column_names=["org", "namespace", "sha256", "data", "size", "created_at"],
        settings={"optimize_on_insert": 0},
    )
    out = tmp_path / "bkp"
    r = _run(str(BACKUP), "--out-dir", str(out), "--org", org)
    assert r.returncode == 0, r.stderr
    r = _run(str(RESTORE), "--in-dir", str(out), "--database", scratch_db)
    assert r.returncode == 0, r.stdout + r.stderr

    manifest = json.loads((out / "manifest.json").read_text())
    restored = {e["name"]: e["rows"] for e in manifest["tables"] if e.get("rows")}
    assert restored["document_blobs"] >= 2
    for table, rows in restored.items():
        assert _count(ch_client, scratch_db, table, org) == rows, table
    got = ch_client.query(
        f"SELECT toString(sha256), hex(data) FROM `{scratch_db}`.document_blobs FINAL "
        "WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    want = {hashlib.sha256(b).hexdigest(): b.hex().upper() for b in (RAW, RAW[::-1])}
    assert dict(got) == want


def _views_db(ch_client, db: str) -> None:
    """A scratch copy of `memory` and the two tables its views fill, with the
    views: the shape `loom migrate` gives a live database."""
    ch_client.command(f"CREATE DATABASE `{db}`")
    for t in ("memory", "memory_versions", "memory_code_link_queue"):
        ch_client.command(f"CREATE TABLE `{db}`.`{t}` AS loom.`{t}`")
    for v in ("memory_versions_mv", "memory_code_link_queue_mv"):
        ddl = ch_client.query(
            "SELECT create_table_query FROM system.tables "
            "WHERE database = 'loom' AND name = {v:String}",
            parameters={"v": v},
        ).result_rows[0][0]
        ch_client.command(str(ddl).replace("loom.", f"`{db}`."))


@pytest.mark.asyncio
async def test_a_restore_into_a_database_with_views_does_not_double_the_history(
    tmp_path: Path, control_plane_url: str, ch_client, scratch_db: str
) -> None:
    org, ns = f"bkv-{uuid.uuid4().hex[:6]}", "views"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        for i in range(3):
            r = await c.post(
                "/v1/memory.set",
                json={
                    "org": org,
                    "namespace": ns,
                    "agent": "w",
                    "memory_key": f"k{i}",
                    "value": {"v": i},
                },
            )
            assert r.status_code == 200, r.text
    out = tmp_path / "bkp"
    tables = "memory,memory_versions,memory_code_link_queue"
    r = _run(str(BACKUP), "--out-dir", str(out), "--org", org, "--tables", tables)
    assert r.returncode == 0, r.stderr
    rows = {e["name"]: e["rows"] for e in json.loads((out / "manifest.json").read_text())["tables"]}
    assert rows["memory_versions"] >= 3

    _views_db(ch_client, scratch_db)
    r = _run(str(RESTORE), "--in-dir", str(out), "--database", scratch_db, "--tables", tables)
    assert r.returncode == 0, r.stdout + r.stderr
    for t in tables.split(","):
        assert _count(ch_client, scratch_db, t, org) == rows[t], t

    # The views still run for a write made after the restore.
    ch_client.command(
        f"INSERT INTO `{scratch_db}`.memory SELECT * FROM `{scratch_db}`.memory "
        "WHERE org = {org:String} LIMIT 1",
        parameters={"org": org},
    )
    assert _count(ch_client, scratch_db, "memory_versions", org) == rows["memory_versions"] + 1


def test_a_selection_that_splits_a_view_is_refused_both_ways(
    tmp_path: Path, scratch_db: str
) -> None:
    out = tmp_path / "bkp"
    org = f"bks-{uuid.uuid4().hex[:6]}"
    r = _run(str(BACKUP), "--out-dir", str(out), "--org", org)
    assert r.returncode == 0, r.stderr
    edges = json.loads((out / "manifest.json").read_text())["views"]
    assert {"view": "memory_versions_mv", "source": "memory", "target": "memory_versions"} in edges

    r = _run(str(RESTORE), "--in-dir", str(out), "--database", scratch_db, "--tables", "memory")
    assert r.returncode == 2 and "memory without memory_versions" in r.stderr
    r = _run(
        str(RESTORE), "--in-dir", str(out), "--database", scratch_db, "--tables", "code_term_freq"
    )
    assert r.returncode == 2 and "code_term_freq without code_chunks" in r.stderr

    excl = tmp_path / "excl"
    r = _run(str(BACKUP), "--out-dir", str(excl), "--org", org, "--exclude", "code_chunks")
    assert r.returncode == 0, r.stderr
    skipped = {
        e["name"]: e["skipped"]
        for e in json.loads((excl / "manifest.json").read_text())["tables"]
        if "skipped" in e
    }
    assert skipped["code_term_freq"] == "filled by a view from excluded code_chunks"
    assert skipped["code_doc_stats"] == "filled by a view from excluded code_chunks"
    r = _run(str(RESTORE), "--in-dir", str(excl), "--database", scratch_db, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr


def _backdate(ch_client, org: str, ns: str) -> None:
    """Every original of the namespace as written long ago, past the age guard."""
    rows = ch_client.query(
        "SELECT sha256, data, size FROM loom.document_blobs "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows
    ch_client.command(
        "DELETE FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    )
    ch_client.insert(
        "loom.document_blobs",
        [[org, ns, sha, data, size, datetime(2026, 1, 1)] for sha, data, size in rows],
        column_names=["org", "namespace", "sha256", "data", "size", "created_at"],
    )


@pytest.mark.asyncio
async def test_the_sweep_never_costs_a_concurrent_upload_its_bytes(
    control_plane_url: str, ch_client
) -> None:
    """A deleted document's original, backdated past the age guard, is a sweep
    candidate; an upload of the same bytes stores its original before its
    document row, so the sweep's read cannot see it. The upload's put restamps
    the row and the delete repeats the age check, so the new row survives."""
    from server.core.ch import get_client
    from server.knowledge.workers import blob_gc

    org = f"bkr-{uuid.uuid4().hex[:6]}"
    ch = get_client()
    await ch.connect()
    try:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
            for i in range(8):
                ns, raw = f"race-{i}", RAW + str(i).encode()  # one namespace a round
                await _add(c, org, ns, "old", raw)
                r = await c.post(
                    "/v1/knowledge.delete",
                    json={"org": org, "namespace": ns, "source_uri": "old"},
                )
                assert r.status_code == 200, r.text
                _backdate(ch_client, org, ns)
                swept, _ = await asyncio.gather(
                    blob_gc.sweep_namespace(org, ns, 3600), _add(c, org, ns, "new", raw)
                )
                assert swept in (0, 1)
                got = await c.get(
                    "/v1/knowledge.download",
                    params={"org": org, "namespace": ns, "source_uri": "new"},
                )
                assert got.status_code == 200, (i, got.text)
                assert got.content == raw and got.headers["X-Loom-Download-Source"] == "blob"
    finally:
        await ch.disconnect()


@pytest.mark.asyncio
async def test_the_sweep_finds_only_an_old_original_no_live_document_names(
    control_plane_url: str, ch_client
) -> None:
    from server.core.ch import get_client
    from server.knowledge.store import blobs

    org, ns = f"bkg-{uuid.uuid4().hex[:6]}", "gc"
    named = RAW + b"named"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _add(c, org, ns, "doc", named)
    long_ago = datetime(2026, 1, 1)
    shas = {k: hashlib.sha256(k.encode()).hexdigest() for k in ("old", "fresh")}
    ch_client.insert(
        "loom.document_blobs",
        [
            [org, ns, shas["old"], b"old", 3, long_ago],
            # Aware: the driver takes a naive datetime as the host's local time.
            [org, ns, shas["fresh"], b"fresh", 5, datetime.now(UTC)],
            # The named original, written long ago too: still not a candidate.
            [org, ns, hashlib.sha256(named).hexdigest(), named, len(named), long_ago],
        ],
        column_names=["org", "namespace", "sha256", "data", "size", "created_at"],
    )
    ch = get_client()
    await ch.connect()
    try:
        assert await blobs.unreferenced(org, ns, 3600, 100, ch=ch) == [shas["old"]]
        deleted = await blobs.delete(org, ns, [shas["old"], shas["fresh"]], min_age_s=3600, ch=ch)
        assert deleted.gone == 1
        left = ch_client.query(
            "SELECT toString(sha256) FROM loom.document_blobs WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows
        assert shas["old"] not in {r[0] for r in left} and shas["fresh"] in {r[0] for r in left}
    finally:
        await ch.disconnect()
