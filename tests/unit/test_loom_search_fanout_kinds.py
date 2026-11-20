"""A namespace_scope=all_mine fan-out spans the memory-kind
namespaces; the MCP loom_search tool drops profile prose matched on
stopwords, knowledge hits under the floor, the duplicate conversation
list and the per-hit bookkeeping fields."""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

import server.auth.context as ctx
import server.auth.fanout as fanout_mod
import server.identity.app.namespace_kinds as nk
from server.memory.app.loom_search import fanout as loom_search_fanout
from server.transport import lean_results
from tests.unit.clock_fake import ManualClock

ORG = "clickhouse"
MINE = [
    "code-clickhouse",
    "jdoe",
    "code-loom",
    "tracker-clickhouse",
    "team-alerts",
    "legacy-ns",
]
KINDS = {
    "code-clickhouse": "code",
    "jdoe": "memory",
    "code-loom": "code",
    "tracker-clickhouse": "qa",
    "team-alerts": "memory",
    # legacy-ns has no directory row
}


def test_keep_kinds_keeps_memory_and_unknown_and_drops_code_and_tracker() -> None:
    out = nk.keep_kinds(MINE, KINDS, frozenset({"memory"}))
    assert out == ["jdoe", "team-alerts", "legacy-ns"], "order kept, unknown kind stays"
    assert nk.keep_kinds(MINE, {}, frozenset({"memory"})) == MINE, "no directory, no narrowing"


def test_fanout_kinds_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_search_fanout_kinds", "memory, bench")
    assert nk.fanout_kinds() == frozenset({"memory", "bench"})
    monkeypatch.setattr(settings_mod.settings, "memory_search_fanout_kinds", "")
    assert nk.fanout_kinds() == frozenset()


def test_resolver_narrows_my_namespaces_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mine(org, subject=None):
        return list(MINE)

    async def _kinds(org):
        return dict(KINDS)

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    monkeypatch.setattr(nk, "kind_map", _kinds)
    monkeypatch.setattr(nk, "fanout_kinds", lambda _=None: frozenset({"memory"}))
    assert list(asyncio.run(fanout_mod.resolve_fanout(ORG)).namespaces) == [
        "jdoe",
        "team-alerts",
        "legacy-ns",
    ]
    # An empty setting sweeps every namespace; an unreadable directory narrows nothing.
    monkeypatch.setattr(nk, "fanout_kinds", lambda _=None: frozenset())
    assert list(asyncio.run(fanout_mod.resolve_fanout(ORG)).namespaces) == MINE

    async def _no_dir(org):
        return {}

    monkeypatch.setattr(nk, "fanout_kinds", lambda _=None: frozenset({"memory"}))
    monkeypatch.setattr(nk, "kind_map", _no_dir)
    assert list(asyncio.run(fanout_mod.resolve_fanout(ORG)).namespaces) == MINE


def test_kind_map_fails_open_without_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as ch

    class _Never:
        _client = None

    monkeypatch.setattr(ch, "get_client", lambda: _Never())
    nk.reset_cache()
    assert asyncio.run(nk.kind_map(ORG)) == {}


class _Directory:
    """A client whose `query_all` answers `KINDS` until `fail` is set."""

    _client = object()

    def __init__(self) -> None:
        self.fail = False
        self.reads = 0

    async def query_all(self, sql: str, params: dict[str, str]) -> list[dict[str, str]]:
        self.reads += 1
        if self.fail:
            raise ConnectionError("ClickHouse went away")
        return [{"namespace": ns, "kind": kind} for ns, kind in KINDS.items()]


def test_a_failed_read_after_the_ttl_serves_the_last_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.ch as ch

    directory = _Directory()
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(ch, "get_client", lambda: directory)
    monkeypatch.setattr(nk, "_cache", {})
    monkeypatch.setattr(nk.time, "monotonic", clock.monotonic)
    assert asyncio.run(nk.kind_map(ORG)) == KINDS
    clock.advance(nk._TTL_S + 1)
    directory.fail = True
    assert asyncio.run(nk.kind_map(ORG)) == KINDS
    assert directory.reads == 2  # the TTL ran out, so the second call read and failed


def test_a_failed_read_with_no_snapshot_narrows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as ch

    directory = _Directory()
    directory.fail = True
    monkeypatch.setattr(ch, "get_client", lambda: directory)
    monkeypatch.setattr(nk, "_cache", {})
    assert asyncio.run(nk.kind_map(ORG)) == {}


