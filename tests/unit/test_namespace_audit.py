"""namespace.audit — how a namespace is being used.

Each finding fires on a fake ClickHouse that answers by the substring that
identifies its SQL; a failing check lands in `skipped`; `healthy` is "no
critical"; shares are bounded; samples never carry a value.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from server.identity.api.models_namespace_catalog import NamespaceAuditInput, NamespaceAuditOutput
from server.identity.app import namespace_audit as na
from server.operations import toolsets
from server.transport import mcp_descriptions

pytestmark = pytest.mark.usefixtures("stub_model_command")


class _CH:
    """Answers `query_all` / `query_one` by the first marker found in the SQL."""

    def __init__(self, by_marker: dict[str, Any] | None = None):
        self.by_marker = by_marker or {}
        self.sqls: list[str] = []
        self.params: list[dict[str, Any]] = []

    def _answer(self, sql: str) -> Any:
        for marker, rows in self.by_marker.items():
            if marker in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        return list(rows) if rows else []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows


# Markers that identify each read (a substring only that SQL carries).
M_LIVE = "countIf(memory_type = 'episodic') AS episodic_rows"
M_EMB = "AS excerpt_bytes"
M_LEDGER = "AS sets"
M_HOT = "HAVING reads >="
M_INDEX = "substring(value, 1, {index_clip:UInt32})"
M_DOUBLE = "AND JSONType(value) = 'String' AND JSONType(JSONExtractString(value))"
M_PREFIX = "length(value) > {clip:UInt32}"
M_OVER = "length(value) > {advisory:UInt32}"
M_UNEMB = "AS error_class"
M_HASHKEY = "match(memory_key, {hash_key_re:String})"
M_COORD = "hasAny(splitByChar('/', lower(m.memory_key))"
M_EPI = "length(value) > {episodic_chars:UInt32}"
M_VER = "FROM loom.memory_versions FINAL"


def _inp(**kw: Any) -> NamespaceAuditInput:
    base = {"org": "o", "namespace": "ns"}
    base.update(kw)
    return NamespaceAuditInput(**base)


async def _run(
    monkeypatch: pytest.MonkeyPatch, by_marker: dict[str, Any], **kw: Any
) -> tuple[NamespaceAuditOutput, _CH]:
    ch = _CH(by_marker)
    monkeypatch.setattr(na, "get_client", lambda: ch)
    return await na.handle_namespace_audit(_inp(**kw)), ch


def _finding(out: NamespaceAuditOutput, code: str):
    return next(f for f in out.findings if f.code == code)


def _live(
    rows: int, bytes_: int, searchable: int | None = None, episodic: int = 0
) -> dict[str, Any]:
    return {
        "live_rows": rows,
        "live_bytes": bytes_,
        "searchable_rows": rows if searchable is None else searchable,
        "episodic_rows": episodic,
    }


def _value_rows(total: int, total_bytes: int, keys: list[str]) -> list[dict[str, Any]]:
    return [
        {"total": total, "total_bytes": total_bytes, "memory_key": k, "value_chars": 900}
        for k in keys
    ]


# ---------------------------------------------------------------------------
# The empty namespace: every check runs, nothing fires, nothing skipped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_namespace_lists_every_check_as_info_and_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(monkeypatch, {})
    assert out.healthy is True and (out.criticals, out.warnings) == (0, 0)
    assert [f.code for f in out.findings] == list(na.FINDING_CODES)
    assert len(out.findings) == 10 and out.infos == 10
    assert all(f.count == 0 and f.share == 0.0 and f.samples == [] for f in out.findings)
    assert out.skipped == []
    assert out.window_seconds == 7 * 86400 and out.summary.get_search_ratio == 0.0
    # every read is scoped to the namespace and binds the caller's values, never interpolates them
    assert all("{org:String}" in s and "{ns:String}" in s for s in ch.sqls)
    assert all(p["org"] == "o" and p["ns"] == "ns" and p["win"] == 7 * 86400 for p in ch.params)
    # the embed clip is the constant memory.set embeds by, not a literal
    assert all(p["clip"] == na.EXCERPT_LIMIT for p in ch.params) and "1200" not in "".join(ch.sqls)
    # the ledger reads are bounded by the window; the value reads are FINAL-scoped
    assert all("INTERVAL {win:UInt32} SECOND" in s for s in ch.sqls if "agent_events" in s)
    assert all(
        "FINAL" in s for s in ch.sqls if "FROM loom.memory " in s or "loom.memory_embeddings" in s
    )


# ---------------------------------------------------------------------------
# exact_key_store — the ledger ratio and its thresholds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_key_store_fires_on_the_measured_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = [
        {"memory_key": "sre/v1/leases/worker", "reads": 116},
        {"memory_key": "sre/v1/current/index", "reads": 40},
    ]
    out, _ = await _run(
        monkeypatch,
        {
            M_LEDGER: {"gets": 6606, "searches": 52, "sets": 1687},
            M_HOT: hot,
        },
    )
    f = _finding(out, "exact_key_store")
    assert f.severity == "critical" and f.count == 6606
    assert f.share == pytest.approx(6606 / 6658)
    assert f.samples == [
        {"memory_key": "sre/v1/leases/worker", "reads": 116},
        {"memory_key": "sre/v1/current/index", "reads": 40},
    ]
    assert "working_memory" in f.remedy and "memory.search" in f.remedy
    assert out.summary.gets == 6606 and out.summary.searches == 52 and out.summary.sets == 1687
    assert out.summary.get_search_ratio == pytest.approx(127.04)
    assert out.healthy is False and out.criticals == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gets", "searches", "severity"),
    [
        (199, 0, "info"),  # under the minimum: a small namespace is not an object store
        (200, 0, "critical"),  # at the minimum with no searches
        (400, 20, "critical"),  # exactly 20x
        (399, 20, "info"),  # under 20x
        (5000, 300, "info"),  # many reads but searched too
        (0, 0, "info"),
    ],
)
async def test_exact_key_store_thresholds(
    monkeypatch: pytest.MonkeyPatch, gets: int, searches: int, severity: str
) -> None:
    out, _ = await _run(monkeypatch, {M_LEDGER: {"gets": gets, "searches": searches, "sets": 1}})
    f = _finding(out, "exact_key_store")
    assert f.severity == severity, (gets, searches)
    assert 0.0 <= f.share <= 1.0
    assert out.healthy is (severity != "critical")


# ---------------------------------------------------------------------------
# hand_rolled_index_rows — the Python scan and its cap.
# ---------------------------------------------------------------------------


def test_count_index_rows_counts_other_keys_named_as_tokens() -> None:
    keys = [f"sre/v1/evidence/case-1/{i:02d}" for i in range(10)]
    index_value = '{"current": [' + ", ".join(f'"{k}"' for k in keys) + "]}"
    rows = [(k, f"a claim about {k}") for k in keys] + [("sre/v1/current/index", index_value)]
    # the index row names 10 other keys; a claim naming itself does not count
    assert na.count_index_rows(rows) == [("sre/v1/current/index", 10)]
    # a list of seven is under the threshold
    seven = " ".join(keys[:7])
    assert na.count_index_rows(rows + [("sre/v1/short", seven)]) == [("sre/v1/current/index", 10)]


@pytest.mark.asyncio
async def test_hand_rolled_index_rows_finding_and_scan_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = [f"k/{i}" for i in range(8)]
    rows = [{"memory_key": k, "value": "claim"} for k in keys] + [
        {"memory_key": "idx", "value": ",".join(keys)},
    ]
    out, _ = await _run(monkeypatch, {M_LIVE: _live(9, 900), M_INDEX: rows})
    f = _finding(out, "hand_rolled_index_rows")
    assert f.count == 1 and f.severity == "warning" and f.share == pytest.approx(1 / 9)
    assert f.samples == [{"memory_key": "idx", "keys_named": 8}]
    assert "memory.list prefix" in f.remedy and "supersedes" in f.remedy

    # above the cap the check is skipped and says so; nothing is fetched
    out2, ch2 = await _run(monkeypatch, {M_LIVE: _live(20_001, 1), M_INDEX: rows})
    assert "hand_rolled_index_rows" not in [f.code for f in out2.findings]
    assert out2.skipped == ["hand_rolled_index_rows: 20001 live rows exceed the 20000-row scan cap"]
    assert not any(M_INDEX in s for s in ch2.sqls)


# ---------------------------------------------------------------------------
# The value-shape checks over loom.memory.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_encoded_values_critical_share_and_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(786, 21_500_000),
            M_DOUBLE: _value_rows(760, 20_000_000, ["sre/v1/evidence/a", "sre/v1/evidence/b"]),
        },
    )
    f = _finding(out, "double_encoded_values")
    assert f.severity == "critical" and f.count == 760 and f.bytes == 20_000_000
    assert f.share == pytest.approx(760 / 786)
    assert f.samples[0] == {"memory_key": "sre/v1/evidence/a", "value_chars": 900}
    assert "double-encoded value" in f.remedy
    assert out.healthy is False


@pytest.mark.asyncio
async def test_double_encoded_under_a_quarter_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = await _run(
        monkeypatch, {M_LIVE: _live(100, 1000), M_DOUBLE: _value_rows(24, 500, ["k"])}
    )
    f = _finding(out, "double_encoded_values")
    assert f.severity == "warning" and f.share == pytest.approx(0.24)


@pytest.mark.asyncio
async def test_json_prefix_excerpts_is_a_warning_at_ten_percent_of_searchable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1000, 1, searchable=200),
            M_PREFIX: _value_rows(20, 4_000_000, ["sre/v1/models/x"]),
        },
    )
    f = _finding(out, "json_prefix_excerpts")
    assert f.severity == "warning" and f.share == pytest.approx(0.1) and f.count == 20
    assert "knowledge.upsert" in f.remedy and "claim" in f.remedy.lower()
    sql = next(s for s in ch.sqls if M_PREFIX in s)
    # only searchable types, only past the clip, only JSON-shaped values
    assert "memory_type NOT IN {ns_types:Array(String)}" in sql
    assert "JSONType(value) IN ('Object', 'Array')" in sql
    assert ch.params[0]["ns_types"] == ["episodic", "profile"]
    out2, _ = await _run(
        monkeypatch, {M_LIVE: _live(1000, 1, searchable=201), M_PREFIX: _value_rows(20, 1, ["k"])}
    )
    assert _finding(out2, "json_prefix_excerpts").severity == "info"


@pytest.mark.asyncio
async def test_oversized_values_uses_the_advisory_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    out, ch = await _run(
        monkeypatch, {M_LIVE: _live(10, 1), M_OVER: _value_rows(3, 3_000_000, ["a", "b", "c"])}
    )
    f = _finding(out, "oversized_values")
    assert f.severity == "warning" and f.count == 3 and f.bytes == 3_000_000
    assert all(p["advisory"] == settings.memory_value_advisory_chars for p in ch.params)
    assert "knowledge.upsert" in f.remedy


@pytest.mark.asyncio
async def test_unembedded_rows_samples_carry_key_and_error_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"total": 179, "memory_key": "sre/v1/models/m1", "error_class": "EmbeddingInputTooLong"}
    ]
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(786, 1, searchable=600),
            M_UNEMB: rows,
            M_EMB: {"excerpt_bytes": 15_400_000, "unembedded_rows": 179},
        },
    )
    f = _finding(out, "unembedded_rows")
    assert f.count == 179 and f.severity == "warning" and f.share == pytest.approx(179 / 600)
    assert f.samples == [{"memory_key": "sre/v1/models/m1", "error_class": "EmbeddingInputTooLong"}]
    assert out.summary.unembedded_rows == 179 and out.summary.excerpt_bytes == 15_400_000
    sql = next(s for s in ch.sqls if M_UNEMB in s)
    assert "embedding_dim = 0" in sql and "embed_last_error != ''" in sql


@pytest.mark.asyncio
async def test_hash_keys_warning_at_a_quarter(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    out, _ = await _run(
        monkeypatch,
        {M_LIVE: _live(100, 1), M_HASHKEY: _value_rows(25, 1, ["sre/v1/evidence/c/" + "a" * 64])},
    )
    f = _finding(out, "hash_keys")
    assert f.severity == "warning" and f.share == pytest.approx(0.25)
    assert "<scope>/<kind>/<id>" in f.remedy
    seg = re.compile(na._HASH_KEY_SEGMENT_RE)
    # the row's identity is a hash: last segment ≥ 24 hex, or any segment a ≥ 40-hex sha
    assert seg.search("sre/v1/evidence/case/" + "9f" * 16)
    assert seg.search("sre/v1/evidence/c610684f5cea771c7c794c5e/008554a9" + "ab" * 28)
    assert seg.search("sre/pr/" + "ab" * 20 + "/3")  # a 40-hex sha mid-path
    assert seg.search("d41d8cd98f00b204e9800998ecf8427e")
    # a readable layout with a hex scope id mid-path is an id, not a hash key
    assert not seg.search("sre/case/c610684f5cea771c7c794c5e/evidence/E-rule")
    assert not seg.search("sre/pr/" + "ab" * 12 + "/3")
    assert not seg.search("sre/case/alert-42/evidence/e17")
    assert not seg.search("deploy_window")


@pytest.mark.asyncio
async def test_coordination_as_memory_joins_the_hot_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "total": 2,
            "total_bytes": 118,
            "memory_key": "sre/v1/leases/worker",
            "value_chars": 59,
            "reads": 116,
        },
        {
            "total": 2,
            "total_bytes": 118,
            "memory_key": "sre/v1/lock/next",
            "value_chars": 59,
            "reads": 0,
        },
    ]
    out, ch = await _run(monkeypatch, {M_LIVE: _live(786, 1), M_COORD: rows})
    f = _finding(out, "coordination_as_memory")
    assert f.count == 2 and f.severity == "warning" and f.bytes == 118
    assert f.samples[0] == {"memory_key": "sre/v1/leases/worker", "value_chars": 59, "reads": 116}
    assert "working_memory" in f.remedy and "never embedded" in f.remedy
    sql = next(s for s in ch.sqls if M_COORD in s)
    assert "LEFT JOIN" in sql and "HAVING reads >= {hot:UInt32}" in sql
    assert ch.params[0]["coord_segs"] == list(na.COORDINATION_SEGMENTS)
    # A short value is a lease only when it is also FEW WORDS — a
    # 158-char lesson in prose read 32× by key is a memory, not a pointer.
    assert "length(splitByWhitespace(m.value)) < {coord_words:UInt32}" in sql
    assert ch.params[0]["coord_words"] == na.COORDINATION_MAX_WORDS == 8
    assert "8 words" in f.detail
    assert "lease" in na.COORDINATION_SEGMENTS and "freezes" in na.COORDINATION_SEGMENTS


@pytest.mark.asyncio
async def test_episodic_blobs_share_is_of_episodic_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(500, 1, episodic=40),
            M_EPI: _value_rows(34, 13_000_000, ["sre/v1/runs/r1"]),
        },
    )
    f = _finding(out, "episodic_blobs")
    assert f.severity == "warning" and f.share == pytest.approx(34 / 40) and f.bytes == 13_000_000
    assert "context.offload" in f.remedy


@pytest.mark.asyncio
async def test_version_churn_fires_on_bytes_or_rows_and_fills_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = [
        {
            "total": 1687,
            "total_bytes": 51_400_000,
            "memory_key": "sre/v1/models/m1",
            "versions": 40,
            "bytes": 30_000_000,
        }
    ]
    out, _ = await _run(monkeypatch, {M_LIVE: _live(786, 21_500_000), M_VER: versions})
    f = _finding(out, "version_churn")
    assert f.severity == "warning" and f.count == 1687 and f.bytes == 51_400_000
    assert f.share == pytest.approx((1687 - 786) / 1687)
    assert f.samples == [{"memory_key": "sre/v1/models/m1", "versions": 40, "bytes": 30_000_000}]
    assert (out.summary.version_rows, out.summary.version_bytes) == (1687, 51_400_000)
    assert "knowledge.upsert" in f.remedy
    # under both ratios it is information
    quiet = [
        {"total": 1000, "total_bytes": 30_000_000, "memory_key": "k", "versions": 2, "bytes": 10}
    ]
    out2, _ = await _run(monkeypatch, {M_LIVE: _live(786, 21_500_000), M_VER: quiet})
    assert _finding(out2, "version_churn").severity == "info"


# ---------------------------------------------------------------------------
# Report-level invariants.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_check_is_skipped_not_raised_and_the_rest_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = await _run(
        monkeypatch, {M_UNEMB: RuntimeError("no such table"), M_VER: TimeoutError()}
    )
    assert out.skipped == ["unembedded_rows: RuntimeError", "version_churn: TimeoutError"]
    codes = [f.code for f in out.findings]
    assert "unembedded_rows" not in codes and "version_churn" not in codes
    assert len(codes) == len(na.FINDING_CODES) - 2
    assert out.healthy is True


@pytest.mark.asyncio
async def test_a_failing_summary_read_is_skipped_and_checks_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = await _run(
        monkeypatch, {M_LEDGER: RuntimeError("ledger down"), M_LIVE: _live(10, 100)}
    )
    assert out.skipped == ["summary_ledger: RuntimeError"]
    assert out.summary.gets == 0 and out.summary.live_rows == 10
    assert len(out.findings) == len(na.FINDING_CODES)


@pytest.mark.asyncio
async def test_healthy_is_false_only_on_criticals(monkeypatch: pytest.MonkeyPatch) -> None:
    # warnings alone: healthy
    out, _ = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_OVER: _value_rows(3, 1, ["a"])})
    assert out.warnings >= 1 and out.criticals == 0 and out.healthy is True
    # one critical: unhealthy
    out2, _ = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_DOUBLE: _value_rows(9, 1, ["a"])})
    assert out2.criticals == 1 and out2.healthy is False
    assert out2.criticals + out2.warnings + out2.infos == len(out2.findings)


@pytest.mark.asyncio
async def test_shares_are_bounded_when_counts_exceed_the_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a stale summary (0 live rows) or a count above the population never escapes 0..1
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(0, 0),
            M_DOUBLE: _value_rows(50, 1, ["a"]),
            M_HASHKEY: _value_rows(5, 1, ["b"]),
        },
    )
    for f in out.findings:
        assert 0.0 <= f.share <= 1.0, f.code
    out2, _ = await _run(monkeypatch, {M_LIVE: _live(3, 1), M_DOUBLE: _value_rows(50, 1, ["a"])})
    assert _finding(out2, "double_encoded_values").share == 1.0


@pytest.mark.asyncio
async def test_samples_never_carry_values_and_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "the value text nobody should see"
    keys = [f"k{i}" for i in range(10)]
    value_rows = [
        {"total": 10, "total_bytes": 1, "memory_key": k, "value_chars": 33, "value": secret}
        for k in keys
    ]
    by_marker = {
        M_LIVE: _live(10, 330),
        M_DOUBLE: value_rows,
        M_PREFIX: value_rows,
        M_OVER: value_rows,
        M_HASHKEY: value_rows,
        M_EPI: value_rows,
        M_COORD: [dict(r, reads=30) for r in value_rows],
        M_INDEX: [{"memory_key": k, "value": secret} for k in keys],
        M_HOT: [{"memory_key": k, "reads": 25} for k in keys],
        M_VER: [
            {"total": 30, "total_bytes": 3, "memory_key": k, "versions": 3, "bytes": 1}
            for k in keys
        ],
        M_UNEMB: [{"total": 10, "memory_key": k, "error_class": "Err"} for k in keys],
    }
    out, _ = await _run(monkeypatch, by_marker, sample_limit=3)
    for f in out.findings:
        assert len(f.samples) <= 3, f.code
        for s in f.samples:
            assert "value" not in s and "content_excerpt" not in s, f.code
            assert secret not in " ".join(str(v) for v in s.values()), f.code


def test_split_drops_aggregates_and_any_value_bearing_column() -> None:
    rows = [
        {
            "total": 3,
            "total_bytes": 9,
            "memory_key": "k",
            "value_chars": 5,
            "value": "secret",
            "content_excerpt": "secret",
            "query": "secret",
        }
    ]
    total, total_bytes, samples = na._split(rows, 5)
    assert (total, total_bytes) == (3, 9)
    assert samples == [{"memory_key": "k", "value_chars": 5}]
    assert na._split([], 5) == (0, 0, [])


@pytest.mark.asyncio
async def test_no_sample_read_selects_the_value_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value-shape reads select `length(value)`, never `value`; the only
    read that fetches values is the index scan, whose rows never leave the
    handler."""
    import re

    _out, ch = await _run(monkeypatch, {})
    for sql in ch.sqls:
        if M_INDEX in sql or "count() OVER ()" not in sql:
            continue
        projection = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
        assert not re.search(
            r"(?<![\w.(])(m\.)?value(?![\w_(])", projection.replace("m.value)", ")")
        ), sql


