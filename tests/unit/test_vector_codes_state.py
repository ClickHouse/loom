"""The codes flag goes only where it reads less — a coded
column on a table without patch parts — and every process knows which."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from server.core import ch as _ch
from server.core.vectors import codes_state as vector_codes_state
from server.core.vectors import patch_parts, quantized_setting
from server.core.vectors import search_settings as search_sql
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]
_FLAG = "vector_search_use_quantized_codes = 1"

# The connect probe as shipped; the unit conftest replaces it per test.
_real_probe = _ch._probe_vector_state


@pytest.fixture(autouse=True)
def _codes_supported(monkeypatch: pytest.MonkeyPatch):
    quantized_setting.set_quantized_codes_supported(True)
    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    yield
    quantized_setting.set_quantized_codes_supported(None)


class _FakeCH:
    def __init__(self, coded: list[tuple[str, str]], patches: dict[str, int], fail: bool = False):
        self.coded, self.patches, self.fail = coded, patches, fail
        self.calls: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if self.fail:
            raise RuntimeError("boom")
        if "system.columns" in sql:
            return [{"table": t, "name": c} for t, c in self.coded]
        if "system.parts" in sql:
            return [
                {"table": t, "patch_parts": n}
                for t, n in self.patches.items()
                if t in params["tables"]
            ]
        if "clusterAllReplicas" in sql:
            return [{"replicas": 3, "with_setting": 3}]
        return []


def test_unprobed_state_keeps_the_deployment_answer() -> None:
    """Never probed = the deployment-wide fragment: withholding the flag from a
    coded, patch-free table costs several times the bytes read, sending it to a
    patched one a few percent."""
    assert _FLAG in search_sql.vector_search_settings(table="loom.memory_chunks")
    assert _FLAG in search_sql.vector_search_settings()


def test_plain_column_gets_no_flag_and_coded_one_does() -> None:
    vector_codes_state.set_coded({"loom.code_chunks.embedding", "loom.memory_embeddings.embedding"})
    assert _FLAG in search_sql.vector_search_settings(table="loom.code_chunks")
    # the prefix twin and the file cards are CODEC(NONE) here
    assert _FLAG not in search_sql.vector_search_settings(table="loom.code_chunks_prefix")
    assert _FLAG not in search_sql.vector_search_settings(table="loom.code_summaries")
    # the column matters, not just the table
    assert _FLAG not in search_sql.vector_search_settings(
        table="loom.memory_embeddings", column="memory_key_embedding"
    )
    # unqualified names resolve to the loom database
    assert _FLAG in search_sql.vector_search_settings(table="code_chunks")


def test_a_patched_table_gets_no_flag_until_its_patches_merge_away() -> None:
    vector_codes_state.set_coded({"loom.chunks.embedding"})
    patch_parts.set_reading(1, "loom.chunks")
    frag = search_sql.vector_search_settings(table="loom.chunks")
    assert _FLAG not in frag
    # without the flag the multiplier is the (inert) HNSW one, not the codes x3
    assert (
        f"vector_search_index_fetch_multiplier = {float(settings.vector_search_index_fetch_multiplier)}"
        in frag
    )
    patch_parts.set_reading(0, "loom.chunks")
    assert _FLAG in search_sql.vector_search_settings(table="loom.chunks")
    # a failed patch probe (None) is unknown, not "patched"
    patch_parts.set_reading(None, "loom.chunks")
    assert _FLAG in search_sql.vector_search_settings(table="loom.chunks")


def test_explicit_quantized_and_the_replica_probe_still_win() -> None:
    vector_codes_state.set_coded(set())
    # the main fact leg decides for itself
    assert _FLAG in search_sql.vector_search_settings(
        table="loom.memory_embeddings", quantized=True
    )
    vector_codes_state.set_coded({"loom.chunks.embedding"})
    quantized_setting.set_quantized_codes_supported(False)
    assert _FLAG not in search_sql.vector_search_settings(table="loom.chunks")


@pytest.mark.asyncio
async def test_probe_reads_codecs_then_patch_parts_of_coded_tables_only() -> None:
    ch = _FakeCH(
        coded=[("chunks", "embedding"), ("memory_chunks", "embedding")],
        patches={"chunks": 1, "code_summaries": 4},
    )
    await vector_codes_state.probe(ch)
    assert vector_codes_state.coded_columns() == {
        "loom.chunks.embedding",
        "loom.memory_chunks.embedding",
    }
    assert patch_parts.last_reading("loom.chunks") == 1
    # a coded table absent from the parts read has no patch part
    assert patch_parts.last_reading("loom.memory_chunks") == 0
    # an uncoded table is not asked about
    assert patch_parts.last_reading("loom.code_summaries") is None
    assert len(ch.calls) == 2


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_the_previous_reading() -> None:
    await vector_codes_state.probe(_FakeCH(coded=[("chunks", "embedding")], patches={}))
    await vector_codes_state.probe(_FakeCH(coded=[], patches={}, fail=True))
    assert vector_codes_state.coded_columns() == {"loom.chunks.embedding"}


@pytest.mark.asyncio
async def test_a_stale_reading_refreshes_in_the_background(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={"chunks": 0})
    await vector_codes_state.probe(ch)
    n = len(ch.calls)
    search_sql.vector_search_settings(table="loom.chunks")  # fresh: no refresh
    await asyncio.sleep(0)
    assert len(ch.calls) == n
    monkeypatch.setattr(settings, "vector_codes_state_probe_seconds", 0)
    ch.patches["chunks"] = 2
    assert _FLAG in search_sql.vector_search_settings(
        table="loom.chunks"
    )  # this statement: the old reading
    await asyncio.sleep(0.01)
    assert len(ch.calls) > n
    monkeypatch.setattr(settings, "vector_codes_state_probe_seconds", 60)
    assert _FLAG not in search_sql.vector_search_settings(table="loom.chunks")


@pytest.mark.asyncio
async def test_connect_probes_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench importing the services must build chunk reads with the flag,
    so connect probes (not only the server lifespan)."""
    from server.core import ch as ch_mod

    class _Client:
        async def close(self) -> None:
            return None

    async def _fake_get_async_client(**_kw: Any) -> _Client:
        return _Client()

    probed: list[Any] = []

    async def _fake_ensure(c: Any) -> None:
        probed.append(c)

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", _fake_get_async_client)
    monkeypatch.setattr(vector_codes_state, "ensure_probed", _fake_ensure)
    monkeypatch.setattr(ch_mod, "_probe_vector_state", _real_probe)
    c = ch_mod.CHClient()
    await c.connect()
    assert probed == [c]