def test_every_all_mine_leg_and_the_tool_call_the_fanout_resolver() -> None:
    from server.knowledge.app import conversations
    from server.knowledge.app import knowledge_search as knowledge
    from server.memory.app.loom_search import handler as loom_search_handler
    from server.transport import mcp_call
    from tests.unit._memory_search_pkg import handler_source

    sources = {"server.memory.app.search": handler_source()}
    sources.update(
        {
            m.__name__: inspect.getsource(m)
            for m in (conversations, knowledge, loom_search_handler, mcp_call)
        }
    )
    for name, src in sources.items():
        call = re.search(r"\b(resolve_fanout|namespaces_to_search)\(", src)
        assert call, name
        # The unnarrowed resolver is for listings and authz, not a search sweep.
        assert "await resolve_my_namespaces(" not in src[: call.start()], name


def test_profile_prose_does_not_match_on_stopwords() -> None:
    persona = "Walk me through your reasoning before showing code. Catch me when the user is wrong."
    q = "where does the user live, home city, residence, address"
    assert not loom_search_fanout.profile_hit_matches(q, "persona", persona)
    assert not loom_search_fanout.profile_hit_matches(
        q, "read-contract", "Every row is a claim the user wrote."
    )
    # A label match still counts, as does a real word in the value.
    assert loom_search_fanout.profile_hit_matches(q, "home", "")
    assert loom_search_fanout.profile_hit_matches(q, "about", "Lives at a residence in Ujjain.")
    assert loom_search_fanout.profile_hit_matches("persona", "persona", "")
    assert not loom_search_fanout.profile_hit_matches("", "persona", "anything")


def test_knowledge_floor_drops_the_far_hits_and_keeps_the_rest() -> None:
    know = {
        "results": [
            {"source_uri": "a", "similarity": 0.24},
            {"source_uri": "b", "similarity": 0.31},
            {"source_uri": "c"},  # no similarity: stays
        ],
        "embedding_model": "m",
    }
    out = loom_search_fanout.knowledge_above_floor(know, 0.30)
    assert [h["source_uri"] for h in out["results"]] == ["b", "c"]
    assert out["embedding_model"] == "m"
    assert loom_search_fanout.knowledge_above_floor(know, 0.0) is know, "no floor, no copy"


def test_lean_drops_the_per_hit_bookkeeping() -> None:
    hit = {
        "memory_key": "where_i_live",
        "content_excerpt": '"ujjain"',
        "rank_score": 0.39,
        "similarity": 0.41,
        "temporal_confidence": 0,
        "body_truncated": False,
        "verified": False,
        "about_other": False,
        "session_id": "-",
        "created_at": "2026-09-19 08:53:41",
        "namespace": "jdoe",
        "status": "active",
        "version": 1789808021781527600,
    }
    out = lean_results.lean({"memory": {"results": [hit]}})["memory"]["results"][0]
    for gone in ("temporal_confidence", "body_truncated", "verified", "about_other", "session_id"):
        assert gone not in out, gone
    for kept in (
        "memory_key",
        "content_excerpt",
        "rank_score",
        "created_at",
        "namespace",
        "version",
    ):
        assert kept in out, kept
    know = {
        "source_uri": "u",
        "similarity": 0.5,
        "lexical_score": 10.0,
        "span_start": 0,
        "span_end": 0,
        "span_similarity": 0,
        "siblings_folded": 0,
        "chunk_idx": 3,
        "section_path": ["a", "b"],
        "section": "a > b",
    }
    out = lean_results.lean(know)
    assert set(out) == {"source_uri", "similarity", "section"}


def test_conversations_section_carries_one_list() -> None:
    src = inspect.getsource(loom_search_fanout.search_sections)
    assert 'conv_dump.pop("hits", None)' in src


def test_a_fail_closed_kind_read_refuses_without_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mint-time check must not pass on the configured kinds alone; a sweep still may."""
    import server.core.ch as chmod
    from server.core.errors import BackendUnavailableError

    class _Unconnected:
        _client = None

    nk.reset_cache()
    monkeypatch.setattr(chmod, "get_client", lambda: _Unconnected())
    with pytest.raises(BackendUnavailableError):
        asyncio.run(nk.kind_map("o", fail_closed=True))
    assert isinstance(asyncio.run(nk.kind_map("o")), dict)
    nk.reset_cache()
