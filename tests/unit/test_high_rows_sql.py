"""The statements that keep 'high' rows home, run on clickhouse-local against the real
tables: high_keys finds a key with any high version, overwritten since or under an id since
re-created (loom.memory_versions keeps every version; loom.memory merges them); a `contains`
search skips a high row's value; and the usage overlay reads the epoch non-recall events
write as no recall."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store.reads import CONTAINS_PREDICATE
from server.memory.store.rows import MemoryRow
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_A, _B, _C, _D, _E, _F, _G, _H = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abcdef98")


class _LocalCH:
    """Runs each query on clickhouse-local after the setup statements."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = run(self.setup, sql, params)
        return rows[0] if rows else None


def _version(mid: str, key: str, version: int, sensitivity: str) -> str:
    return (
        "INSERT INTO loom.memory_versions (org, namespace, agent, memory_id, memory_key, "
        f"version, sensitivity) VALUES ('o', 'n', 'a', '{mid}', '{key}', {version}, "
        f"'{sensitivity}')"
    )


@pytest.mark.asyncio
async def test_high_keys_finds_any_high_version_of_a_key() -> None:
    ch = _LocalCH(
        ddl("memory_versions"),
        # overwritten: one id, high then low
        _version(_A, "overwritten", 1, "high"),
        _version(_A, "overwritten", 2, "low"),
        # deleted and written again: a new id the second time
        _version(_B, "recreated", 1, "high"),
        _version(_C, "recreated", 2, ""),
        _version(_D, "plain", 1, ""),
        # a merge keeps every version: `version` is in the sort key
        "OPTIMIZE TABLE loom.memory_versions FINAL",
    )
    got = await memory_repo.high_keys(
        "o", "n", ["overwritten", "recreated", "plain", "absent"], ch=ch
    )
    assert got == {"overwritten", "recreated"}


@pytest.mark.asyncio
async def test_a_contains_search_matches_a_high_row_by_key_only() -> None:
    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, memory_id, memory_key, value, "
        f"sensitivity) VALUES ('o', 'n', 'a', '{_A}', 'door', 'code 4412', 'high'), "
        f"('o', 'n', 'a', '{_B}', 'note', 'code 4412 is old', '')",
    )
    sql = f"SELECT memory_key FROM loom.memory WHERE {CONTAINS_PREDICATE} ORDER BY memory_key"
    by_value = await ch.query_all(sql, {"contains": "4412"})
    by_key = await ch.query_all(sql, {"contains": "door"})
    assert [r["memory_key"] for r in by_value] == ["note"]
    assert [r["memory_key"] for r in by_key] == ["door"]


@pytest.mark.asyncio
async def test_the_usage_overlay_reads_the_epoch_as_no_recall() -> None:
    ch = _LocalCH(
        ddl("memory_usage_totals"),
        "INSERT INTO loom.memory_usage_totals (memory_id, org, namespace, evidence_count, "
        f"recall_count, last_recalled_at) VALUES ('{_A}', 'o', 'n', 2, 0, 0)",
    )
    row = MemoryRow(
        memory_id=_A,
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 19),
        expires_at=None,
    )
    [got] = await memory_repo.overlay_usage_totals_many([row], ch=ch)
    assert got.evidence_count == 2 and got.last_recalled_at is None


@pytest.mark.asyncio
async def test_aggregate_never_groups_or_filters_by_a_high_value(monkeypatch) -> None:
    from server.memory.api.models_search import MemoryAggregateInput
    from server.memory.app import query

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, memory_id, memory_key, value, "
        "sensitivity) VALUES "
        f"('o', 'n', 'a', '-', '{_A}', 'vault', '{{\"pw\": \"hunter2\"}}', 'high'), "
        f"('o', 'n', 'a', '-', '{_B}', 'note', '{{\"pw\": \"public\"}}', '')",
    )
    monkeypatch.setattr(query, "get_client", lambda: ch)
    grouped = await query.handle_memory_aggregate(
        MemoryAggregateInput(org="o", namespace="n", agent="a", group_by=["pw"])
    )
    assert [r.group["pw"] for r in grouped.rows] == ["public"]
    guessed = await query.handle_memory_aggregate(
        MemoryAggregateInput(org="o", namespace="n", agent="a", filter={"pw": "hunter2"})
    )
    assert [r.metric for r in guessed.rows] == [0.0]


def _memory(*rows: str) -> _LocalCH:
    """loom.memory with `rows`, each the VALUES tuple for the columns below."""
    cols = (
        "org, namespace, agent, user_id, session_id, memory_id, memory_key, memory_type, "
        "value, status, sensitivity, version, conflict_with"
    )
    return _LocalCH(ddl("memory"), f"INSERT INTO loom.memory ({cols}) VALUES " + ", ".join(rows))


