"""The codes flag goes only where it reads less — a coded
column on a table without patch parts — and every process knows which."""

from __future__ import annotations

import ast
import asyncio
import re
import time
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
    def __init__(
        self,
        coded: list[tuple[str, str]],
        patches: dict[str, int],
        fail: bool = False,
        width: int = 1024,
        codec: bool = True,
    ):
        self.coded, self.patches, self.fail, self.width = coded, patches, fail, width
        self.codec = codec
        self.calls: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if self.fail:
            raise RuntimeError("boom")
        if "system.settings" in sql:
            return [{"n": int(self.codec)}]
        if "system.columns" in sql:
            return [
                {
                    "table": t,
                    "name": c,
                    "compression_codec": f"CODEC(Quantized('rabitq', {self.width}, 0))",
                    "patch_parts": self.patches.get(t, 0),
                }
                for t, c in self.coded
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
    assert len(ch.calls) == 2  # the codec, asked until it answers, then one statement
    await vector_codes_state.probe(ch)
    assert len(ch.calls) == 3
    assert vector_codes_state.stored_width("chunks", "embedding") == 1024
    assert vector_codes_state.stored_width("loom.memory_chunks", "embedding") == 1024
    assert vector_codes_state.stored_width("code_summaries", "embedding") is None


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_the_previous_reading() -> None:
    await vector_codes_state.probe(_FakeCH(coded=[("chunks", "embedding")], patches={}))
    await vector_codes_state.probe(_FakeCH(coded=[], patches={}, fail=True))
    assert vector_codes_state.coded_columns() == {"loom.chunks.embedding"}


async def _the_refresh() -> None:
    """Wait for the re-probe a stale reading scheduled."""
    assert vector_codes_state._refresh is not None, "no refresh was scheduled"
    await vector_codes_state._refresh


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
    await _the_refresh()
    assert len(ch.calls) > n
    monkeypatch.setattr(settings, "vector_codes_state_probe_seconds", 60)
    assert _FLAG not in search_sql.vector_search_settings(table="loom.chunks")


@pytest.mark.asyncio
async def test_a_process_that_only_writes_refreshes_its_widths_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild to a plain column reaches the write guards, not only the reads."""
    from server.core.errors import CodedWidthMismatchError

    ch = _FakeCH(coded=[("chunks", "embedding")], patches={"chunks": 0})
    await vector_codes_state.probe(ch)
    ch.coded = []  # the table was rebuilt plain
    monkeypatch.setattr(settings, "vector_codes_state_probe_seconds", 0)
    with pytest.raises(CodedWidthMismatchError):
        await vector_codes_state.refuse_width("chunks", 8)  # a fresh, old reading
    await _the_refresh()
    monkeypatch.setattr(settings, "vector_codes_state_probe_seconds", 60)
    await vector_codes_state.refuse_width("chunks", 8)


@pytest.mark.asyncio
async def test_a_refusal_rereads_a_reading_older_than_a_few_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table rebuilt outside Loom is not refused on its old width meanwhile."""
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={"chunks": 0})
    await vector_codes_state.probe(ch)
    ch.coded = []  # rebuilt plain by an operator's script
    _age_reading(monkeypatch)
    await vector_codes_state.refuse_width("chunks", 8)  # re-read, then let through


def _age_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    old = vector_codes_state._probed_at - vector_codes_state.FRESH_READING_S - 1
    monkeypatch.setattr(vector_codes_state, "_probed_at", old)