def test_no_check_reads_query_text() -> None:
    """The ledger keeps no query text (`event_args` replaces free text with
    `sha256:<16 hex>/<length>`) and `search_trace_log.query` is opt-in, so no
    finding is derived from what was searched for (a `hash_shaped_queries`
    check would grade every search critical)."""
    src = inspect.getsource(na)
    assert "hash_shaped_queries" not in na.FINDING_CODES
    body = src.split('"""', 2)[2]  # past the module docstring, which names both
    assert "search_trace_log" not in body
    assert "args['query']" not in body


def test_every_finding_has_text_and_a_recipe_or_op_remedy() -> None:
    remedies = ("memory.", "working_memory", "knowledge.upsert", "context.offload", "guides/")
    assert set(na._TEXT) == set(na.FINDING_CODES) and len(na.FINDING_CODES) == 10
    for code, (detail, remedy) in na._TEXT.items():
        assert detail.endswith(".") and len(detail) > 40, code
        assert any(r in remedy for r in remedies), code


def test_input_bounds() -> None:
    from pydantic import ValidationError

    _inp(window_seconds=60)
    _inp(window_seconds=90 * 86400, sample_limit=50)
    for bad in (
        {"window_seconds": 59},
        {"window_seconds": 90 * 86400 + 1},
        {"sample_limit": 51},
        {"sample_limit": -1},
    ):
        with pytest.raises(ValidationError):
            _inp(**bad)