def _row(
    mid: str,
    key: str,
    value: str,
    *,
    status: str = "active",
    sensitivity: str = "",
    kind: str = "semantic",
    peers: str = "[]",
    user: str = "-",
) -> str:
    return (
        f"('o', 'n', 'a', '{user}', '-', '{mid}', '{key}', '{kind}', '{value}', '{status}', "
        f"'{sensitivity}', 1, {peers})"
    )


@pytest.mark.asyncio
async def test_the_conflicts_queue_lists_a_high_row_without_its_value() -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import conflicts

    ch = _memory(
        _row(_A, "vault", "code 4412", status="conflicted", sensitivity="high"),
        _row(_B, "note", "plain words", status="conflicted"),
    )
    rows = await conflicts.list_conflicted("o", "n", scope=WHOLE_NAMESPACE, ch=ch)
    assert {r["memory_key"]: r["value"] for r in rows} == {"vault": None, "note": "plain words"}


@pytest.mark.asyncio
async def test_a_revision_proposal_never_shows_a_high_peer(monkeypatch) -> None:
    from server.memory.api.models_conflicts import ConflictsProposeRevisionInput
    from server.memory.app import conflicts_ops

    ch = _memory(
        _row(_A, "note", "plain words", status="conflicted", peers=f"['{_B}']"),
        _row(_B, "vault", "code 4412", status="conflicted", sensitivity="high"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.conflicts._default_client", lambda: ch)
    out = await conflicts_ops.handle_conflicts_propose_revision(
        ConflictsProposeRevisionInput(org="o", namespace="n", memory_id=_A)
    )
    assert [m.memory_key for m in out.memories] == ["note"]


@pytest.mark.asyncio
async def test_profile_render_leaves_a_high_section_out(monkeypatch) -> None:
    from server.memory.api.models_profile import ProfileRenderInput
    from server.memory.app import profile

    ch = _memory(
        _row(_A, "persona", "likes tea", kind="profile"),
        _row(_B, "door", "code 4412", kind="profile", sensitivity="high"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await profile.handle_profile_render(ProfileRenderInput(org="o", namespace="n", agent="a"))
    assert "likes tea" in out.rendered and "4412" not in out.rendered


@pytest.mark.asyncio
async def test_the_ask_bridge_hop_never_answers_from_a_high_row(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.memory.app.ask import completion

    ch = _memory(
        _row(_A, "acme_corp_founded", "1999"),
        _row(_B, "acme_corp_vault_code", "4412", sensitivity="high"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    hits = await completion.bridge_rows_by_key(
        SimpleNamespace(org="o", namespace="n", agent="*", user_id="-", session_id=None),
        "Acme Corp",
    )
    assert [h.memory_key for h in hits] == ["acme_corp_founded"]


@pytest.mark.asyncio
async def test_a_proposal_lists_a_high_row_without_its_preview(monkeypatch) -> None:
    from server.memory.api.models_proposals import MemoryProposalsInput
    from server.memory.app import proposals, proposals_ops

    ch = _memory(
        _row(_A, "trial/tea", "likes tea", status="proposed"),
        _row(_B, "trial/door", "code 4412", status="proposed", sensitivity="high"),
    )

    async def _policy(*_a: Any, **_k: Any) -> Any:
        from server.memory.domain.proposal_policy import ProposalPolicy

        return ProposalPolicy()

    async def _empty(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(proposals_ops, "get_client", lambda: ch)
    monkeypatch.setattr(proposals, "policy_for", _policy)
    monkeypatch.setattr(proposals, "graded_counts", _empty)
    monkeypatch.setattr(proposals_ops, "_usage_counts", _empty)
    out = await proposals_ops.handle_memory_proposals(MemoryProposalsInput(org="o", namespace="n"))
    previews = {p.memory_key: p.value_preview for p in out.proposals}
    assert previews == {"trial/tea": "likes tea", "trial/door": ""}


@pytest.mark.asyncio
async def test_ask_never_masks_with_a_high_rows_value(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.memory.app.ask import supersession

    ch = _memory(
        _row(_A, "door", "code 4412", status="superseded", sensitivity="high"),
        _row(_B, "tea", "likes tea", status="superseded"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    rows = await supersession.superseded_rows_for_pool(
        SimpleNamespace(org="o", namespace="n", agent="*", user_id="-", session_id=None),
        [
            SimpleNamespace(memory_key="door", source="fact"),
            SimpleNamespace(memory_key="tea", source="fact"),
            SimpleNamespace(memory_key="chunk:s:1", source="chunk"),  # a chunk asks for masks
        ],
    )
    assert [r.memory_key for r in rows] == ["tea"]


@pytest.mark.asyncio
async def test_namespace_validate_never_quotes_a_high_row() -> None:
    from server.identity.app import namespace_validate as nv

    ch = _memory(
        _row(_A, "trip", '"we flew there last week"'),
        _row(_B, "door", '"the code changed last week: 4412"', sensitivity="high"),
    )
    _severity, _hint, sql = nv._CHECKS["unanchored_time_cue"]
    rows = await ch.query_all(
        sql, {"org": "o", "ns": "n", "keys": [], "cue": nv._TIME_CUE_RE, "lim": 10}
    )
    assert [r["memory_key"] for r in rows] == ["trip"]


@pytest.mark.asyncio
async def test_a_revision_proposal_for_a_high_or_unseen_target_is_not_found(monkeypatch) -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.api.models_conflicts import ConflictsProposeRevisionInput
    from server.memory.app import conflicts_ops

    ch = _memory(
        _row(_A, "vault", "code 4412", status="conflicted", sensitivity="high", peers=f"['{_B}']"),
        _row(_B, "note", "plain words", status="conflicted", user="u"),
        _row(_C, "theirs", "their words", status="conflicted", user="other", peers=f"['{_B}']"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.conflicts._default_client", lambda: ch)
    high = await conflicts_ops.handle_conflicts_propose_revision(
        ConflictsProposeRevisionInput(org="o", namespace="n", memory_id=_A)
    )
    assert high.found is False  # never a peer presented as the target
    own_user = ReadFilters(scope="own_user", user_id="u", session_id=None)
    monkeypatch.setattr(conflicts_ops, "request_scope", lambda: own_user)
    unseen = await conflicts_ops.handle_conflicts_propose_revision(
        ConflictsProposeRevisionInput(org="o", namespace="n", memory_id=_C)
    )
    assert unseen.found is False  # another user's id is not confirmed to exist


@pytest.mark.asyncio
async def test_namespace_validate_still_resolves_links_to_a_high_row() -> None:
    from server.identity.app import namespace_validate as nv

    ch = _LocalCH(
        ddl("memory"),
        "SET enable_quantized_codec = 1",  # the embeddings table's codec
        ddl("memory_embeddings"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, superseded_by, created_at) "
        f"VALUES ('o', 'n', 'a', '-', '-', '{_A}', 'old', 'semantic', 'v1', 'superseded', '', 1, "
        f"'{_B}', now() - 3600), ('o', 'n', 'a', '-', '-', '{_B}', 'new', 'semantic', 'v2', "
        "'active', 'high', 1, '', now() - 3600)",
    )
    params = {"org": "o", "ns": "n", "keys": [], "lim": 10, "grace": 60}
    _s, _h, dangling = nv._CHECKS["superseded_dangling"]
    _s, _h, unembedded = nv._CHECKS["unembedded"]
    assert await ch.query_all(dangling, params) == []  # its high successor is live
    # a high row is never embedded, so it is not reported as missing its vector
    assert "new" not in [r["memory_key"] for r in await ch.query_all(unembedded, params)]


@pytest.mark.asyncio
async def test_search_never_seats_a_high_canonical(monkeypatch) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.app import served_duplicates

    ch = _memory(_row(_A, "door", "code 4412", sensitivity="high"), _row(_B, "tea", "likes tea"))
    monkeypatch.setattr("server.memory.store.dedup._default_client", lambda: ch)
    seated = await served_duplicates._canonicals("o", ["n"], [_A, _B], WHOLE_NAMESPACE)
    assert [row.memory_key for row in seated.values()] == ["tea"]


@pytest.mark.asyncio
async def test_the_profile_fold_never_reads_a_high_row(monkeypatch) -> None:
    from server.memory.workers import profile_consolidation as fold

    ch = _memory(
        _row(_A, "user_tea", "likes tea", kind="preference"),
        _row(_B, "user_health", "a private diagnosis", kind="preference", sensitivity="high"),
    )
    read: list[Any] = []

    def _render(rows: list[Any]) -> str:
        read.extend(rows)
        return ""  # nothing to fold: the worker stops before the LLM

    monkeypatch.setattr(fold, "render_fold_input", _render)
    cfg = fold.ProfileConsolidationConfig()
    await fold._consolidate_identity(ch, {"org": "o", "namespace": "n", "user_id": "-"}, cfg)
    assert [r["value"] for r in read] == ["likes tea"]


@pytest.mark.asyncio
async def test_the_entity_extractor_never_reads_a_high_row() -> None:
    from server.memory.store import entities

    ch = _memory(_row(_A, "tea", "likes tea"), _row(_B, "door", "code 4412", sensitivity="high"))
    keys = await entities.find_unentitied_keys(batch_size=10, ch=ch)
    assert [k["memory_key"] for k in keys] == ["tea"]
    every_key = [dict(k, memory_key=name) for k in keys for name in ("tea", "door")]
    rows = await entities.read_unentitied_rows(every_key, ch=ch)
    assert [r["memory_key"] for r in rows] == ["tea"]


@pytest.mark.asyncio
async def test_namespace_validate_never_quotes_a_high_rows_evidence() -> None:
    from server.identity.app import namespace_validate as nv

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, grounding, evidence) "
        f"VALUES ('o', 'n', 'a', '-', '-', '{_A}', 'trip', 'semantic', 'v', 'active', '', 1, "
        f"'unresolved', 'we flew'), ('o', 'n', 'a', '-', '-', '{_B}', 'door', 'semantic', 'v', "
        "'active', 'high', 1, 'unresolved', 'the code is 4412')",
    )
    _s, _h, sql = nv._CHECKS["unresolved_evidence"]
    rows = await ch.query_all(sql, {"org": "o", "ns": "n", "keys": [], "lim": 10})
    assert [r["memory_key"] for r in rows] == ["trip"]


@pytest.mark.asyncio
async def test_the_graph_walk_never_seeds_from_a_high_rows_entities() -> None:
    from server.memory.app import recent_seeds

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, entities) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'tea', 'semantic', 'v', 'active', '', 1, ['oolong']), "
        f"('o', 'n', 'a', '-', '-', '{_B}', 'door', 'semantic', 'v', 'active', 'high', 1, ['4412'])",
    )
    sql, params = recent_seeds.build_query(
        org="o", namespace="n", agent=None, user_id=None, session_id=None, memories=10, limit=10
    )
    rows = await ch.query_all(sql, params)
    assert [r["entity"] for r in rows] == ["oolong"]


@pytest.mark.asyncio
async def test_related_never_serves_a_high_sources_entities() -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, find_related_by_entities

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, entities) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'door', 'semantic', 'v', 'active', 'high', 1, "
        f"['acme-prod']), ('o', 'n', 'a', '-', '-', '{_B}', 'tea', 'semantic', 'v', 'active', "
        "'', 1, ['acme-prod'])",
    )
    ident = Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    ents, rows = await find_related_by_entities(ident, "door", scope=WHOLE_NAMESPACE, ch=ch)
    assert ents == [] and rows == []


@pytest.mark.asyncio
async def test_a_wildcard_key_read_without_high_answers_the_plain_row_behind_it() -> None:
    # procedure.match and procedure.list: another agent's newer high row of the key must
    # not hide the plain row the search matched.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, get_many_by_keys

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'p:deploy', 'procedural', 'plain', 'active', '', 1), "
        f"('o', 'n', 'b', '-', '-', '{_B}', 'p:deploy', 'procedural', 'vault', 'active', "
        "'high', 2)",
    )
    ident = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    every = await get_many_by_keys(ident, ["p:deploy"], scope=WHOLE_NAMESPACE, ch=ch)
    assert [r.sensitivity for r in every] == ["high"]
    plain = await get_many_by_keys(
        ident, ["p:deploy"], scope=WHOLE_NAMESPACE, ch=ch, exclude_high_sensitivity=True
    )
    assert [(r.agent, r.value) for r in plain] == [("a", "plain")]


@pytest.mark.asyncio
async def test_a_key_read_without_high_answers_nothing_when_the_head_is_high() -> None:
    # One identity: FINAL collapses to the high v2 before the filter, so the older plain v1
    # never answers in its place.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, get_many_by_keys

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'p:deploy', 'procedural', 'plain', 'active', '', 1), "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'p:deploy', 'procedural', 'vault', 'active', "
        "'high', 2)",
    )
    for agent in ("a", "*"):
        ident = Identity(org="o", namespace="n", agent=agent, user_id="-", session_id="-")
        rows = await get_many_by_keys(
            ident, ["p:deploy"], scope=WHOLE_NAMESPACE, ch=ch, exclude_high_sensitivity=True
        )
        assert rows == []


@pytest.mark.asyncio
async def test_high_memory_ids_names_only_the_rows_with_a_high_version() -> None:
    ch = _LocalCH(
        ddl("memory_versions"),
        _version(_A, "k", 1, ""),
        _version(_B, "k", 1, ""),
        _version(_B, "k", 2, "high"),
        _version(_C, "other", 1, "high"),
    )
    assert await memory_repo.high_memory_ids("o", "n", "k", [_A, _B, _C], ch=ch) == {_B}
    assert await memory_repo.high_memory_ids("o", "n", "k", [_A], ch=ch) == set()


@pytest.mark.asyncio
async def test_a_key_read_without_high_still_answers_one_row_per_key() -> None:
    # Agent and user pinned, session open: the high filter must not count as a pinned axis.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, get_many_by_keys

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version) VALUES "
        f"('o', 'n', 'a', 'u', 's1', '{_A}', 'p:deploy', 'procedural', 'old', 'active', '', 1), "
        f"('o', 'n', 'a', 'u', 's2', '{_B}', 'p:deploy', 'procedural', 'new', 'active', '', 2)",
    )
    ident = Identity(org="o", namespace="n", agent="a", user_id="u", session_id="*")
    rows = await get_many_by_keys(
        ident, ["p:deploy"], scope=WHOLE_NAMESPACE, ch=ch, exclude_high_sensitivity=True
    )
    assert [r.value for r in rows] == ["new"]


