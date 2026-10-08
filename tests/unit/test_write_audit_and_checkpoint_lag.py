"""memory.set leaves a hash-only audit row; the checkpoint backlog is a
gauge."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from server.observability import metrics_gauges

ROOT = Path(__file__).resolve().parents[2]


def test_set_path_audits_hashes_not_values() -> None:
    src = (ROOT / "server" / "memory" / "app" / "set_commit.py").read_text()
    i = src.index('event_type="memory.set"')
    block = src[i - 400 : i + 900]
    for key in ("memory_key", "memory_id", "content_sha256", "source_class", "verdict", "pinned"):
        assert f'"{key}":' in block, key
    assert '"value"' not in block and "serialized_value" not in block
    assert "never fails the write" in block


def test_probe_reads_the_checkpoint_lag_and_the_gauge_is_registered() -> None:
    otel = (ROOT / "server" / "observability" / "otel_probes.py").read_text()
    assert "audit_checkpoint_lag_windows" in otel and "FROM loom.audit_checkpoints" in otel
    assert re.search(r"dateDiff\('hour', max\(window_end\), now\(\)\)", otel)

    class _Meter:
        def __init__(self) -> None:
            self.gauges: dict[str, Any] = {}

        def create_observable_gauge(self, *, name: str, callbacks: Any, **_: Any) -> None:
            self.gauges[name] = callbacks[0]

        def create_counter(self, **_: Any) -> Any:
            return None

        def create_histogram(self, **_: Any) -> Any:
            return None

    m = _Meter()
    metrics_gauges.register_state_gauges(m, ch_probe=lambda: {"audit_checkpoint_lag_windows": 7})
    assert "loom.audit.checkpoint_lag_windows" in m.gauges
