"""The `keeper` doctor line: Loom refuses to run without a ClickHouse Keeper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.cli import doctor

_ZK = {"database": "system", "name": "zookeeper_connection", "engine": "SystemZooKeeperConnection"}
_LOCK = {"database": "loom", "name": "_migration_lock", "engine": "KeeperMap"}


@pytest.mark.parametrize(
    ("rows", "grade", "says"),
    [
        ([_ZK, _LOCK, {**_LOCK, "name": "graph_projections"}], doctor.PASS, "Keeper present"),
        ([_ZK, _LOCK], doctor.PASS, "Keeper present"),
        (
            [_ZK, _LOCK, {"database": "loom", "name": "graph_projections", "engine": "MergeTree"}],
            doctor.WARN,
            "a part per memory.set",
        ),
        ([_ZK], doctor.WARN, "run `loom migrate`"),
        ([_LOCK], doctor.FAIL, "Loom needs a ClickHouse Keeper"),
        ([], doctor.FAIL, "keeper.xml"),
    ],
)
def test_the_keeper_line(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]], grade: str, says: str
) -> None:
    monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: rows)
    name, got, detail = doctor._doctor_keeper_check(SimpleNamespace())
    assert (name, got) == ("keeper", grade) and says in detail


def test_an_unreadable_catalog_warns() -> None:
    def boom(*_: object) -> list[dict[str, str]]:
        raise ConnectionError("down")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(doctor, "_doctor_ch_query", boom)
        assert doctor._doctor_keeper_check(SimpleNamespace())[1] == doctor.WARN
    assert "keeper" in [n for n, _, _ in doctor._doctor_ch_read_checks(None)]