@pytest.mark.asyncio
async def test_a_user_bounded_token_still_reads_one_row_per_key() -> None:
    # The token's scope adds a predicate, which must not count as a pinned session: with the
    # session open, the newest of the user's rows answers, not every session's.
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.store import Identity, get_many_by_keys

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version) VALUES "
        f"('o', 'n', 'a', 'u', 's1', '{_A}', 'note', 'semantic', 'old', 'active', '', 1), "
        f"('o', 'n', 'a', 'u', 's2', '{_B}', 'note', 'semantic', 'new', 'active', '', 2)",
    )
    own_user = ReadFilters(scope="own_user", user_id="u", session_id=None)
    ident = Identity(org="o", namespace="n", agent="a", user_id="u", session_id="*")
    rows = await get_many_by_keys(ident, ["note"], scope=own_user, ch=ch)
    assert [r.value for r in rows] == ["new"]


@pytest.mark.asyncio
async def test_a_period_row_never_carries_a_high_sources_entities() -> None:
    from types import SimpleNamespace

    from server.memory.app import period_digest

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, entities) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'door', 'semantic', 'v', 'active', 'high', 1, "
        f"['acme-prod']), ('o', 'n', 'a', '-', '-', '{_B}', 'tea', 'semantic', 'v', 'active', "
        "'', 1, ['oolong'])",
    )
    period = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="-", digests=[{"derived_from": ["door", "tea"]}]
    )
    ids, ents = await period_digest._source_memories(ch, period)  # type: ignore[arg-type]
    assert sorted(ids) == sorted([_A, _B]) and ents == ["oolong"]
    # another user's period finds neither: the keys are this identity's rows
    other = SimpleNamespace(**{**vars(period), "user_id": "u2"})
    assert await period_digest._source_memories(ch, other) == ([], [])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_period_row_takes_entities_only_from_sources_the_authority_serves(
    monkeypatch,
) -> None:
    # Under Postgres a change ClickHouse has not published still reads as the old row here.
    # A relabel to 'high', a rewrite, a supersede or a delete and set again lends no
    # entities (the source keeps its place in the lineage); a copy of the same text (a
    # pin, a touch) still lends them.
    from types import SimpleNamespace

    from server.core import pg
    from server.memory.app import period_digest
    from server.memory.store import pg_memory

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, entities) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'door', 'semantic', 'v', 'active', '', 1, "
        f"['acme-prod']), ('o', 'n', 'a', '-', '-', '{_B}', 'tea', 'semantic', 'v', 'active', "
        f"'', 1, ['oolong']), ('o', 'n', 'a', '-', '-', '{_C}', 'desk', 'semantic', 'w', "
        f"'active', '', 1, ['teak']), ('o', 'n', 'a', '-', '-', '{_D}', 'chair', 'semantic', "
        f"'x', 'active', '', 1, ['oak']), ('o', 'n', 'a', '-', '-', '{_E}', 'lamp', "
        "'semantic', 'y', 'active', '', 1, ['brass'])",
    )
    read = {
        r["memory_key"]: r
        for r in await ch.query_all(
            "SELECT memory_key, toString(memory_id) AS memory_id, "
            "toString(content_sha256) AS content_sha256 FROM loom.memory FINAL"
        )
    }
    newer = {
        "door": {"sensitivity": "high"},  # labelled 'high' since the read
        "tea": {},  # pinned since the read: the same text
        "desk": {"content_sha256": "0" * 64},  # rewritten since the read
        "chair": {"status": "superseded"},  # superseded since the read
        "lamp": {"memory_id": _F},  # deleted and set again with the same text
    }

    async def _heads(keys, **_k):
        return {
            k: {
                **read[k[5]],
                "version": 2,
                "status": "active",
                "redacted_at": None,
                "sensitivity": "",
                **newer[k[5]],
            }
            for k in keys
        }

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    period = SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        digests=[{"derived_from": ["door", "tea", "desk", "chair", "lamp"]}],
    )
    ids, ents = await period_digest._source_memories(ch, period)  # type: ignore[arg-type]
    assert sorted(ids) == sorted([_A, _B, _C, _D, _E]) and ents == ["oolong"]


