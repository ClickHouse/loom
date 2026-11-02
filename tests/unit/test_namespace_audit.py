"""namespace.audit — how a namespace is being used.

Each finding fires on a fake ClickHouse that answers by the substring that
identifies its SQL; a failing check lands in `skipped`; `healthy` is "no
critical"; shares are bounded; samples never carry a value.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from typing import Any

import pytest

from server.core.hashing import sha256_value_json
from server.identity.api.models_namespace_catalog import NamespaceAuditInput, NamespaceAuditOutput
from server.identity.app import namespace_audit as na
from server.identity.domain.read_scope import honours_read_scope
from server.operations import toolsets
from server.transport import mcp_descriptions
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

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
M_HOT = "reads >= {hot:UInt32}"
M_INDEX = "substring(value, 1, {index_clip:UInt32})"
M_DOUBLE = "AND JSONType(value) = 'String' AND JSONType(JSONExtractString(value))"
M_PREFIX = "length(value) > {clip:UInt32}"
M_OVER = "length(value) > {advisory:UInt32}"
M_UNEMB = "AS error_class"
M_HASHKEY = "match(memory_key, {hash_key_re:String})"
M_COORD = "hasAny(splitByChar('/', lower(m.memory_key))"
M_EPI = "length(value) > {episodic_chars:UInt32}"
M_VER = "FROM loom.memory_versions FINAL"
# The one read after the checks: the live revision of every sampled row.
M_CITE = "IN {identities:Array(Tuple(String, String, String, String))}"
M_VALUE = "IN {keys:Array(Tuple(String, String, String, String))}"


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


def _hot(rows: list[dict[str, Any]], window_gets: int | None = None) -> list[dict[str, Any]]:
    """Hot-key rows as the read returns them: the window's gets and the hot keys' reads
    ride every row. `window_gets` defaults to the hot keys' own reads (no cold key)."""
    total = sum(int(r["reads"]) for r in rows)
    gets = total if window_gets is None else window_gets
    return [{**r, "hot_reads": total, "window_gets": gets} for r in rows]


