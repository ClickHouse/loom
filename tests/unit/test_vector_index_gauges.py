"""The vector-index gauges read keys the OTel probe computes."""

from __future__ import annotations


def test_gauges_read_keys_the_probe_returns() -> None:
    """The three index gauges read keys `_probe_state` must compute, or they
    emit nothing (the invariant test's rule, applied to the index gauges)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    otel = (root / "server" / "observability" / "otel_probes.py").read_text()
    metrics = (root / "server" / "observability" / "metrics_gauges.py").read_text()
    for gauge, key in {
        "loom.vector_index.searches_total": "usearch_searches_total",
        "loom.vector_index.adds_total": "usearch_adds_total",
        "loom.vector_index.reads_indexed_ratio": "vector_reads_indexed_ratio",
    }.items():
        assert f'name="{gauge}"' in metrics
        assert f'_reading("{key}")' in metrics
        assert f"AS {key}" in otel
        assert f'"{key}":' in otel