@pytest.mark.asyncio
async def test_a_period_taken_out_counts_as_written_so_a_pass_never_writes_it_back() -> None:
    from server.memory.app import period_digest

    def row(
        mid: str,
        key: str,
        status: str,
        version: int,
        *,
        gone: bool = False,
        redacted: bool = False,
        **who: str,
    ) -> str:
        w = {"agent": "a", "user_id": "-", "session_id": "-", **who}
        return (
            f"('o', 'n', '{w['agent']}', '{w['user_id']}', '{w['session_id']}', '{mid}', "
            f"'{key}', 'semantic', 'v', '{status}', {version}, {'now()' if gone else 'NULL'}, "
            f"{'now()' if redacted else 'NULL'})"
        )

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, version, tombstoned_at, redacted_at) VALUES "
        + ", ".join(
            [
                row(_A, "period_digest_s1_s5", "active", 1),
                row(_A, "period_digest_s1_s5", "active", 2, gone=True),  # deleted since
                row(_B, "period_digest_s6_s10", "rejected", 1),
                row(_C, "period_digest_s11_s15", "active", 1),
                row(_D, "summary:s1", "active", 1),  # not a period
                row(_E, "period_digest_s16_s20", "active", 1, agent="other"),
                row(_F, "period_digest_s21_s25", "active", 1, session_id="s9"),
                row(_G, "period_digest_s26_s30", "active", 1, gone=True),
                row(_G, "period_digest_s26_s30", "active", 2),  # restored since
                row(_H, "period_digest_s31_s35", "active", 1, redacted=True),
            ]
        ),
    )
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-"}
    every = {
        "period_digest_s1_s5",
        "period_digest_s6_s10",
        "period_digest_s11_s15",
        "period_digest_s26_s30",
        "period_digest_s31_s35",
    }
    assert await period_digest._skipped_period_keys(ch, ident, rewrite=False) == every
    taken_out = {"period_digest_s1_s5", "period_digest_s6_s10", "period_digest_s31_s35"}
    assert await period_digest._skipped_period_keys(ch, ident, rewrite=True) == taken_out