def _mid(key: str) -> str:
    """A stable memory_id for a fixture key."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _value_rows(total: int, total_bytes: int, keys: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "total": total,
            "total_bytes": total_bytes,
            "memory_key": k,
            "memory_id": _mid(k),
            "value_chars": 900,
        }
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
    assert len(out.findings) == 13 and out.infos == 13
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
            M_HOT: _hot(hot, 6606),
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


def test_keys_named_counts_other_keys_named_as_tokens() -> None:
    keys = {f"sre/v1/evidence/case-1/{i:02d}" for i in range(10)}
    index_value = '{"current": [' + ", ".join(f'"{k}"' for k in sorted(keys)) + "]}"
    every = keys | {"sre/v1/current/index"}
    # the index row names 10 other keys; a claim naming itself does not count
    assert na.keys_named("sre/v1/current/index", index_value, every) == 10
    own = "sre/v1/evidence/case-1/00"
    assert na.keys_named(own, f"a claim about {own}", every) == 0
    # a list of seven is under the threshold
    seven = na.keys_named("sre/v1/short", " ".join(sorted(keys)[:7]), every)
    assert seven == 7
    assert seven < na.INDEX_ROW_MIN_KEYS


@pytest.mark.asyncio
async def test_hand_rolled_index_rows_finding_and_scan_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = [f"k/{i}" for i in range(8)]
    rows = [{"memory_key": k, "memory_id": _mid(k), "value": "claim"} for k in keys] + [
        {"memory_key": "idx", "memory_id": _mid("idx"), "value": ",".join(keys)},
    ]
    out, _ = await _run(monkeypatch, {M_LIVE: _live(9, 900), M_INDEX: rows})
    f = _finding(out, "hand_rolled_index_rows")
    assert f.count == 1 and f.severity == "warning" and f.share == pytest.approx(1 / 9)
    assert f.samples == [{"memory_key": "idx", "memory_id": _mid("idx"), "keys_named": 8}]
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
    assert f.samples[0] == {
        "memory_key": "sre/v1/evidence/a",
        "memory_id": _mid("sre/v1/evidence/a"),
        "value_chars": 900,
    }
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
    audit_params = [p for sql, p in zip(ch.sqls, ch.params, strict=True) if M_CITE not in sql]
    assert all(p["advisory"] == settings.memory_value_advisory_chars for p in audit_params)
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
    assert "LEFT JOIN" in sql and "HAVING memory_key != '' AND reads >= {hot:UInt32}" in sql
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
    # Missing telemetry is not judged on a ledger read that failed.
    assert out.skipped == [
        "summary_ledger: RuntimeError",
        "telemetry_unavailable: summary read failed",
    ]
    assert out.summary.gets == 0 and out.summary.live_rows == 10
    assert len(out.findings) == len(na.FINDING_CODES) - 1


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
        M_HOT: _hot([{"memory_key": k, "reads": 25} for k in keys]),
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
    """The value-shape reads select `length(value)`, never `value`; the reads that
    fetch values are the index scan and the citation's value read (which hashes
    them), and neither returns a value."""
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
    assert set(na._TEXT) == set(na.FINDING_CODES) and len(na.FINDING_CODES) == 13
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
    assert toolsets.op_toolsets("namespace.audit") == ["all", "assistant"]
    assert "object store" in mcp_descriptions.TOOL_DESCRIPTIONS["namespace.audit"]


def _sample_output() -> NamespaceAuditOutput:
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
                # The join column is not printed.
                samples=[
                    {"memory_key": "sre/v1/leases/worker", "memory_id": _mid("w"), "reads": 116}
                ],
                detail=detail,
                remedy=remedy,
                kind="usage_heuristic",
                rule_version=1,
                source_op="namespace.audit",
                sample_limit=5,
                samples_complete=False,
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
                kind="usage_heuristic",
                rule_version=1,
                source_op="namespace.audit",
                sample_limit=5,
                samples_complete=False,
            ),
        ],
        skipped=["version_churn: TimeoutError"],
        coverage={"rules_run": [], "rules_skipped": ["version_churn"], "complete": False},
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
    assert "not counted" not in lines[1]
    shown = na.render_table(
        _sample_output().model_copy(
            update={"summary": _sample_output().summary.model_copy(update={"inspector_calls": 9})}
        )
    )
    assert shown.splitlines()[1].endswith("; 9 dashboard and assistant calls not counted")
    assert "live 786 rows / 21,500,000 B; versions 1687 rows / 51,400,000 B" in lines[1]
    header = next(line for line in lines if line.strip().startswith("code"))
    assert header.split() == ["code", "severity", "kind", "count", "share", "first", "sample"]
    row = next(line for line in lines if "exact_key_store" in line)
    assert row.split()[:5] == ["exact_key_store", "critical", "heuristic", "6606", "99%"]
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


# ---------------------------------------------------------------------------
# The evidence contract every finding carries.
# ---------------------------------------------------------------------------


def _revision(key: str, version: int, *, agent: str = "writer", mid: str = "") -> dict[str, Any]:
    # As the citation read returns it: the id under `id`.
    return {
        "memory_key": key,
        "id": mid or _mid(key),
        "version": version,
        "sensitivity": "",
        "value_len": 10,
        "agent": agent,
        "user_id": "-",
        "session_id": "-",
        "memory_type": "semantic",
        "source_class": "tool",
        "updated_at": datetime(2026, 1, 1),
    }


def test_every_code_declares_its_kind_version_and_uncertainty() -> None:
    assert set(na._CONTRACT) == set(na.FINDING_CODES)
    # Every heuristic says why a hit may still be a legitimate design.
    assert all(r.uncertainty for r in na._CONTRACT.values() if r.kind == "usage_heuristic")


@pytest.mark.asyncio
async def test_findings_carry_the_contract_and_cite_the_live_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(10, 1),
            M_OVER: _value_rows(3, 3_000_000, ["a", "b"]),
            M_CITE: [_revision("a", 1), _revision("b", 2)],
        },
        sample_limit=2,
    )
    f = _finding(out, "oversized_values")
    assert f.kind == "usage_heuristic" and f.rule_version == 1
    assert f.source_op == "namespace.audit"
    assert f.population == 10 and f.sample_limit == 2
    # Three rows match and two are sampled: the listing is not complete.
    assert f.samples_complete is False
    assert [(e.memory_key, e.version) for e in f.evidence] == [("a", 1), ("b", 2)]
    # Current-state checks have no window; the ledger checks carry the request's.
    assert f.window_seconds is None
    assert _finding(out, "exact_key_store").window_seconds == out.window_seconds
    # One citation read for the whole report, bound to the sampled rows' ids only.
    cites = [p for sql, p in zip(ch.sqls, ch.params, strict=True) if M_CITE in sql]
    assert len(cites) == 1 and cites[0]["ids"] == sorted([_mid("a"), _mid("b")])
    assert cites[0]["identities"] == []
    # Summary reads are rules the findings depend on.
    assert out.coverage.complete
    assert out.coverage.rules_run == [*na._SUMMARY_STEPS, *na.FINDING_CODES, "evidence"]


@pytest.mark.asyncio
async def test_a_key_under_two_identities_cites_only_the_row_that_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched = _revision("deploy", 3, agent="a")
    other = _revision("deploy", 9, agent="b", mid="00000000-0000-0000-0000-0000000000bb")
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(2, 1),
            M_OVER: _value_rows(1, 900, ["deploy"]),
            # The read returns every row it was asked about; only the sampled id is cited.
            M_CITE: [matched, other],
        },
    )
    f = _finding(out, "oversized_values")
    assert [(e.agent, e.version) for e in f.evidence] == [("a", 3)]


@pytest.mark.asyncio
async def test_samples_that_name_no_single_row_are_not_cited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(2, 1),
            M_HOT: _hot([{"memory_key": "lease", "reads": 40}]),
            M_VER: [{"total": 9, "total_bytes": 90, "memory_key": "k", "versions": 9, "bytes": 90}],
        },
    )
    assert _finding(out, "exact_key_store").evidence == []
    assert _finding(out, "version_churn").evidence == []
    assert not any(M_CITE in sql for sql in ch.sqls)


@pytest.mark.asyncio
async def test_version_churn_measures_its_count_against_the_version_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(2, 1),
            M_VER: [{"total": 9, "total_bytes": 90, "memory_key": "k", "versions": 9, "bytes": 90}],
        },
    )
    f = _finding(out, "version_churn")
    assert f.count == 9 and f.population == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sample_limit", "returned", "complete"),
    # The read asks for one row past the cap: a full page is complete, one more is cut.
    [(5, 5, True), (5, 6, False), (0, 0, True), (0, 1, False)],
)
async def test_hot_key_samples_are_complete_only_when_the_read_returned_fewer_than_asked(
    monkeypatch: pytest.MonkeyPatch, sample_limit: int, returned: int, complete: bool
) -> None:
    hot = _hot([{"memory_key": f"k{i}", "reads": 30} for i in range(returned)])
    ledger = {"gets": 30 * returned}
    out, _ = await _run(
        monkeypatch, {M_LIVE: _live(1, 1), M_LEDGER: ledger, M_HOT: hot}, sample_limit=sample_limit
    )
    assert _finding(out, "exact_key_store").samples_complete is complete


@pytest.mark.asyncio
async def test_a_failed_citation_read_leaves_findings_uncited_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(10, 1),
            M_OVER: _value_rows(1, 900, ["a"]),
            M_CITE: TimeoutError("boom"),
        },
    )
    assert _finding(out, "oversized_values").evidence == []
    assert "evidence: TimeoutError" in out.skipped
    assert out.coverage.complete is False and out.coverage.rules_skipped == ["evidence"]


@pytest.mark.asyncio
async def test_coverage_names_a_skipped_check(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = await _run(monkeypatch, {M_LIVE: _live(1, 1), M_VER: TimeoutError("slow")})
    assert out.coverage.rules_skipped == ["version_churn"]
    assert "version_churn" not in out.coverage.rules_run

    out, _ = await _run(monkeypatch, {M_LIVE: TimeoutError("slow")})
    # Missing telemetry is judged on the live summary too.
    assert out.coverage.rules_skipped == ["summary_live", "telemetry_unavailable"]
    assert "summary_live" not in out.coverage.rules_run
    assert out.coverage.complete is False


@pytest.mark.asyncio
async def test_a_population_whose_summary_read_was_skipped_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashed = [{"total": 3, "memory_key": "a", "memory_id": _mid("a")}]
    out, _ = await _run(monkeypatch, {M_LIVE: TimeoutError("slow"), M_HASHKEY: hashed})
    # A count against a denominator never read is not a measurement.
    assert _finding(out, "hash_keys").population is None
    out, _ = await _run(monkeypatch, {M_LIVE: _live(10, 900), M_HASHKEY: hashed})
    assert _finding(out, "hash_keys").population == 10


@pytest.mark.asyncio
async def test_an_index_row_is_judged_and_cited_per_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [f"k/{i}" for i in range(8)]
    plain, index = "00000000-0000-0000-0000-0000000000aa", "00000000-0000-0000-0000-0000000000bb"
    rows = [{"memory_key": k, "memory_id": _mid(k), "value": "claim"} for k in keys] + [
        # The same key under two identities: a plain claim first, the index second.
        {"memory_key": "idx", "memory_id": plain, "value": "a claim"},
        {"memory_key": "idx", "memory_id": index, "value": ",".join(keys)},
    ]
    cited = _revision("idx", 2, agent="b", mid=index)
    out, ch = await _run(monkeypatch, {M_LIVE: _live(10, 900), M_INDEX: rows, M_CITE: [cited]})
    f = _finding(out, "hand_rolled_index_rows")
    # The citation read is asked for the index row alone; the sample joins it by id.
    assert next(p for sql, p in zip(ch.sqls, ch.params, strict=True) if M_CITE in sql)["ids"] == [
        index
    ]
    assert [e.memory_id for e in f.evidence] == [index]
    assert f.samples == [{"memory_key": "idx", "memory_id": index, "keys_named": 8}]


@pytest.mark.asyncio
async def test_exact_key_samples_are_incomplete_when_cold_keys_add_to_the_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = [{"memory_key": "lease", "reads": 40}]
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1, 1),
            M_LEDGER: {"gets": 52, "searches": 0, "sets": 0},
            M_HOT: _hot(hot, 52),
        },
    )
    assert _finding(out, "exact_key_store").samples_complete is False
    out, _ = await _run(
        monkeypatch,
        {M_LIVE: _live(1, 1), M_LEDGER: {"gets": 40, "searches": 0, "sets": 0}, M_HOT: _hot(hot)},
    )
    assert _finding(out, "exact_key_store").samples_complete is True


@pytest.mark.asyncio
async def test_a_row_behind_two_samples_is_cited_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # One memory row, two embedding rows (two embedders during a model change).
    identity = {"memory_key": "k", "agent": "writer", "user_id": "-", "session_id": "-"}
    unembedded = [{"total": 2, **identity, "error_class": "TooLong"}] * 2
    out, _ = await _run(
        monkeypatch,
        {M_LIVE: _live(1, 1), M_UNEMB: unembedded, M_CITE: [_revision("k", 4)]},
    )
    f = _finding(out, "unembedded_rows")
    assert len(f.samples) == 2 and [e.version for e in f.evidence] == [4]


@pytest.mark.asyncio
async def test_a_row_is_cited_with_the_hash_of_the_value_memory_get_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # memory.get hashes the value it serves, decoded: an object from the JSON column, and
    # a string value, which is stored raw (not JSON).
    ident = {"agent": "writer", "user_id": "-", "session_id": "-"}
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(2, 2),
            M_OVER: _value_rows(2, 900, ["a", "b"]),
            M_CITE: [_revision("a", 1), _revision("b", 1)],
            M_VALUE: [
                {**ident, "memory_key": "a", "version": 1, "value": '{"b": 1}'},
                {**ident, "memory_key": "b", "version": 1, "value": "Tuesdays."},
            ],
        },
    )
    cited = {e.memory_key: e.content_sha256 for e in _finding(out, "oversized_values").evidence}
    assert cited == {"a": sha256_value_json('{"b": 1}'), "b": sha256_value_json('"Tuesdays."')}
    # The values are read in one statement, by each row's sorting key.
    reads = [p for sql, p in zip(ch.sqls, ch.params, strict=True) if M_VALUE in sql]
    assert [sorted(r["keys"]) for r in reads] == [
        [("writer", "-", "-", "a"), ("writer", "-", "-", "b")]
    ]


@pytest.mark.asyncio
async def test_a_value_over_the_hash_bound_is_cited_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "namespace_report_hash_max_bytes", 100)
    big = {**_revision("big", 1), "value_len": 3_000_000}
    out, ch = await _run(
        monkeypatch,
        {M_LIVE: _live(1, 1), M_OVER: _value_rows(1, 3_000_000, ["big"]), M_CITE: [big]},
    )
    # The citation names the revision; the value is never read to hash it.
    [evidence] = _finding(out, "oversized_values").evidence
    assert (evidence.version, evidence.content_sha256) == (1, "")
    assert not any(M_VALUE in sql for sql in ch.sqls)
    # A value exactly at the bound is hashed, and the value read carries the bound too.
    at = {**_revision("at", 1), "value_len": 100}
    ident = {"agent": "writer", "user_id": "-", "session_id": "-", "memory_key": "at"}
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1, 1),
            M_OVER: _value_rows(1, 100, ["at"]),
            M_CITE: [at],
            M_VALUE: [{**ident, "version": 1, "value": '"x"'}],
        },
    )
    assert [e.content_sha256 for e in _finding(out, "oversized_values").evidence] == [
        sha256_value_json('"x"')
    ]
    assert (
        next(p for sql, p in zip(ch.sqls, ch.params, strict=True) if M_VALUE in sql)["max"] == 100
    )
    # 0 cites no hash: not even an empty value is read.
    monkeypatch.setattr(settings, "namespace_report_hash_max_bytes", 0)
    empty = {**_revision("empty", 1), "value_len": 0}
    out, ch = await _run(
        monkeypatch,
        {M_LIVE: _live(1, 1), M_OVER: _value_rows(1, 0, ["empty"]), M_CITE: [empty]},
    )
    assert [e.content_sha256 for e in _finding(out, "oversized_values").evidence] == [""]
    assert not any(M_VALUE in sql for sql in ch.sqls)


@pytest.mark.asyncio
async def test_a_high_sensitivity_row_is_cited_without_its_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1, 1),
            M_OVER: _value_rows(1, 900, ["pin"]),
            M_CITE: [{**_revision("pin", 1), "sensitivity": "high"}],
        },
    )
    # A short secret's hash can be guessed: none is served, and no value is read.
    cited = [(e.memory_key, e.content_sha256) for e in _finding(out, "oversized_values").evidence]
    assert cited == [("pin", "")]
    assert not any(M_VALUE in sql for sql in ch.sqls)


@pytest.mark.asyncio
async def test_a_row_revised_between_the_two_reads_is_cited_without_a_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newer = {"agent": "writer", "user_id": "-", "session_id": "-", "memory_key": "a"}
    out, _ = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1, 1),
            M_OVER: _value_rows(1, 900, ["a"]),
            M_CITE: [_revision("a", 1)],
            M_VALUE: [{**newer, "version": 2, "value": '"latte"'}],
        },
    )
    # Version 1 is cited: version 2's value is not its hash, so none is given.
    [evidence] = _finding(out, "oversized_values").evidence
    assert (evidence.version, evidence.content_sha256) == (1, "")


@pytest.mark.asyncio
async def test_exact_key_samples_are_not_complete_when_the_ledger_summary_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = _hot([{"memory_key": "k", "reads": 30}])
    out, _ = await _run(
        monkeypatch, {M_LIVE: _live(1, 1), M_LEDGER: TimeoutError("slow"), M_HOT: hot}
    )
    assert "summary_ledger" in out.coverage.rules_skipped
    # Hot keys or none, a listing against a count the summary never read is not complete.
    assert _finding(out, "exact_key_store").samples_complete is False


@pytest.mark.asyncio
async def test_hot_key_completeness_compares_counts_of_one_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot = [{"memory_key": "lease", "reads": 40, "hot_reads": 40, "window_gets": 41}]
    # The summary saw fewer gets than the hot-key read: a later get must not make it whole.
    out, _ = await _run(monkeypatch, {M_LIVE: _live(1, 1), M_LEDGER: {"gets": 30}, M_HOT: hot})
    assert _finding(out, "exact_key_store").samples_complete is False


# A reminder, not enforcement: a threshold edit fails here, beside the version that must
# rise with it.
# code: (rule version, its built-in thresholds now, the thresholds at that version)
_THRESHOLDS = {
    "exact_key_store": (
        2,
        (na.EXACT_KEY_STORE_RATIO, na.EXACT_KEY_STORE_MIN_GETS, na.HOT_KEY_READS),
        (20.0, 200, 20),
    ),
    "hand_rolled_index_rows": (
        1,
        (na.INDEX_ROW_MIN_KEYS, na.INDEX_ROW_VALUE_CLIP, na._TOKEN_SPLIT_RE.pattern),
        (8, 65_536, r"""[\s"',\[\]{}()<>\\]+"""),
    ),
    "double_encoded_values": (1, (na.DOUBLE_ENCODED_CRITICAL_SHARE,), (0.25,)),
    "json_prefix_excerpts": (
        1,
        (
            na.JSON_PREFIX_WARNING_SHARE,
            na.EXCERPT_LIMIT,
            tuple(sorted(na.STRUCTURED_TYPES)),
            tuple(sorted(na.NON_SEARCHABLE_TYPES)),
        ),
        (0.10, 1200, ("procedural",), ("episodic", "profile")),
    ),
    "oversized_values": (
        1,
        (tuple(sorted(na.STRUCTURED_TYPES)), tuple(sorted(na.NON_SEARCHABLE_TYPES))),
        (("procedural",), ("episodic", "profile")),
    ),
    "unembedded_rows": (
        2,
        (tuple(sorted(na.NON_SEARCHABLE_TYPES)), na.EMBED_HOLDS),
        (
            ("episodic", "profile"),
            ("model call held", "MODEL_CALL_UNATTRIBUTED: the row names no org to bill"),
        ),
    ),
    "hash_keys": (
        1,
        (na.HASH_KEY_WARNING_SHARE, na._HASH_KEY_SEGMENT_RE),
        (0.25, r"(^|[/:])[0-9a-fA-F]{24,}$|(^|[/:])[0-9a-fA-F]{40,}([/:]|$)"),
    ),
    "coordination_as_memory": (
        2,
        (
            na.HOT_KEY_READS,
            na.COORDINATION_VALUE_CHARS,
            na.COORDINATION_MAX_WORDS,
            na.COORDINATION_SEGMENTS,
        ),
        (20, 160, 8, ("lease", "leases", "lock", "locks", "freeze", "freezes")),
    ),
    "episodic_blobs": (1, (na.EPISODIC_BLOB_CHARS,), (50_000,)),
    "version_churn": (
        2,
        (na.VERSION_CHURN_ROWS_RATIO, na.VERSION_CHURN_BYTES_RATIO),
        (3.0, 2.0),
    ),
    "index_backlog": (1, (na.INDEX_GRACE_SECONDS,), (600,)),
    "telemetry_unavailable": (1, (na.TELEMETRY_LAG_SECONDS,), (60,)),
}


