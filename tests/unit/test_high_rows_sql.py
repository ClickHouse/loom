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

_A, _B, _C, _D = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abcd")


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