@pytest.mark.asyncio
async def test_the_taxonomy_never_counts_a_high_rows_entities() -> None:
    from server.identity.api.models_namespace_catalog import NamespaceTaxonomyInput
    from server.identity.app import namespace_catalog

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, entities) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'door', 'semantic', 'v', 'active', 'high', 1, "
        f"['acme-prod']), ('o', 'n', 'a', '-', '-', '{_B}', 'tea', 'semantic', 'v', 'active', "
        "'', 1, ['oolong'])",
    )
    out = await namespace_catalog._compute_taxonomy(
        ch, NamespaceTaxonomyInput(org="o", namespace="n")
    )
    assert [e.value for e in out.entities] == ["oolong"]


@pytest.mark.asyncio
async def test_a_distill_pass_never_reads_a_high_observation_into_its_context() -> None:
    # The whole-session context feeds the notes prompt, which is sent to the model; earlier
    # passes may have marked a high observation done, so this read must filter it itself.
    from server.memory.domain.observation_tags import OBSERVATION_TAG
    from server.memory.workers import observe_extract

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, tags) VALUES "
        f"('o', 'n', 'a', '-', 's', '{_A}', 'obs/1', 'episodic', "
        "'{\"narrative\": \"ran the tests\"}', 'active', '', 1, "
        f"['{OBSERVATION_TAG}']), ('o', 'n', 'a', '-', 's', '{_B}', 'obs/2', 'episodic', "
        "'{\"narrative\": \"printed the token\"}', 'active', 'high', 1, "
        f"['{OBSERVATION_TAG}']), ('o', 'n', 'a', '-', 's', '{_C}', 'obs/3', 'episodic', "
        f"'{{\"narrative\": \"export GITHUB_TOKEN=ghp_{'a' * 36}\"}}', 'active', '', 1, "
        f"['{OBSERVATION_TAG}'])",
    )
    session = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "s"}
    context = await observe_extract._load_session_context(session, ch=ch)
    assert len(context) == 1 and "ran the tests" in str(context[0])