def test_a_threshold_change_raises_its_rule_version() -> None:
    for code, (version, now, pinned) in _THRESHOLDS.items():
        assert na._CONTRACT[code].version == version, f"{code}: update the pin with the version"
        assert now == pinned, f"{code}: a threshold changed; raise its rule_version"


def test_a_token_narrowed_below_the_namespace_cannot_call_either_report() -> None:
    """Both reports read the whole namespace, identities included, so a token narrowed
    below it is refused before either runs."""
    assert not honours_read_scope("namespace.audit")
    assert not honours_read_scope("namespace.validate")


@pytest.mark.asyncio
async def test_inspector_calls_are_left_out_of_every_ledger_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, ch = await _run(
        monkeypatch,
        {
            M_LIVE: _live(1, 1),
            M_LEDGER: {"gets": 3, "searches": 1, "sets": 0, "inspector_calls": 9},
        },
    )
    assert out.summary.inspector_calls == 9 and out.summary.gets == 3
    ledger_sqls = [s for s in ch.sqls if "FROM loom.agent_events" in s]
    assert ledger_sqls
    for sql in ledger_sqls:
        assert "consumer NOT IN {inspectors:Array(String)}" in sql
    assert ch.params[0]["inspectors"] == ["loom-ui", "loom-assistant"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_ledger_summary_counts_a_dashboard_write_but_no_inspector_read() -> None:
    def call(operation: str, consumer: str) -> str:
        return (
            "INSERT INTO loom.agent_events (org, namespace, agent, operation, consumer, ts) "
            f"VALUES ('o', 'ns', 'bot', '{operation}', '{consumer}', now())"
        )

    rows = [
        call("memory.get", ""),
        call("memory.get", "loom-ui"),
        call("memory.search", "loom-assistant"),
        # A person editing a memory from the dashboard uses the namespace.
        call("memory.set", "loom-ui"),
    ]

    class _Local:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            # The ledger summary also reads which keys are procedures (loom.memory).
            return run(
                ["SET enable_quantized_codec = 1", ddl("memory"), ddl("agent_events"), *rows],
                sql,
                params,
            )

        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            found = await self.query_all(sql, params)
            return found[0] if found else None

    audit = na.new_audit(_inp(), ch=_Local())
    await audit.summary_ledger()
    s = audit.summary
    assert (s.gets, s.searches, s.sets, s.inspector_calls) == (1, 0, 1, 2)


# ---------------------------------------------------------------------------
# Missing telemetry, intentional exact-key reads and an indexing backlog are told apart
# from misuse.
# ---------------------------------------------------------------------------

M_BACKLOG = "AS stalled"


@pytest.mark.asyncio
async def test_writes_with_no_ledger_skip_the_ledger_checks_instead_of_passing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {**_live(10, 1), "written_in_window": 4}
    out, _ = await _run(monkeypatch, {M_LIVE: live, M_LEDGER: {"ledger_calls": 0}})
    gap = _finding(out, "telemetry_unavailable")
    assert gap.severity == "warning" and gap.kind == "system_state" and gap.count == 4
    codes = {f.code for f in out.findings}
    for code in ("exact_key_store", "intentional_exact_key_reads"):
        assert code not in codes and f"{code}: telemetry_unavailable" in out.skipped
    assert out.coverage.complete is False
    # coordination_as_memory still runs its key-segment arm, which reads no ledger.
    assert "coordination_as_memory" in codes

    # A ledger that has the window's calls: every check runs and the gap reads clean.
    out, _ = await _run(monkeypatch, {M_LIVE: live, M_LEDGER: {"ledger_calls": 9}})
    assert _finding(out, "telemetry_unavailable").count == 0
    assert "exact_key_store" in {f.code for f in out.findings}
    # A quiet namespace has nothing to count: no gap either.
    out, _ = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_LEDGER: {"ledger_calls": 0}})
    assert _finding(out, "telemetry_unavailable").severity == "info"


