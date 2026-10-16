"""The `trusted_proxies` doctor line: a default setting behind one balancer address."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.auth.attempts import _digest
from server.cli import doctor


def _rows(n: int, top_n: int, top: str) -> list[dict[str, object]]:
    return [{"n": str(n), "top_n": str(top_n), "top": top}]


def test_warns_when_every_attempt_comes_from_one_non_loopback_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def query(_s: object, sql: str, params: dict[str, object]) -> list[dict[str, object]]:
        seen.update(sql=sql, params=params)
        return _rows(40, 40, _digest("10.0.3.7"))

    monkeypatch.setattr(doctor, "_doctor_ch_query", query)
    name, grade, detail = doctor._doctor_trusted_proxies_check(
        SimpleNamespace(trusted_proxies="127.0.0.1")
    )
    assert (name, grade) == ("trusted_proxies", doctor.WARN)
    assert "LOOM_TRUSTED_PROXIES" in detail
    assert "loom.auth_attempts" in str(seen["sql"]) and "toIntervalHour(24)" in str(seen["sql"])
    assert seen["params"] == {"kinds": ["login.address", "signup.human", "signup.agent"]}


@pytest.mark.parametrize(
    ("trusted", "rows"),
    [
        ("10.0.0.0/8", _rows(40, 40, "a" * 64)),  # set: nothing to read
        ("127.0.0.1", _rows(40, 40, _digest("127.0.0.1"))),  # a local dev stack
        ("127.0.0.1", _rows(40, 30, "a" * 64)),  # several addresses
        ("127.0.0.1", _rows(5, 5, "a" * 64)),  # too few to judge
        ("127.0.0.1", []),
    ],
)
def test_passes_otherwise(
    monkeypatch: pytest.MonkeyPatch, trusted: str, rows: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: rows)
    name, grade, _ = doctor._doctor_trusted_proxies_check(SimpleNamespace(trusted_proxies=trusted))
    assert (name, grade) == ("trusted_proxies", doctor.PASS)


def test_read_failure_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> list[dict[str, object]]:
        raise RuntimeError("down")

    monkeypatch.setattr(doctor, "_doctor_ch_query", boom)
    line = doctor._doctor_trusted_proxies_check(SimpleNamespace(trusted_proxies="127.0.0.1"))
    assert line[1] == doctor.WARN and "could not read" in line[2]


def test_skipped_line_is_listed_when_clickhouse_is_down() -> None:
    names = [n for n, _, _ in doctor._doctor_ch_read_checks(None)]
    assert "trusted_proxies" in names
