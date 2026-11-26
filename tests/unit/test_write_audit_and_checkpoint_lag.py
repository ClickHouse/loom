"""memory.set leaves a hash-only audit row; the checkpoint backlog is a
gauge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from server.observability import metrics_gauges

ROOT = Path(__file__).resolve().parents[2]


async def test_set_path_audits_hashes_not_values(monkeypatch) -> None:
    from server.core.hashing import sha256_value
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.set_commit import _audit_set_receipt

    rows = []

    class Client:
        async def insert(self, table, batch, **kwargs):
            assert table == "loom.audit_log"
            rows.extend(batch)

    monkeypatch.setattr("server.core.pg.enabled", lambda: False)
    value = "content that belongs only in memory"
    await _audit_set_receipt(
        MemorySetInput(org="o", namespace="n", agent="a", memory_key="k", value=value),
        ch=Client(),
        memory_id="m",
        content_sha256=sha256_value(value),
        source_class="external",
        verdict="clear",
        actor="process:writer",
    )
    subject = json.loads(rows[0]["subject"])
    assert set(subject) == {
        "memory_key",
        "memory_id",
        "content_sha256",
        "source_class",
        "verdict",
        "pinned",
    }
    assert subject["content_sha256"] == sha256_value(value)
    assert value not in repr(rows)


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