@pytest.mark.asyncio
async def test_reads_of_structured_rows_are_left_out_of_the_object_store_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = {"ledger_calls": 400, "gets": 300, "typed_gets": 250, "searches": 5, "sets": 0}
    out, ch = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_LEDGER: ledger})
    exact = _finding(out, "exact_key_store")
    assert exact.count == 50 and exact.severity != "critical"
    typed = _finding(out, "intentional_exact_key_reads")
    assert typed.count == 250 and typed.severity == "info" and typed.population == 300
    summary_sql = next(s for s in ch.sqls if M_LEDGER in s)
    hot_sql = next(s for s in ch.sqls if "AS hot_reads" in s)
    for sql in (summary_sql, hot_sql):
        assert "memory_type IN {structured:Array(String)}" in sql
    # Without the typed reads the same counts are an object store.
    out, _ = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_LEDGER: {**ledger, "typed_gets": 0}})
    assert _finding(out, "exact_key_store").severity == "critical"


@pytest.mark.asyncio
async def test_an_indexing_backlog_is_the_systems_state_and_warns_only_when_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [
        {"total": 2, "stalled": 0, "memory_key": "k", "memory_id": _mid("k"), "age_seconds": 30}
    ]
    out, ch = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_BACKLOG: pending})
    f = _finding(out, "index_backlog")
    assert (f.kind, f.severity, f.count) == ("system_state", "info", 2)
    assert "stalled" not in f.samples[0]
    assert ch.params[0]["grace"] == na.INDEX_GRACE_SECONDS
    stalled = [{**pending[0], "stalled": 1, "age_seconds": 4000}]
    out, _ = await _run(monkeypatch, {M_LIVE: _live(10, 1), M_BACKLOG: stalled})
    assert _finding(out, "index_backlog").severity == "warning"


