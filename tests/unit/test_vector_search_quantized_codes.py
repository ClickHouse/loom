"""The codec setting rides on the SETTINGS fragment only when
the server knows it, so a pre-26.7 ClickHouse never sees an unknown name."""

from __future__ import annotations

import asyncio

import pytest

from server.core.vectors import quantized_setting
from server.core.vectors import search_settings as search_sql
from server.settings import settings

_NAME = "vector_search_use_quantized_codes"


@pytest.fixture(autouse=True)
def _reset_probe():
    quantized_setting.set_quantized_codes_supported(None)
    yield
    quantized_setting.set_quantized_codes_supported(None)


def test_unprobed_server_gets_no_codec_setting() -> None:
    frag = search_sql.vector_search_settings()
    assert frag.startswith("SETTINGS vector_search_index_fetch_multiplier = ")
    assert "vector_search_index_fetch_multiplier = " in frag
    assert _NAME not in frag


def test_probed_support_adds_the_setting_once_after_the_multiplier(monkeypatch) -> None:
    quantized_setting.set_quantized_codes_supported(True)
    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    frag = search_sql.vector_search_settings(fetch_multiplier=3)
    assert frag == (f"SETTINGS vector_search_index_fetch_multiplier = 3.0, {_NAME} = 1")


def test_quantized_reads_use_their_own_smaller_multiplier(monkeypatch) -> None:
    """X3 for the codes-first scan, the HNSW x10 otherwise; an
    explicit caller value still wins."""
    quantized_setting.set_quantized_codes_supported(True)
    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    monkeypatch.setattr(settings, "vector_search_quantized_fetch_multiplier", 3.0)
    assert "vector_search_index_fetch_multiplier = 3.0" in search_sql.vector_search_settings()
    monkeypatch.setattr(settings, "vector_search_quantized_codes", False)
    assert _NAME not in search_sql.vector_search_settings()
    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    assert "vector_search_index_fetch_multiplier = 7.0" in search_sql.vector_search_settings(
        fetch_multiplier=7
    )


def test_default_is_on_but_gated_by_the_replica_probe() -> None:
    """On by default (every vector table is coded); a mixed 26.7/26.6
    cluster's failure is prevented by the per-replica probe,
    which keeps the name off the fragment until every replica knows it."""
    assert settings.vector_search_quantized_codes is True
    quantized_setting.set_quantized_codes_supported(False)
    assert _NAME not in search_sql.vector_search_settings()
    quantized_setting.set_quantized_codes_supported(True)
    assert _NAME in search_sql.vector_search_settings()


class _CH:
    """Answers the clusterAllReplicas probe with (replicas, with_setting), or
    raises on it so the local system.settings fallback runs."""

    def __init__(
        self,
        replicas: int | None = None,
        with_setting: int = 0,
        local_rows=None,
        cluster_error: Exception | None = None,
        local_error: Exception | None = None,
    ) -> None:
        self.replicas, self.with_setting = replicas, with_setting
        self.local_rows, self.cluster_error, self.local_error = (
            local_rows or [],
            cluster_error,
            local_error,
        )
        self.calls: list[str] = []

    async def query_all(self, sql, params=None):
        self.calls.append(sql)
        assert params == {"n": _NAME}
        if "clusterAllReplicas" in sql:
            if self.cluster_error:
                raise self.cluster_error
            return [{"replicas": self.replicas, "with_setting": self.with_setting}]
        if self.local_error:
            raise self.local_error
        return self.local_rows


def _probe(ch: _CH) -> bool:
    return asyncio.run(quantized_setting.probe_vector_search_settings(ch))


def test_probe_needs_every_replica_to_know_the_setting() -> None:
    assert _probe(_CH(replicas=3, with_setting=3)) is True
    assert quantized_setting.quantized_codes_supported() is True


def test_probe_mixed_cluster_stays_off() -> None:
    """Two replicas on 26.7, one on 26.6: unsupported."""
    ch = _CH(replicas=3, with_setting=2)
    assert _probe(ch) is False
    assert not quantized_setting.quantized_codes_supported()
    assert len(ch.calls) == 1, "a mixed answer is final; no local fallback"


def test_probe_falls_back_to_the_local_server_without_a_cluster() -> None:
    ch = _CH(
        cluster_error=RuntimeError("Requested cluster 'default' not found"),
        local_rows=[{"name": _NAME}],
    )
    assert _probe(ch) is True
    assert "system.settings" in ch.calls[1] and "clusterAllReplicas" not in ch.calls[1]
    ch = _CH(cluster_error=RuntimeError("x"), local_rows=[])
    assert _probe(ch) is False


def test_probe_on_error_stays_unsupported() -> None:
    assert _probe(_CH(cluster_error=RuntimeError("x"), local_error=RuntimeError("y"))) is False
    assert not quantized_setting.quantized_codes_supported()
    assert _probe(_CH(replicas=0, with_setting=0)) is False
