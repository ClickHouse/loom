"""The long-session harness's arm rules, offline: each arm on its own ClickHouse."""

from __future__ import annotations

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