# ---------------------------------------------------------------------------
# Registry, MCP surface, CLI.
# ---------------------------------------------------------------------------


def test_registered_read_only_for_viewers_and_kept_off_the_curated_toolsets() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["namespace.audit"]
    assert op.idempotent is True and op.audit is False
    assert op.required_role.name == "VIEWER"
    assert op.input_model.__name__ == "NamespaceAuditInput"
    assert op.output_model.__name__ == "NamespaceAuditOutput"
    # the listing budget is a shared ratchet: audit is callable by name, never listed on core/coding/atomic
    assert "namespace.audit" not in toolsets._MCP_CORE_OPS
    assert "namespace.audit" not in toolsets._MCP_CODING_OPS
    assert "namespace.audit" not in toolsets._MCP_ATOMIC_OPS
    assert "namespace.audit" not in toolsets._MCP_HIDDEN_OPS
    assert toolsets.op_toolsets("namespace.audit") == ["all"]
    assert "object store" in mcp_descriptions.TOOL_DESCRIPTIONS["namespace.audit"]


def _sample_output() -> NamespaceAuditOutput:
    from datetime import datetime

    from server.identity.api.models_namespace_catalog import (
        NamespaceAuditFinding,
        NamespaceAuditSummary,
    )

    detail, remedy = na._TEXT["exact_key_store"]
    return NamespaceAuditOutput(
        org="clickhouse",
        namespace="ops-alerts",
        computed_at=datetime(2026, 9, 18, 12, 0, 0),
        window_seconds=3 * 86400,
        summary=NamespaceAuditSummary(
            live_rows=786,
            live_bytes=21_500_000,
            excerpt_bytes=15_400_000,
            version_rows=1687,
            version_bytes=51_400_000,
            unembedded_rows=179,
            gets=6606,
            searches=52,
            sets=1687,
            get_search_ratio=127.04,
        ),
        findings=[
            NamespaceAuditFinding(
                code="exact_key_store",
                severity="critical",
                count=6606,
                share=0.992,
                samples=[{"memory_key": "sre/v1/leases/worker", "reads": 116}],
                detail=detail,
                remedy=remedy,
            ),
            NamespaceAuditFinding(
                code="double_encoded_values",
                severity="critical",
                count=760,
                share=0.967,
                bytes=20_000_000,
                samples=[],
                detail=detail,
                remedy=remedy,
            ),
        ],
        skipped=["version_churn: TimeoutError"],
        healthy=False,
        criticals=2,
        warnings=0,
        infos=0,
    )