@pytest.mark.asyncio
async def test_ensure_probed_skips_what_is_known_and_never_raises() -> None:
    quantized_setting.set_quantized_codes_supported(None)
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={})
    await vector_codes_state.ensure_probed(ch)
    assert quantized_setting.quantized_codes_state() is True
    n = len(ch.calls)
    await vector_codes_state.ensure_probed(ch)
    assert len(ch.calls) == n
    vector_codes_state.reset()
    await vector_codes_state.ensure_probed(_FakeCH(coded=[], patches={}, fail=True))


def _calls_in(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "vector_search_settings"
    ]


def test_every_server_vector_read_names_its_table() -> None:
    """The gate only works if the call site says what it reads. The main fact
    leg passes `quantized=` from its own gate instead."""
    missing = []
    for path in (ROOT / "server").rglob("*.py"):
        for call in _calls_in(path):
            kws = {k.arg for k in call.keywords}
            if not kws & {"table", "quantized"}:
                missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not missing, missing


def test_cards_leg_carries_its_measured_multiplier() -> None:
    src = (ROOT / "server/code/store/index/search.py").read_text()
    assert 'table="loom.code_summaries"' in src
    assert settings.code_search_cards_fetch_multiplier == 10.0


def test_rebuild_runner_sets_the_twin_granularity() -> None:
    from scripts.ops import vector_codec_rebuild as vcr

    ddl = (
        "CREATE TABLE loom.code_summaries\n(\n    `org` LowCardinality(String),\n"
        "    `embedding` Array(BFloat16) DEFAULT [] CODEC(NONE),\n    `embedding_dim` UInt16 DEFAULT 0\n)\n"
        "ENGINE = SharedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', version)\n"
        "ORDER BY (org, namespace)\nSETTINGS index_granularity = 8192"
    )
    out = vcr.rewrite_ddl(
        ddl,
        "loom.code_summaries",
        "loom.code_summaries_q",
        ["embedding"],
        None,
        "rabitq",
        256,
        dim=3072,
    )
    assert "CREATE TABLE loom.code_summaries_q" in out
    assert "`embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0))," in out
    assert out.endswith("SETTINGS index_granularity = 256")
    # 0 keeps the table's own
    assert vcr.rewrite_ddl(
        ddl, "loom.code_summaries", "loom.code_summaries_q", ["embedding"], None, dim=3072
    ).endswith("index_granularity = 8192")
    assert re.search(
        r"index_granularity = 512",
        vcr.with_granularity("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x", 512),
    )


def test_rebuild_gate_reads_the_served_multiplier_and_skips_placeholders() -> None:
    from scripts.ops import vector_codec_rebuild as vcr

    _, st = vcr.twin_read("embedding", "[1]", 10.0)
    assert st["vector_search_index_fetch_multiplier"] == 10.0
    src = (ROOT / "scripts/ops/vector_codec_rebuild.py").read_text()
    assert "twin_read(vcol, lit, self.a.gate_fetch_multiplier)" in src
    assert "{vcol}[1] != 0" in src
