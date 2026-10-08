"""Every ClickHouse image CI pulls is the one dev/ci_start_clickhouse.sh pins.

The unit job copies its clickhouse binary out of that image, so the
clickhouse-local checks and the integration server parse the same SQL.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_IMAGE = re.compile(r"clickhouse/clickhouse-server:([0-9][\w.]*)")


def test_ci_pulls_one_clickhouse_version() -> None:
    (pinned,) = set(_IMAGE.findall((ROOT / "dev" / "ci_start_clickhouse.sh").read_text()))
    used = set(_IMAGE.findall((ROOT / ".github" / "workflows" / "ci.yml").read_text()))
    assert used == {pinned}, (
        f"ci.yml pulls {sorted(used)}; dev/ci_start_clickhouse.sh pins {pinned}"
    )