def test_cli_table_renders_from_a_sample_output() -> None:
    text = na.render_table(_sample_output())
    lines = text.splitlines()
    assert lines[0].startswith(
        "namespace.audit clickhouse/ops-alerts — window 3d, UNHEALTHY (2 critical, 0 warning, 0 info)"
    )
    assert "gets 6606 / searches 52 / sets 1687 (ratio 127.04)" in lines[1]
    assert "live 786 rows / 21,500,000 B; versions 1687 rows / 51,400,000 B" in lines[1]
    header = next(line for line in lines if line.strip().startswith("code"))
    assert header.split() == ["code", "severity", "count", "share", "first", "sample"]
    row = next(line for line in lines if "exact_key_store" in line)
    assert row.split()[:4] == ["exact_key_store", "critical", "6606", "99%"]
    assert row.endswith("memory_key=sre/v1/leases/worker, reads=116")
    row2 = next(line for line in lines if "double_encoded_values" in line)
    assert row2.rstrip().endswith("-")  # no sample
    assert lines[-1] == "  skipped: version_churn: TimeoutError"


def test_cli_registers_audit_namespace_with_a_window_in_days() -> None:
    import argparse

    from server.cli import reports as cli

    parser = argparse.ArgumentParser()
    cli._add_audit_namespace_parser(parser.add_subparsers())
    ns = parser.parse_args(
        ["audit-namespace", "--org", "o", "--namespace", "n", "--window-days", "30", "--json"]
    )
    assert ns.func is cli._cmd_audit_namespace
    assert (ns.org, ns.namespace, ns.window_days, ns.json, ns.samples) == ("o", "n", 30, True, 5)
    assert parser.parse_args(["audit-namespace", "--org", "o", "--namespace", "n"]).window_days == 7
    with pytest.raises(SystemExit):
        parser.parse_args(["audit-namespace", "--org", "o"])


def test_cli_command_prints_the_table_or_json_and_exits_on_criticals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import json

    from server.cli import reports as cli
    from server.identity.app import namespace_audit as handler_mod

    sample = _sample_output()
    calls: list[NamespaceAuditInput] = []

    async def fake_handle(inp: NamespaceAuditInput) -> NamespaceAuditOutput:
        calls.append(inp)
        return sample

    class _Client:
        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...

    monkeypatch.setattr(handler_mod, "handle_namespace_audit", fake_handle)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    args = argparse.Namespace(
        org="clickhouse", namespace="ops-alerts", window_days=3, samples=2, json=False
    )
    assert cli._cmd_audit_namespace(args) == 1
    assert calls[0].window_seconds == 3 * 86400 and calls[0].sample_limit == 2
    assert "exact_key_store" in capsys.readouterr().out
    args.json = True
    assert cli._cmd_audit_namespace(args) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["healthy"] is False and parsed["summary"]["gets"] == 6606
    assert [f["code"] for f in parsed["findings"]] == ["exact_key_store", "double_encoded_values"]
