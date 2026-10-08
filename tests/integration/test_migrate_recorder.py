"""Watermark recorder in server/core/schema/migrate.py.

clickhouse-connect's dict-row insert path strict-matches column count, so
the recorder passes `column_names=[...]` explicitly and the server's DEFAULT
fills the missing column (the ledger table has one more column than the
recorder supplies).

This test pins that the recorder lands a row + the applied-set query
sees it, so a strict column-match insert cannot silently drop a watermark.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.core.schema import migrate as _loom_migrate  # noqa: E402


@pytest.fixture
def sync_ch_client(ch_client):
    """The migrate.py recorder uses a synchronous clickhouse-connect client
    (it's a one-shot CLI, no asyncio loop). The conftest `ch_client`
    fixture is already a sync client — re-use it."""
    return ch_client


def test_record_inserts_a_watermark_row(sync_ch_client) -> None:
    """The recorder + the `applied_set` query should round-trip the
    watermark for a freshly-added migration name."""
    fake_name = f"999_test_recorder_{uuid.uuid4().hex[:8]}.sql"
    fake_checksum = "0" * 64

    try:
        _loom_migrate._record(sync_ch_client, fake_name, fake_checksum)
        applied = _loom_migrate._applied(sync_ch_client)
        assert fake_name in applied
        assert applied[fake_name] == fake_checksum
    finally:
        # Cleanup so subsequent runs see a clean state.
        sync_ch_client.command(
            "ALTER TABLE loom._migrations DELETE WHERE filename = "
            "{f:String} SETTINGS mutations_sync=2",
            parameters={"f": fake_name},
        )


def test_record_does_not_drop_other_columns(sync_ch_client) -> None:
    """The server-side DEFAULT for applied_at must populate even though
    the recorder doesn't supply it. Without column_names the insert
    fails with 'column count mismatch' before this assertion runs."""
    fake_name = f"999_test_recorder_defaults_{uuid.uuid4().hex[:8]}.sql"
    fake_checksum = "f" * 64

    try:
        _loom_migrate._record(sync_ch_client, fake_name, fake_checksum)
        row = sync_ch_client.query(
            "SELECT filename, checksum, applied_at, applied_by "
            "FROM loom._migrations FINAL WHERE filename = {f:String}",
            parameters={"f": fake_name},
        ).result_rows
        assert row, "row not found after insert"
        filename, checksum, applied_at, applied_by = row[0]
        assert filename == fake_name
        assert checksum.decode("ascii") == fake_checksum
        # applied_at populated by server-side DEFAULT now()
        assert applied_at is not None
        # applied_by populated by the recorder from hostname
        assert applied_by != ""
    finally:
        sync_ch_client.command(
            "ALTER TABLE loom._migrations DELETE WHERE filename = "
            "{f:String} SETTINGS mutations_sync=2",
            parameters={"f": fake_name},
        )
