"""A stale timestamp never fences a paused coordinator."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from server.insights.store import provider_lock


@pytest.mark.parametrize("stopped", [False, True])
def test_kernel_proof_is_required_even_when_keeper_ttl_expired(monkeypatch, stopped):
    ch = Mock()
    ch.query.return_value = SimpleNamespace(result_rows=[(0,)])
    lock = provider_lock.ProcessKeeperLock(ch, lock_name="provider_configuration")
    lock._try_insert = Mock(return_value=False)
    old = f"{uuid4()}/{uuid4()}"
    row = (old, datetime.now(UTC), 1, True)
    lock._holder_row = Mock(return_value=row)

    @contextmanager
    def proof(identity):
        assert identity == old.split("/")[0]
        yield stopped

    monkeypatch.setattr(provider_lock, "stopped_process", proof)
    assert lock._attempt() == (False, row)
    if stopped:
        assert ch.query.call_count == 3
        sql = ch.command.call_args.args[0]
        assert "DELETE" in sql and "holder = {old:String}" in sql
        assert ch.command.call_args.kwargs["parameters"] == {
            "name": "provider_configuration",
            "old": old,
        }
    else:
        ch.query.assert_not_called()
        ch.command.assert_not_called()


@pytest.mark.parametrize("result", [[], [(None,)], [(1,)], [(False,)]])
def test_missing_or_busy_storage_status_never_allows_recovery(result):
    ch = Mock()
    ch.query.return_value = SimpleNamespace(result_rows=result)
    lock = provider_lock.ProcessKeeperLock(ch)
    assert not lock._storage_idle()


def test_generic_keeper_looks_up_requested_key_not_migration_key():
    ch = Mock()
    ch.query.return_value = SimpleNamespace(result_rows=[])
    lock = provider_lock.KeeperLock(ch, lock_name="provider_configuration")
    assert lock._holder_row() is None
    assert ch.query.call_args.kwargs["parameters"] == {"name": "provider_configuration"}
