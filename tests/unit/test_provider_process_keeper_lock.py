"""A dead holder of the provider lock is taken over once its participant stopped.

The fake below is the KeeperMap lock key plus the participant rows and
system.processes, with the server's clock under the test's control; the lock
reads it through the async client, `loom provider-lock clear` through the sync one.
"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from server.core import leases
from server.core.provider_process import participant_tag
from server.insights.store import provider_lock
from server.insights.store import provider_participants as pp
from tests.unit.claims_fake import FakeLockTable


class FakeCH(FakeLockTable):
    def __init__(self) -> None:
        super().__init__()
        self.participants: dict[str, dict[str, Any]] = {}
        self.tags: set[str] = set()
        self.cluster = False

    def participant(self, pid: str, *, state: str = "running", stale: int = 60) -> None:
        self.participants[pid] = {"state": state, "version": 1, "stale": stale, "beat": self.now()}

    def _participant_row(self, pid: str) -> dict[str, Any] | None:
        r = self.participants.get(pid)
        if r is None:
            return None
        expired = r["beat"] + r["stale"] < self.now()
        return {
            "state": r["state"],
            "version": r["version"],
            "stale_after_s": r["stale"],
            "expired": expired,
        }

    def _running(self, sql: str, tag: str) -> int:
        if "clusterAllReplicas" in sql and not self.cluster:
            raise RuntimeError("Code: 701. DB::Exception: Requested cluster 'default' not found")
        return int(tag in self.tags)

    async def query_all(self, sql: str, params: Any = None) -> Any:
        p = params or {}
        if sql == pp.ONE_SQL:
            row = self._participant_row(p["id"])
            return [] if row is None else [row]
        if sql in (pp.running_sql(), pp.RUNNING_LOCAL_SQL):
            return [{"running": self._running(sql, p["tag"])}]
        return await super().query_all(sql, params)

    # -- the sync client `loom provider-lock clear` uses

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        p = parameters or {}
        if sql == leases.LOCK_HOLDER:
            key = self.keys.get(p["name"])
            rows = [] if key is None else [(key["holder"], None, key["ttl"], self._stale(key))]
            return SimpleNamespace(result_rows=rows)
        if sql == pp.ONE_SQL:
            row = self._participant_row(p["id"])
            return SimpleNamespace(result_rows=[] if row is None else [tuple(row.values())])
        if sql in (pp.running_sql(), pp.RUNNING_LOCAL_SQL):
            return SimpleNamespace(result_rows=[(self._running(sql, p["tag"]),)])
        raise AssertionError(sql)

    def command(self, sql: str, parameters: Any = None) -> None:
        p = parameters or {}
        assert sql == provider_lock.LOCK_CLEAR
        key = self.keys.get(p["name"])
        if key and key["holder"] == p["old"]:
            del self.keys[p["name"]]

    def close(self) -> None:
        pass


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> FakeCH:
    fake = FakeCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
    return fake


@pytest.fixture
async def killed_holder(ch: FakeCH) -> str:
    """A holds the lock (mid-migration) and is SIGKILLed: no release, no beats."""
    a = str(uuid4())
    ch.participant(a)
    lease = await leases.take(provider_lock.LOCK_NAME, ttl_s=60, holder=f"{a}/{uuid4()}")
    assert lease is not None
    return lease.holder


async def _takes(ch: FakeCH) -> bool:
    try:
        async with provider_lock.configuration_lock(wait_s=0):
            return True
    except provider_lock.ConfigurationLockUnavailable:
        return False


async def test_another_process_takes_over_once_ttl_and_heartbeat_expired(ch, killed_holder):
    assert not await _takes(ch)
    ch.advance(provider_lock.LOCK_TTL_S + 1)
    assert await _takes(ch)
    assert ch.keys == {}  # released after the block


@pytest.mark.parametrize("case", ["beating", "query_running", "no_row"])
async def test_an_expired_lock_is_not_taken_from_a_holder_that_may_still_run(
    ch, killed_holder, case
):
    a = killed_holder.split("/")[0]
    ch.advance(120)
    if case == "beating":
        ch.participants[a]["beat"] = ch.now() - 5
    elif case == "query_running":
        ch.tags.add(participant_tag(a))
    else:
        del ch.participants[a]
    assert not await _takes(ch)
    assert ch.holder(provider_lock.LOCK_NAME) == killed_holder


async def test_a_retired_holder_is_taken_over_once_its_key_expired(ch, killed_holder):
    ch.participants[killed_holder.split("/")[0]]["state"] = "retired"
    ch.participants[killed_holder.split("/")[0]]["beat"] = ch.now()
    assert not await _takes(ch)
    ch.advance(provider_lock.LOCK_TTL_S + 1)
    assert await _takes(ch)


async def test_running_queries_are_counted_on_every_node_when_the_cluster_exists(ch, killed_holder):
    a = killed_holder.split("/")[0]
    ch.cluster = True
    ch.advance(120)
    ch.tags.add(participant_tag(a))
    assert pp.running_queries_sync(ch, a) == 1
    assert not await _takes(ch)


def test_clear_refuses_while_the_holder_is_live(ch):
    a = str(uuid4())
    ch.participant(a)
    holder = f"{a}/{uuid4()}"
    ch.seed(provider_lock.LOCK_NAME, holder, ttl=provider_lock.LOCK_TTL_S)
    assert provider_lock.clear_holder(ch, "someone-else") == "absent"
    assert provider_lock.clear_holder(ch, holder) == "live"
    ch.advance(provider_lock.LOCK_TTL_S + 1)
    ch.participants[a]["beat"] = ch.now()
    assert provider_lock.clear_holder(ch, holder) == "live"
    ch.advance(provider_lock.LOCK_TTL_S + 1)  # its heartbeat expired too
    ch.tags.add(participant_tag(a))
    assert provider_lock.clear_holder(ch, holder) == "running"
    ch.tags.clear()
    assert provider_lock.clear_holder(ch, holder) == "cleared"
    assert provider_lock.LOCK_NAME not in ch.keys


def test_the_cli_clears_only_a_stopped_holder(ch, monkeypatch, capsys):
    from server.cli.app import main
    from server.core.schema import migrate

    a = str(uuid4())
    ch.participant(a)
    holder = f"{a}/{uuid4()}"
    ch.seed(provider_lock.LOCK_NAME, holder, ttl=provider_lock.LOCK_TTL_S)
    monkeypatch.setattr(migrate, "client", lambda **_: ch)
    assert main(["provider-lock", "clear", "--holder", holder]) == 1
    assert "renewed the lock" in capsys.readouterr().err
    ch.advance(300)
    assert main(["provider-lock", "clear", "--holder", holder]) == 0
    assert provider_lock.LOCK_NAME not in ch.keys
