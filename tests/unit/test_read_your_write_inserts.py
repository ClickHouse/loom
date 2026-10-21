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

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.core.ch_writes import batched_insert_settings  # noqa: E402


def _insert_settings(path: Path, table: str) -> list[dict[str, int]]:
    """Every `settings=` passed to a `ch.insert` for `table`, in one module or in
    every module of a package directory: a dict literal, `batched_insert_settings()`
    resolved to its value, the session defaults for an insert that passes none, and
    `{"unresolved": 1}` for any other value."""
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    trees = [ast.parse(f.read_text()) for f in files]
    # A module-level name bound to the table, like `OUTBOX = "loom.memory_webhook_outbox"`.
    table_names = {
        target.id
        for tree in trees
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value == table
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }
    nodes = [n for tree in trees for n in ast.walk(tree)]
    found: list[dict[str, int]] = []
    for node in nodes:
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
        if not any(kw.arg == "settings" for kw in node.keywords):
            # The client's session defaults: a committed insert.
            found.append({"async_insert": 0, "wait_for_async_insert": 1})
        for kw in node.keywords:
            if kw.arg != "settings":
                continue
            value = kw.value
            if (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", "") == "batched_insert_settings"
            ):
                found.append(batched_insert_settings())
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
    ("server/memory/app/chunk_projection.py", "loom.memory_chunks"),
]


@pytest.mark.parametrize("rel_path,table", READ_PATH_TABLES)
def test_default_read_path_tables_are_written_synchronously(rel_path: str, table: str) -> None:
    settings_seen = _insert_settings(ROOT / rel_path, table)
    assert settings_seen, f"no `settings=` on any {table} insert in {rel_path}"
    for s in settings_seen:
        # The guarantee is "written before the INSERT returns":
        # `wait_for_async_insert = 1`, committed or batched.
        assert s.get("wait_for_async_insert") == 1 and s.get("async_insert") in (0, 1), (
            f"{table} in {rel_path} is written with {s}. This table is read on the "
            "default search path, so a fire-and-forget insert lets memory.set "
            "return 200 before the memory can be found. Keep wait_for_async_insert = 1."
        )


BATCHED_TABLES = [
    ("server/observability/audit.py", "loom.audit_log"),
    ("server/observability/events.py", "loom.agent_events"),
    ("server/memory/store/webhooks.py", "loom.memory_webhook_outbox"),
    ("server/memory/store/webhooks.py", "loom.memory_webhook_attempts"),
    ("server/memory/app/evidence.py", "loom.memory_evidence"),
    ("server/memory/app/evidence.py", "loom.memory_belief_sources"),
    ("server/core/namespace_write_marks.py", "loom.namespace_write_marks"),
    ("server/core/rmt_carry.py", "loom.carry_acks"),
    ("server/auth/attempts.py", "loom.auth_attempts"),
    ("server/memory/app/projection_failures.py", "loom.memory_projection_failures"),
    ("server/memory/store/usage.py", "loom.memory_usage_events"),
]


@pytest.mark.parametrize("rel_path,table", BATCHED_TABLES)
def test_tables_written_on_every_request_are_batched(rel_path: str, table: str) -> None:
    """A committed insert per request makes one part per row in these tables;
    batched, concurrent writers share parts and each still waits for its row."""
    settings_seen = _insert_settings(ROOT / rel_path, table)
    assert settings_seen, f"no `settings=` on any {table} insert in {rel_path}"
    for s in settings_seen:
        assert s == batched_insert_settings(), (
            f"{table} in {rel_path} is written with {s}; pass "
            "`settings=batched_insert_settings()` so its inserts share parts."
        )


def test_graph_projections_may_stay_async() -> None:
    """The counter-case, so the rule above reads as a decision and not a blanket
    ban: the entity graph is opt-in on read (`search_mode=graph`) and eventual
    there is fine, so those inserts intentionally stay async."""
    from server.memory.app.projections import graph_insert_settings

    for table in ("entity_edges", "entity_triples"):
        for wait in (False, True):
            assert graph_insert_settings(table, wait=wait, dedup_token="")["async_insert"] == 1
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