@pytest.mark.asyncio
async def test_a_distill_pass_reads_only_observations_the_authority_serves(monkeypatch) -> None:
    # Under Postgres ClickHouse can still hold a row whose delete, rewrite or redaction is
    # not published: the context is sent to the model, so it keeps what the authority's
    # heads still show. An earlier pass's DONE mark changes only the tags: its row stays.
    from datetime import datetime

    from server.core import pg
    from server.memory.domain.observation_tags import CRYSTALLIZE_DONE_TAG, OBSERVATION_TAG
    from server.memory.store import pg_memory
    from server.memory.workers import observe_extract

    narratives = ["ran the tests", "deleted since", "rewritten", "redacted", "set again"]
    narratives += ["labelled", "skipped", "retyped", "untagged"]
    ids = (_A, _B, _C, _D, *(f"00000000-0000-0000-0000-0000000000{i}0" for i in range(1, 6)))
    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, tags) VALUES "
        + ", ".join(
            f"('o', 'n', 'a', '-', 's', '{mid}', 'obs/{i}', 'episodic', "
            f"'{{\"narrative\": \"{text}\"}}', 'active', '', 1, ['{OBSERVATION_TAG}'])"
            for i, (mid, text) in enumerate(zip(ids, narratives, strict=True))
        ),
    )
    read = {
        r["memory_key"]: r
        for r in await ch.query_all(
            "SELECT memory_key, toString(memory_id) AS memory_id, "
            "toString(content_sha256) AS content_sha256 FROM loom.memory FINAL"
        )
    }
    done = [OBSERVATION_TAG, CRYSTALLIZE_DONE_TAG]
    newer = {
        "obs/0": {"tags": done},
        "obs/2": {"content_sha256": "0" * 64},
        "obs/3": {"redacted_at": datetime(2026, 1, 1)},
        # deleted, then set again with the same text: another memory
        "obs/4": {"memory_id": "00000000-0000-0000-0000-0000000000ff"},
        # the same memory and text, no longer an observation the statement selects
        "obs/5": {"sensitivity": "high"},
        "obs/6": {"tags": [OBSERVATION_TAG, "skipped:empty"]},
        "obs/7": {"memory_type": "semantic"},
        "obs/8": {"tags": []},
    }

    async def _heads(keys, **_k):
        return {
            k: {
                **read[k[5]],
                "version": 2,
                "memory_type": "episodic",
                "tags": [OBSERVATION_TAG],
                "sensitivity": "",
                "redacted_at": None,
                **newer[k[5]],
            }
            for k in keys
            if k[5] in newer
        }

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    session = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "s"}
    context = await observe_extract._load_session_context(session, ch=ch)
    assert [o.narrative for o in context] == ["ran the tests"]