# ---------------------------------------------------------------------------
# The new predicates on real rows (clickhouse-local).
# ---------------------------------------------------------------------------


class _LocalCH:
    """Runs every statement on clickhouse-local over the given tables and rows."""

    def __init__(self, tables: list[str], rows: list[str]) -> None:
        self.setup = ["SET enable_quantized_codec = 1", *[ddl(t) for t in tables], *rows]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        found = await self.query_all(sql, params)
        return found[0] if found else None


def _memory_row(
    key: str, *, minutes_ago: int, memory_type: str = "semantic", value: str = "v"
) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        f"memory_type, value, version, created_at, updated_at) VALUES ('o', 'ns', 'bot', '-', "
        f"'-', '{key}', '{memory_type}', '{value}', 1, now() - INTERVAL {minutes_ago} MINUTE, "
        f"now() - INTERVAL {minutes_ago} MINUTE)"
    )


def _ledger_row(operation: str, *, key: str = "", consumer: str = "") -> str:
    return (
        "INSERT INTO loom.agent_events (org, namespace, agent, operation, memory_key, consumer, ts) "
        f"VALUES ('o', 'ns', 'bot', '{operation}', '{key}', '{consumer}', now())"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_an_audit_run_twice_still_sees_writes_the_ledger_never_recorded() -> None:
    # The first audit's own row, a summary and a dashboard read are in the ledger: none is
    # a call that says the window was recorded.
    ch = _LocalCH(
        ["memory", "agent_events"],
        [
            _memory_row("team/fact", minutes_ago=10),
            _ledger_row("namespace.audit"),
            _ledger_row("namespace.summary"),
            _ledger_row("memory.search", consumer="loom-ui"),
        ],
    )
    audit = na.new_audit(_inp(window_seconds=3600), ch=ch)
    await na.run_checks(audit, ["telemetry_unavailable"])
    gap = _finding(audit, "telemetry_unavailable")
    assert gap.severity == "warning" and gap.count == 1
    # A write through any op (a procedure added) is a recorded call: the gap closes.
    ch.setup.append(_ledger_row("procedure.add", key="team/fact"))
    audit = na.new_audit(_inp(window_seconds=3600), ch=ch)
    await na.run_checks(audit, ["telemetry_unavailable"])
    assert _finding(audit, "telemetry_unavailable").count == 0


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_version_churn_counts_each_writers_distinct_contents() -> None:
    def version(user: str, key: str, version: int, sha: str) -> str:
        return (
            "INSERT INTO loom.memory_versions (org, namespace, agent, user_id, session_id, "
            f"memory_key, value, version, content_sha256) VALUES ('o', 'ns', 'bot', '{user}', "
            f"'-', '{key}', 'UTC', {version}, '{sha * 64}')"
        )

    rows = [
        # u1 wrote once, then two status changes copied the same value.
        version("u1", "pref/tz", 1, "a"),
        version("u1", "pref/tz", 2, "a"),
        version("u1", "pref/tz", 3, "a"),
        # u2 holds the same key with the same content: a version of its own.
        version("u2", "pref/tz", 1, "a"),
        # A status that flips back and forth is rewritten every time.
        *(version("u1", "job/state", v, "ab"[v % 2]) for v in range(1, 5)),
    ]
    audit = na.new_audit(_inp(), ch=_LocalCH(["memory_versions"], rows))
    await audit.version_churn()
    f = _finding(audit, "version_churn")
    assert f.count == 6
    assert {s["memory_key"]: s["versions"] for s in f.samples} == {"job/state": 4, "pref/tz": 2}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_hot_short_procedure_is_not_coordination_state() -> None:
    reads = [
        _ledger_row("memory.get", key=k) for k in ("runbook/restart", "lease/x") for _ in range(25)
    ]
    rows = [
        _memory_row("runbook/restart", minutes_ago=10, memory_type="procedural", value="drain"),
        _memory_row("lease/x", minutes_ago=10, value="held"),
        *reads,
    ]
    audit = na.new_audit(_inp(window_seconds=3600), ch=_LocalCH(["memory", "agent_events"], rows))
    await audit.coordination_as_memory()
    f = _finding(audit, "coordination_as_memory")
    assert [s["memory_key"] for s in f.samples] == ["lease/x"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_held_model_call_is_a_backlog_and_an_empty_row_is_neither() -> None:
    def stub(key: str, error: str) -> str:
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, embedding_model, embedding_dim, embedding, memory_key_embedding, "
            f"embed_last_error, status, version) SELECT 'o', 'ns', 'bot', '-', '-', '{key}', 'm', "
            f"0, v, v, '{error}', 'active', 1 FROM (SELECT arrayWithConstant(3072, toBFloat16(0)) AS v)"
        )

    rows = [
        _memory_row("held/row", minutes_ago=30),
        stub("held/row", na.EMBED_HOLDS[0]),
        _memory_row("failed/row", minutes_ago=30),
        stub("failed/row", "ValueError: input too long"),
        _memory_row("empty/row", minutes_ago=30, value=""),
    ]
    ch = _LocalCH(["memory", "memory_embeddings"], rows)
    audit = na.new_audit(_inp(), ch=ch)
    await audit.summary_live()
    await audit.index_backlog()
    await audit.unembedded_rows()
    backlog = _finding(audit, "index_backlog")
    assert [s["memory_key"] for s in backlog.samples] == ["held/row"]
    assert backlog.severity == "warning"  # older than the grace
    assert [s["memory_key"] for s in _finding(audit, "unembedded_rows").samples] == ["failed/row"]


def test_the_held_markers_are_the_embedding_workers() -> None:
    from server.llm.customer import NO_CUSTOMER
    from server.memory.workers.embedding import HELD

    assert na.EMBED_HOLDS == (HELD, NO_CUSTOMER)
