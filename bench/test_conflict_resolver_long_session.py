"""The long-session harness's arm rules, offline: each arm on its own ClickHouse, and
an `on` arm counted only once its drain completed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bench import conflict_resolver_long_session as ls


def test_two_arms_on_one_clickhouse_are_refused() -> None:
    argv = ["--base-url", "on=http://on", "--base-url", "off=http://off"]
    with pytest.raises(SystemExit, match="own ClickHouse"):
        ls.main([*argv, "--ch-port", "8123"])
    args = ls._parse([*argv, "--ch", "on=ch1:8123", "--ch", "off=ch2:8123"])
    assert ls._arm_clickhouses(args, ls._servers(args.base_url)) == {
        "on": ("ch1", 8123),
        "off": ("ch2", 8123),
    }


def _arm(monkeypatch: pytest.MonkeyPatch, drained: float | None, owed: int) -> dict[str, Any]:
    queue = {"owed": owed, "recheck": 0, "judged": 1}
    replies = {
        ls._QUEUE_SQL: [queue],
        ls._COST_SQL: [{"resolver_calls": 1, "resolver_cost_usd": 0.0, "namespace_cost_usd": 0.0}],
        ls._LAST_PASS_SQL: [{"status": "ok", "detail": '{"judged": 1}'}],
    }
    monkeypatch.setattr(ls, "_query", lambda ch, sql, ns: replies.get(sql, []))
    monkeypatch.setattr(ls, "wait_for_drain", lambda *a, **k: drained)
    monkeypatch.setattr(ls, "write_set_session", lambda *a: None)
    monkeypatch.setattr(ls, "_top_hits", lambda *a, **k: {})
    monkeypatch.setattr(ls, "score_by_key", lambda rows, top: {})
    monkeypatch.setattr(ls, "remember_bench_namespace", lambda *a: None)
    args = SimpleNamespace(keep_namespaces=False, drain_timeout=1.0, poll=0.0, settle=0.0)
    return ls.run_arm(lambda op, body: {}, object(), "on", "set", args)  # type: ignore[arg-type]


def test_an_on_arm_whose_drain_timed_out_does_not_count(monkeypatch) -> None:
    r = _arm(monkeypatch, drained=None, owed=12)
    assert r["treatment"] == "FAILED"
    assert "12 owed" in r["failure"] and '{"judged": 1}' in r["failure"]


def test_an_on_arm_that_drained_counts(monkeypatch) -> None:
    assert _arm(monkeypatch, drained=3.0, owed=0)["treatment"] == "ok"