@pytest.mark.asyncio
async def test_memory_digests_never_serves_a_stored_credential_key_fact(monkeypatch) -> None:
    # A digest an earlier build wrote may hold one: the read leaves it out.
    from server.memory.api.models_temporal import MemoryDigestsInput
    from server.memory.app import curation

    token = "ghp_" + "a" * 36
    ch = _LocalCH(
        "SET enable_quantized_codec = 1",  # the summary embedding column's codec
        ddl("session_summaries"),
        "INSERT INTO loom.session_summaries (org, namespace, agent, user_id, session_id, "
        "summary_text, key_facts, summary_embedding) VALUES ('o', 'n', 'a', '-', 's', "
        f"'a session', ['the token is {token}', 'ran the tests'], "
        "arrayWithConstant(3072, toFloat32(0)))",
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await curation.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a")
    )
    assert [r.key_facts for r in out.rows] == [["ran the tests"]]


@pytest.mark.asyncio
async def test_the_sweep_labels_a_token_on_its_own_line() -> None:
    # Stored as JSON, the newline before the token is `\n`, which hides the detector's word
    # boundary; the sweep reads the decoded value, as the write path does. A string value is
    # a quoted literal in `value` (`value_json` holds only objects).
    import json

    from server.memory.app import sensitivity_sweep

    token = "ghp_" + "a" * 36
    as_object = json.dumps({"narrative": "[command_run] tool=Bash\noutput:\n" + token})
    as_string = json.dumps("output:\n" + token)
    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, value_json, status, sensitivity, version) VALUES "
        f"('o', 'n', 'a', '-', 's', '{_A}', 'obs/1', 'episodic', {as_object!r}, {as_object!r}, "
        f"'active', '', 1), ('o', 'n', 'a', '-', 's', '{_B}', 'note', 'semantic', "
        f"{as_string!r}, '{{}}', 'active', '', 1)",
    )
    report = await sensitivity_sweep.scan(
        org="o", namespace="n", limit=100, page_size=100, pages=1, ch=ch
    )
    found = sorted((c.memory_key, c.level) for c in report.candidates)
    assert found == [("note", "high"), ("obs/1", "high")]