@pytest.mark.asyncio
async def test_placeholders_take_the_width_a_refusal_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with a vector at the rebuilt width and an empty key vector lands."""
    from server.core.vectors.placeholders import pad_rows

    columns = [("memory_embeddings", "embedding"), ("memory_embeddings", "memory_key_embedding")]
    ch = _FakeCH(coded=columns, patches={}, width=3072)
    await vector_codes_state.probe(ch)
    ch.width = 1024  # rebuilt outside Loom
    _age_reading(monkeypatch)
    row = {"embedding": [0.1] * 1024, "memory_key_embedding": []}
    await vector_codes_state.refuse_other_widths("memory_embeddings", [row])
    pad_rows("memory_embeddings", [row])
    assert len(row["memory_key_embedding"]) == 1024


@pytest.mark.asyncio
async def test_a_write_that_embeds_is_judged_on_a_recent_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before any part of it lands, even when the old reading would let it by."""
    from server.core.errors import CodedWidthMismatchError

    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=3072)
    await vector_codes_state.probe(ch)
    ch.width = 1024  # rebuilt outside Loom
    _age_reading(monkeypatch)
    with pytest.raises(CodedWidthMismatchError):
        await vector_codes_state.refuse_width("chunks", 3072)


@pytest.mark.asyncio
async def test_a_refusal_rereads_a_reading_a_second_old(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even inside FRESH_READING_S: the table went back to the served width."""
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=1024)
    await vector_codes_state.probe(ch)
    ch.width = 3072
    old = vector_codes_state._probed_at - vector_codes_state.REFUSAL_READING_S - 0.5
    monkeypatch.setattr(vector_codes_state, "_probed_at", old)
    await vector_codes_state.refuse_width("chunks", 3072)


@pytest.mark.asyncio
async def test_a_probe_that_finishes_last_keeps_the_later_reading() -> None:
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=1024)
    gate = asyncio.Event()
    first_read = ch.query_all

    async def held(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = await first_read(sql, params)
        if "system.columns" in sql:
            await gate.wait()
        return rows

    ch.query_all = held  # type: ignore[method-assign]
    older = asyncio.create_task(vector_codes_state.probe(ch))
    await asyncio.sleep(0)
    ch.query_all = first_read  # type: ignore[method-assign]
    ch.width = 3072
    await vector_codes_state.probe(ch)
    gate.set()
    await older
    assert vector_codes_state.stored_width("chunks", "embedding") == 3072


@pytest.mark.asyncio
async def test_a_failed_probe_is_no_reading_to_refuse_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old widths are not published as fresh; ClickHouse judges the write."""
    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=1024)
    await vector_codes_state.probe(ch)
    _age_reading(monkeypatch)
    ch.fail = True
    await vector_codes_state.refuse_width("chunks", 3072)  # a mismatch, but not refused
    await vector_codes_state.refuse_other_widths("chunks", [{"embedding": [0.1] * 3072}])
    assert vector_codes_state.stored_width("chunks", "embedding") == 1024


@pytest.mark.asyncio
async def test_a_process_whose_first_probes_fail_reads_once_clickhouse_answers() -> None:
    """A worker that booted while ClickHouse was down is not left without a reading."""
    from server.core.errors import CodedWidthMismatchError

    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=1024, fail=True)
    await vector_codes_state.probe(ch)
    assert vector_codes_state._probed_at is None and vector_codes_state._codec_available is None
    ch.fail = False
    with pytest.raises(CodedWidthMismatchError):
        await vector_codes_state.refuse_width("chunks", 3072)
    assert vector_codes_state.stored_width("chunks", "embedding") == 1024


@pytest.mark.asyncio
async def test_inserts_refused_together_share_one_probe() -> None:
    from server.core.errors import CodedWidthMismatchError

    ch = _FakeCH(coded=[("chunks", "embedding")], patches={}, width=1024)
    await vector_codes_state.probe(ch)
    began = time.monotonic()
    n = len(ch.calls)
    ch.width = 3072  # rebuilt outside Loom
    rows = [{"embedding": [0.1] * 1024}]
    results = await asyncio.gather(
        *(
            vector_codes_state.refuse_on_fresh_widths("chunks", rows, since=began)
            for _ in range(10)
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, CodedWidthMismatchError) for r in results)
    assert len(ch.calls) - n == 1


