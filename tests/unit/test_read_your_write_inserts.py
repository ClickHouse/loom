"""Writes on the default read path must be durable before the request returns.

`memory.set` returning 200 has to mean "you can find this now". Three tables
carry that promise — `loom.memory` (the row itself), `loom.memory_embeddings`
(what `memory.search` / `memory.timeline` / `knowledge.search` actually read)
and `loom.memory_chunks` (read on the default bm25_hybrid path).

`memory_embeddings` and `memory_chunks` were both written with
`async_insert=1, wait_for_async_insert=0`, so visibility was at ClickHouse's
discretion. That is what made `test_timeline_returns_entries_in_temporal_order`
fail roughly one CI run in three: a 45s poll for rows whose flush nobody had
asked for.

These tests are deliberately about the SETTINGS rather than behaviour, because
the failure mode is a plausible-looking future "optimization" — async insert is
faster in isolation, and the cost only shows up as an intermittent test and an
occasional agent that cannot find what it just wrote.

The tables written on every request (audit rows, event flushes, ledgers) are
batched instead: one committed insert per request makes one part per row there.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.core.ch_writes import (  # noqa: E402
    async_insert_settings,
    batched_insert_settings,
    committed_insert_settings,
)
from server.settings import settings  # noqa: E402


def _insert_settings(path: Path, table: str) -> list[dict[str, int]]:
    """Every `settings=` passed to a `ch.insert` for `table`, in one module or in
    every module of a package directory: a dict literal, `committed_insert_settings()`,
    `batched_insert_settings()` resolved to its value under the current switch and
    `async_insert_settings()` (a `background=` that is not a constant reads as False),
    the session defaults for an insert that passes none, and `{"unresolved": 1}` for any
    other value."""
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    found: list[dict[str, int]] = []
    for f in files:
        tree = ast.parse(f.read_text())
        found.extend(_module_insert_settings(tree, table))
    return found


def _module_insert_settings(tree: ast.Module, table: str) -> list[dict[str, int]]:
    # A module-level name bound to the table, like `OUTBOX = "loom.memory_webhook_outbox"`,
    # matched only within its own module.
    table_names = {
        target.id
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value == table
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }
    found: list[dict[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "insert"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        names_table = (isinstance(first, ast.Constant) and first.value == table) or (
            isinstance(first, ast.Name) and first.id in table_names
        )
        if not names_table:
            continue
        if len(node.args) > 2 or any(kw.arg is None for kw in node.keywords):
            # Settings passed by position or through **kw: unknown here, so it fails.
            found.append({"unresolved": 1})
        elif not any(kw.arg == "settings" for kw in node.keywords):
            # The client's session defaults: a committed insert.
            found.append({"async_insert": 0, "wait_for_async_insert": 1})
        for kw in node.keywords:
            if kw.arg != "settings":
                continue
            value = kw.value
            name = getattr(value.func, "id", "") if isinstance(value, ast.Call) else ""
            if name == "batched_insert_settings":
                found.append(batched_insert_settings())
                continue
            if name == "async_insert_settings":
                background = any(
                    k.arg == "background" and isinstance(k.value, ast.Constant) and k.value.value
                    for k in value.keywords
                )
                found.append(async_insert_settings(background=background))
                continue
            if name == "committed_insert_settings":
                found.append(committed_insert_settings())
                continue
            if not isinstance(value, ast.Dict):
                # A name or another call: unknown here, so it fails every check.
                found.append({"unresolved": 1})
                continue
            d: dict[str, int] = {}
            for k, v in zip(value.keys, value.values, strict=True):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    d[str(k.value)] = int(v.value)
            found.append(d)
    return found


READ_PATH_TABLES = [
    ("server/memory/store", "loom.memory_embeddings"),
    ("server/memory/store", "loom.memory"),
]


@pytest.mark.parametrize("rel_path,table", READ_PATH_TABLES)
def test_default_read_path_tables_are_written_synchronously(rel_path: str, table: str) -> None:
    settings_seen = _insert_settings(ROOT / rel_path, table)
    assert settings_seen, f"no `settings=` on any {table} insert in {rel_path}"
    for s in settings_seen:
        # The guarantee is "written before the INSERT returns":
        # `wait_for_async_insert = 1`, committed or batched.
        # `loom.memory_chunks` is also on this path; COMMITTED_TABLES pins it.
        assert s.get("wait_for_async_insert") == 1 and s.get("async_insert") in (0, 1), (
            f"{table} in {rel_path} is written with {s}. This table is read on the "
            "default search path, so a fire-and-forget insert lets memory.set "
            "return 200 before the memory can be found. Keep wait_for_async_insert = 1."
        )


# (module, table, window). "switched": batched_insert_settings(), committed with the switch
# off. The others are waited async inserts whether or not batching is on
# (async_insert_settings): "request" takes the short window, "either" is written with
# both (a request's flush and the timer's).
BATCHED_TABLES = [
    ("server/observability/audit.py", "loom.audit_log", "switched"),
    ("server/observability/events.py", "loom.agent_events", "switched"),
    ("server/memory/store/webhooks.py", "loom.memory_webhook_outbox", "switched"),
    ("server/memory/app/evidence.py", "loom.memory_evidence", "switched"),
    ("server/memory/app/evidence.py", "loom.memory_belief_sources", "switched"),
    ("server/core/rmt_carry.py", "loom.carry_acks", "request"),
    ("server/auth/attempts.py", "loom.auth_attempts", "request"),
    ("server/memory/app/projection_failures.py", "loom.memory_projection_failures", "request"),
    ("server/memory/store/usage.py", "loom.memory_usage_events", "either"),
]


@pytest.mark.parametrize("switch", [True, False])
@pytest.mark.parametrize("rel_path,table,window", BATCHED_TABLES)
def test_tables_written_on_every_request_are_batched(
    monkeypatch: pytest.MonkeyPatch, rel_path: str, table: str, window: str, switch: bool
) -> None:
    """A committed insert per request makes one part per row in these tables;
    batched, concurrent writers share parts and each still waits for its row. A
    request-path insert keeps the short window its op budget assumes, and with the
    switch off a request-path table is committed and a ledger flush stays a waited
    async insert on the same bounds."""
    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    settings_seen = _insert_settings(ROOT / rel_path, table)
    assert settings_seen, f"no `settings=` on any {table} insert in {rel_path}"
    if window == "switched":
        allowed = [batched_insert_settings()]
        expected = "batched_insert_settings()"
    elif window == "request":
        allowed = [async_insert_settings()]
        expected = "async_insert_settings()"
    else:
        allowed = [async_insert_settings(), async_insert_settings(background=True)]
        expected = "async_insert_settings(background=...)"
    for s in settings_seen:
        assert s in allowed, (
            f"{table} in {rel_path} is written with {s}; pass `settings={expected}`."
        )


class _WriteMarksCH:
    def __init__(self) -> None:
        self.settings: list[dict] = []

    async def insert(self, table, rows, settings=None, **kw):
        self.settings.append(settings)

    async def execute(self, sql, params=None, **kw):
        return None


@pytest.mark.parametrize("switch", [True, False])
def test_write_marks_take_the_window_of_who_waits(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    """The timer's flush is a waited async insert on the background window whether
    or not batching is on; the erasure's bump and the process-start mark write one
    row each, so they stay committed."""
    import asyncio

    from server.core import namespace_write_marks as marks

    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    marks._reset_for_tests()
    try:
        bump_ch, flush_ch, start_ch = _WriteMarksCH(), _WriteMarksCH(), _WriteMarksCH()
        asyncio.run(marks.bump("o", "a", "memory", ch=bump_ch))
        marks.touch("o", "a", "memory")
        asyncio.run(marks.flush_now(ch=flush_ch))
        asyncio.run(marks.mark_process_start(ch=start_ch))
    finally:
        marks._reset_for_tests()
    assert bump_ch.settings == [committed_insert_settings()]
    assert flush_ch.settings == [async_insert_settings(background=True)]
    assert start_ch.settings == [committed_insert_settings()]


class _RecordingCH:
    def __init__(self) -> None:
        self.settings: list[dict] = []

    async def insert(self, table, rows, *, settings=None):
        self.settings.append(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [True, False])
async def test_usage_flush_a_request_waits_on_takes_the_short_window(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    """The event that fills the buffer flushes it inside the request, whose op
    budget assumes the short window."""
    from server.memory.store import usage

    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    monkeypatch.setattr(usage, "_ensure_usage_flusher_started", lambda: None)
    usage._reset_for_tests()
    usage._USAGE_BUFFER.extend({"memory_id": "m"} for _ in range(usage._USAGE_BUFFER_MAX - 1))
    ch = _RecordingCH()
    try:
        await usage.record_usage_event(
            memory_id="m", org="o", namespace="n", event_type="recall", consumer="", ch=ch
        )
    finally:
        usage._reset_for_tests()
    assert ch.settings == [async_insert_settings()]


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [True, False])
async def test_usage_flush_of_the_timer_takes_the_background_window(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    """The flush loop's tick waits on nobody, so it takes the background window."""
    import asyncio

    from server.memory.store import usage

    async def _one_tick(_seconds: float) -> None:
        if ch.settings:
            raise asyncio.CancelledError

    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    ch = _RecordingCH()
    monkeypatch.setattr(usage, "_default_client", lambda: ch)
    # Only the flusher's sleep is replaced, not the event loop's.
    monkeypatch.setattr(usage, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": _one_tick}))
    usage._reset_for_tests()
    usage._USAGE_BUFFER.append({"memory_id": "m"})
    try:
        with pytest.raises(asyncio.CancelledError):
            await usage._usage_flush_loop()
    finally:
        usage._reset_for_tests()
    assert ch.settings == [async_insert_settings(background=True)]


# Every insert of these tables anywhere under server/, not one module per table.
COMMITTED_TABLES = [
    # A Quantized-coded vector: the codec refuses a row of another width (code 190).
    ("server", "loom.memory_chunks"),
    ("server", "loom.entity_vectors"),
    # PARTITION BY org: a flush across too many orgs exceeds max_partitions_per_insert_block.
    ("server", "loom.entity_canonical_map"),
    # One worker writes it twice a pass, in sequence: batching would save no parts.
    ("server/memory/store/webhooks.py", "loom.memory_webhook_attempts"),
]


@pytest.mark.parametrize("rel_path,table", COMMITTED_TABLES)
def test_tables_that_fail_a_whole_flush_stay_committed(rel_path: str, table: str) -> None:
    """These tables stay committed. For most, a block-level error in a shared flush
    fails every insert waiting on it, so one tenant's bad row or org count would fail
    another tenant's write."""
    settings_seen = _insert_settings(ROOT / rel_path, table)
    assert settings_seen, f"no insert of {table} in {rel_path}"
    for s in settings_seen:
        assert s == {"async_insert": 0, "wait_for_async_insert": 1}, (
            f"{table} in {rel_path} is written with {s}. Keep it committed: a "
            "block-level error in a shared flush fails every insert waiting on it, "
            "including other tenants' inserts."
        )


@pytest.mark.parametrize("switch", [True, False])
def test_graph_projections_may_stay_async(monkeypatch: pytest.MonkeyPatch, switch: bool) -> None:
    """The counter-case, so the rule above reads as a decision and not a blanket
    ban: the entity graph is opt-in on read (`search_mode=graph`) and eventual
    there is fine, so those inserts intentionally stay async."""
    from server.memory.app.projections import graph_insert_settings

    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    for table in ("entity_edges", "entity_triples"):
        for wait in (False, True):
            assert graph_insert_settings(table, wait=wait, dedup_token="")["async_insert"] == 1
        # A waited graph insert uses the live wait bounds whatever the switch says.
        assert graph_insert_settings(table, wait=True, dedup_token="") == async_insert_settings()
    # The projection repair's replay is the one synchronous, token-deduplicated form.
    assert graph_insert_settings("entity_edges", wait=False, dedup_token="t")["async_insert"] == 0


def test_judged_relevance_carries_its_scoring_scheme() -> None:
    """Two incompatible populations already share this metric: the original
    anchored prompt collapsed onto {0.0, 0.75} across 2,963 judgements, and the
    current scheme derives relevance from `answered` + useful-excerpt fraction.
    Averaging them is meaningless, so new points are labelled and the old ones
    stay selectable as `scheme = ''`."""
    from server.observability import metrics as m
    from server.observability.retrieval_judge import JUDGE_SCHEME

    recorded: list[tuple[float, dict]] = []

    class _H:
        def record(self, v, attributes=None):
            recorded.append((v, dict(attributes or {})))

    class _C:
        def add(self, v, attributes=None):
            recorded.append((v, dict(attributes or {})))

    prev_h, prev_c = m._search_judged_relevance, m._search_judge_verdict
    m._search_judged_relevance, m._search_judge_verdict = _H(), _C()
    try:
        m.record_judged_relevance(
            relevance=0.76, verdict="answers", namespace="ns", scheme=JUDGE_SCHEME
        )
    finally:
        m._search_judged_relevance, m._search_judge_verdict = prev_h, prev_c

    assert recorded, "nothing recorded"
    assert all(a.get("scheme") == JUDGE_SCHEME for _v, a in recorded)
    # Bounded constant, not model output — the cardinality rule still holds.
    assert len(JUDGE_SCHEME) <= 40


# --- judge review payload: consent-gated disclosure ----------------------------


@pytest.mark.parametrize(
    "level,want_query,want_excerpts",
    [("all", True, True), ("query", True, False), ("off", False, False)],
)
@pytest.mark.asyncio
async def test_review_payload_follows_namespace_consent(
    monkeypatch, level, want_query, want_excerpts
) -> None:
    """The annotation queue needs to show a reviewer what the judge saw, but that
    is user query text and memory content going to a third party — so it rides
    the SAME per-namespace consent as generation content, by the same rules."""
    from server.observability import retrieval_judge as rj

    async def _fake_resolve(org, namespace):
        return level

    monkeypatch.setattr("server.observability.capture_consent.resolve", _fake_resolve)
    got = await rj._review_payload("what is my dog's name", ["a beagle", "b"], "o", "n")
    assert ("review_query" in got) is want_query
    assert ("review_excerpts" in got) is want_excerpts


@pytest.mark.asyncio
async def test_review_payload_fails_closed(monkeypatch) -> None:
    """If consent cannot be resolved, disclose nothing — the consent module fails
    closed and this must not undo that."""
    from server.observability import retrieval_judge as rj

    async def _boom(org, namespace):
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr("server.observability.capture_consent.resolve", _boom)
    assert await rj._review_payload("q", ["e"], "o", "n") == {}


@pytest.mark.asyncio
async def test_review_payload_is_bounded(monkeypatch) -> None:
    """Bounded on both axes: an 80KB memory must not become an 80KB score."""
    from server.observability import retrieval_judge as rj

    async def _all(org, namespace):
        return "all"

    monkeypatch.setattr("server.observability.capture_consent.resolve", _all)
    got = await rj._review_payload("q" * 5000, ["e" * 5000] * 20, "o", "n")
    assert len(got["review_query"]) == rj._REVIEW_QUERY_CHARS
    assert len(got["review_excerpts"]) == rj._REVIEW_EXCERPTS
    assert all(len(e) == rj._REVIEW_EXCERPT_CHARS for e in got["review_excerpts"])