@pytest.mark.asyncio
async def test_the_scheduled_sweep_applies_every_high_shape(monkeypatch) -> None:
    # The worker keeps only `credential` and `email` rows by default; a `ghp_` token or a
    # password assignment is high and must count as a credential, not `other`.
    from server.memory.app import sensitivity_sweep as sweep
    from server.memory.workers import sensitivity_sweep as worker

    ch = _memory(
        _row(_A, "gh", "the token is ghp_" + "a" * 36),
        _row(_B, "pw", "db password: hunter2secret"),
        _row(_C, "plain", "tea at four"),
    )
    applied: list[str] = []

    async def _apply(candidates, **_k):
        applied.extend(c.memory_key for c in candidates)
        return sweep.ApplyResult()

    monkeypatch.setattr(sweep, "apply", _apply)
    await worker.run_pass(ch=ch)
    assert sorted(applied) == ["gh", "pw"]


@pytest.mark.asyncio
async def test_an_earlier_builds_escaped_key_fact_keeps_its_digest_home() -> None:
    import json

    from server.memory.domain import okf_evidence

    token = "ghp_" + "a" * 36
    digest = okf_evidence.DigestRecord(
        session_id="s",
        summary_text="a session",
        key_facts=[json.dumps({"note": "token:\n" + token})],  # as an earlier build stored it
    )
    assert okf_evidence.evidence_is_high(digest, set())


def test_a_digests_summary_and_key_facts_are_read_one_by_one() -> None:
    # Joined, a summary ending in `password:` read the first key fact as its value; each
    # part is checked on its own, as memory.digests and the outcome judge read them.
    from server.memory.domain import okf_evidence

    joined = okf_evidence.DigestRecord(
        session_id="s", summary_text="set the db password:", key_facts=["abcdefgh12"]
    )
    assert not okf_evidence.evidence_is_high(joined, set())
    inside = okf_evidence.DigestRecord(
        session_id="s", summary_text="a session", key_facts=["the token is ghp_" + "a" * 36]
    )
    assert okf_evidence.evidence_is_high(inside, set())


@pytest.mark.asyncio
async def test_a_high_row_past_its_window_is_never_read_nor_hides_a_live_row() -> None:
    # Agent b's newer high row of the key is past its retention window; agent a's plain row is
    # live. An open-axis read answers a's row, as memory.get does, and b's own read finds none.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, get_many_by_keys

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, expires_at) VALUES "
        f"('o', 'n', 'a', '-', '-', '{_A}', 'k', 'semantic', 'plain', 'active', '', 1, NULL), "
        f"('o', 'n', 'b', '-', '-', '{_B}', 'k', 'semantic', 'vault', 'active', 'high', 2, "
        "'2020-01-01 00:00:00')",
    )
    ident = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    rows = await get_many_by_keys(ident, ["k"], scope=WHOLE_NAMESPACE, ch=ch)
    assert [(r.agent, r.value) for r in rows] == [("a", "plain")]
    b = Identity(org="o", namespace="n", agent="b", user_id="-", session_id="-")
    assert await get_many_by_keys(b, ["k"], scope=WHOLE_NAMESPACE, ch=ch) == []


@pytest.mark.asyncio
async def test_an_as_of_read_never_serves_a_high_row_past_its_window() -> None:
    # memory.get with as_of replays past expiry, but a high row's expiry is its retention.
    from datetime import UTC, datetime

    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import Identity, get_by_key

    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, memory_type, value, status, sensitivity, version, created_at, expires_at) "
        f"VALUES ('o', 'n', 'a', '-', '-', '{_A}', 'vault', 'semantic', 'v', 'active', 'high', 1, "
        f"'2019-01-01 00:00:00', '2020-01-01 00:00:00'), ('o', 'n', 'a', '-', '-', '{_B}', "
        "'old', 'semantic', 'v', 'active', '', 1, '2019-01-01 00:00:00', '2020-01-01 00:00:00')",
    )
    ident = Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    now = datetime.now(UTC).replace(tzinfo=None)
    read = {"scope": WHOLE_NAMESPACE, "ch": ch, "as_of": now, "include_expired": False}
    assert await get_by_key(ident, "vault", **read) is None
    plain = await get_by_key(ident, "old", **read)
    assert plain is not None and plain.memory_key == "old"