@pytest.mark.asyncio
async def test_concurrent_refusals_share_one_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import CodedWidthMismatchError

    ch = _FakeCH(coded=[("chunks", "embedding")], patches={})
    await vector_codes_state.probe(ch)
    _age_reading(monkeypatch)
    n = len(ch.calls)
    results = await asyncio.gather(
        *(vector_codes_state.refuse_width("chunks", 8) for _ in range(5)), return_exceptions=True
    )
    assert all(isinstance(r, CodedWidthMismatchError) for r in results)
    assert len(ch.calls) - n == 1


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


def test_each_serving_column_is_held_to_the_width_its_writes_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.vectors import code_tables

    monkeypatch.setattr(
        vector_codes_state,
        "_widths",
        {
            "loom.memory_embeddings.embedding": 1024,  # the served model's table
            "loom.code_chunks_small.embedding": 8,  # routed to an 8-dim label
            "loom.code_chunks.embedding": 3072,  # the served model's code table, stale
            "loom._provider_rebuild_x_memory_embeddings_target.embedding": 16,  # staging
        },
    )
    monkeypatch.setattr(code_tables, "_mapping", lambda: {"small": ("loom.code_chunks_small", 8)})
    assert vector_codes_state.width_mismatches(1024) == {"loom.code_chunks.embedding": (3072, 1024)}


def test_only_the_served_models_coded_columns_count_as_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain primary table beside a coded secondary one: a model change has
    nothing coded to rebuild, so any primary width is fine."""
    from server.core.vectors import code_tables

    monkeypatch.setattr(vector_codes_state, "_widths", {"loom.code_chunks_small.embedding": 8})
    monkeypatch.setattr(code_tables, "_mapping", lambda: {"small": ("loom.code_chunks_small", 8)})
    assert not vector_codes_state.served_model_writes_coded()
    assert vector_codes_state.width_mismatches(12) == {}
    monkeypatch.setattr(vector_codes_state, "_widths", {"loom.chunks.embedding": 3072})
    assert vector_codes_state.served_model_writes_coded()


def test_a_vector_its_coded_column_cannot_take_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import CodedWidthMismatchError

    monkeypatch.setattr(
        vector_codes_state,
        "_widths",
        {"loom.memory_embeddings.embedding": 8, "loom.memory_embeddings.memory_key_embedding": 8},
    )
    fits = {"embedding": [0.0] * 8, "memory_key_embedding": []}
    refuse = vector_codes_state.refuse_other_widths
    asyncio.run(refuse("memory_embeddings", [fits]))
    asyncio.run(refuse("loom.chunks", [{"embedding": [0.0] * 3}]))
    with pytest.raises(CodedWidthMismatchError) as e:
        asyncio.run(
            refuse(
                "loom.memory_embeddings",
                [fits, {"embedding": [0.0] * 8, "memory_key_embedding": [1.0]}],
            )
        )
    assert e.value.status_code == 503
    assert e.value.to_detail()["code"] == "CODED_WIDTH_MISMATCH"
    assert "loom.memory_embeddings.memory_key_embedding stores 8-dim" in str(e.value)


def test_the_boot_probe_reads_the_tables_the_boot_migrations_created(monkeypatch) -> None:
    """`connect()` probes before a fresh install's tables exist; the capability
    probe after the migrations must still learn their widths."""
    from server import boot
    from server.core.vectors import vector_floor

    async def _nothing(ch: Any) -> None:
        return None

    monkeypatch.setattr(patch_parts, "probe_patch_parts", _nothing)
    monkeypatch.setattr(vector_floor, "warn_below_vector_floor", _nothing)
    vector_codes_state.reset()
    ch = _FakeCH([], {})
    asyncio.run(vector_codes_state.probe(ch))  # connect(), before the migrations
    ch.coded = [("memory_embeddings", "embedding")]
    asyncio.run(boot.probe_vector_capabilities(ch))
    assert vector_codes_state.stored_width("loom.memory_embeddings", "embedding") == 1024
