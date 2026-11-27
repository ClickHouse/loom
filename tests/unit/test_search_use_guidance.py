"""Guidance for search results is bound to the authorized served head."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.domain.read_scope import ReadFilters
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app import search_pages
from server.memory.app.search import guidance
from server.memory.store import pg_memory, reads
from server.memory.store.rows import IDENTITY_COLUMNS

_INPUT = MemorySearchInput(org="o", namespace="n", query="deploy")
_KEY = ("o", "n", "a", "u", "s", "notes/deploy")


def _hit(*, version: int = 42, user: str = "u") -> _MemorySearchHit:
    hit = _MemorySearchHit(
        memory_key=_KEY[-1],
        memory_type="semantic",
        content_excerpt="a deployed excerpt",
        distance=0,
        similarity=1,
        version=version,
    )
    hit._row_identity = ("a", user, "s")
    return hit


def _head(**fields: Any) -> dict[str, Any]:
    return {
        "version": 42,
        "status": "active",
        "source_class": "asserted",
        "source_trace": "",
        "source_resource_id": "",
        "requires_review": 0,
        "verified_by": "human:reviewer",
        "verified_sha256": "a" * 64,
        "content_sha256": "a" * 64,
        "tags": [],
        "files": [],
        **fields,
    }


@pytest.mark.asyncio
async def test_search_and_page_project_same_exact_authorized_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        assert keys == [_KEY]
        return {_KEY: _head()}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    assert await guidance.attach_search_guidance(_INPUT, [hit]) == [hit]
    assert hit.use_guidance is not None and hit.use_guidance.review == "human"
    wire = hit.model_dump()
    assert (wire["agent"], wire["user_id"], wire["session_id"]) == ("a", "u", "s")
    parked = search_pages._parked(hit)
    assert "use_guidance" not in parked
    parked["use_guidance"] = hit.use_guidance.model_dump()
    round_trip = search_pages.unparked(parked)
    assert round_trip is not None and round_trip.use_guidance is None
    assert await guidance.attach_search_guidance(_INPUT, [round_trip], parked=True) == [round_trip]
    assert round_trip.use_guidance == hit.use_guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["fresh", "changed", "unknown", "not_applicable"])
async def test_parked_hit_refreshes_cached_code_state(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        assert keys == [_KEY]
        return {_KEY: _head()}

    async def code_states(org: str, rows: list[dict[str, Any]]) -> list[str]:
        return [state]

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    monkeypatch.setattr(guidance, "guidance_code_states", code_states)
    hit = _hit()
    hit.code_state = "missing"
    await guidance.attach_search_guidance(_INPUT, [hit], snapshot_only=True)
    parked = search_pages.unparked(search_pages._parked(hit))
    assert parked is not None
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == [parked]
    assert parked.use_guidance is not None and parked.use_guidance.freshness.code == state
    assert parked.code_state == (state if state in ("changed", "fresh") else "")


@pytest.mark.asyncio
async def test_search_guidance_classifies_the_served_quarantined_procedure(monkeypatch):
    async def heads(keys):
        return {_KEY: _head(procedure_fields='{"quarantined": {}}')}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    hit.memory_type = "procedural"
    assert await guidance.attach_search_guidance(_INPUT, [hit]) == [hit]
    assert hit.use_guidance is not None
    assert hit.use_guidance.lifecycle.procedure_status == "quarantined"
    assert hit.use_guidance.next_step == "inspect_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head",
    [
        None,
        _head(version=43),
        _head(tombstoned_at="now"),
        _head(redacted_at="now"),
        _head(sensitivity="high"),
        _head(status="rejected"),
    ],
)
async def test_parked_hit_cannot_keep_guidance_after_edit_or_erasure(
    monkeypatch: pytest.MonkeyPatch,
    head: dict[str, Any] | None,
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        assert keys == [_KEY]
        return {} if head is None else {_KEY: head}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    fresh = await guidance.attach_search_guidance(_INPUT, [hit])
    if head is not None and head.get("version") == 43:
        assert fresh == [hit]
        assert hit.use_guidance is not None and hit.use_guidance.review == "unknown"
    else:
        assert fresh == []
    assert await guidance.attach_search_guidance(_INPUT, [hit], parked=True) == []


@pytest.mark.asyncio
async def test_narrower_page_scope_never_reads_or_serves_another_users_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        assert keys == [_KEY]
        return {_KEY: _head()}

    monkeypatch.setattr(guidance, "request_scope", lambda: ReadFilters("own_user", "u", None))
    monkeypatch.setattr(guidance, "guidance_heads", heads)
    mine, other = _hit(), _hit(user="other")
    assert await guidance.attach_search_guidance(_INPUT, [mine, other], parked=True) == [mine]
    assert other.use_guidance is None


@pytest.mark.asyncio
async def test_unavailable_current_evidence_cannot_reuse_a_parked_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        raise OSError("store unreachable")

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    assert await guidance.attach_search_guidance(_INPUT, [hit]) == [hit]
    assert hit.use_guidance is not None and hit.use_guidance.review == "unknown"
    assert await guidance.attach_search_guidance(_INPUT, [hit], parked=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("head_redacted", [False, True])
async def test_replay_keeps_edited_history_and_withholds_a_redacted_version(
    monkeypatch, head_redacted
):
    from server.memory.store.history import HistoryTarget

    async def heads(keys):
        return {_KEY: _head(memory_id="m", redacted_at="now" if head_redacted else None)}

    async def history(org, memory_id, version):
        assert memory_id == "m"
        return HistoryTarget(
            org=org,
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            memory_key=_KEY[-1],
            version=version,
            memory_id=memory_id,
            content_sha256="a" * 64,
            redacted_at="now" if version == 40 else None,
        )

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    monkeypatch.setattr(guidance, "find_history_target", history)
    current, historical, redacted = _hit(), _hit(version=41), _hit(version=40)
    assert await guidance.attach_search_guidance(
        _INPUT, [current, historical, redacted], parked=True, replay=True
    ) == ([historical] if head_redacted else [current, historical])
    assert historical.use_guidance is not None and historical.use_guidance.review == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("procedure_fields", [None, '{"quarantined": {}}'])
async def test_postgres_guidance_projection_decodes_sparse_defaults(
    monkeypatch: pytest.MonkeyPatch,
    procedure_fields: str | None,
) -> None:
    async def fetch(query: str, *params: Any) -> list[dict[str, Any]]:
        assert params[:6] == tuple([part] for part in _KEY)
        assert "value" not in params[6]
        assert "c.row->>'memory_type' = 'procedural'" in query
        return [
            {
                **dict(zip(IDENTITY_COLUMNS, _KEY, strict=True)),
                "evidence": {"version": 42, "source_class": "asserted", "requires_review": 0},
                "procedure_fields": procedure_fields,
            }
        ]

    monkeypatch.setattr(pg_memory.pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory.pg, "fetch", fetch)
    heads = await reads.guidance_heads([_KEY])
    assert heads[_KEY]["status"] == "active"
    assert heads[_KEY]["verified_by"] == ""
    assert heads[_KEY]["source_trace"] == ""
    assert heads[_KEY]["tags"] == []
    assert "value" not in heads[_KEY]
    assert heads[_KEY].get("procedure_fields") == procedure_fields


@pytest.mark.asyncio
async def test_source_lookup_failure_keeps_authorized_code_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        return {_KEY: _head(source_class="external", source_resource_id="doc")}

    async def source_states(org: str, rows: list[dict[str, Any]]) -> list[str]:
        assert org == "o" and len(rows) == 1
        raise TimeoutError

    async def code_states(org: str, rows: list[dict[str, Any]]) -> list[str]:
        return ["changed"]

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    monkeypatch.setattr(guidance, "guidance_source_states", source_states)
    monkeypatch.setattr(guidance, "guidance_code_states", code_states)
    hit = _hit()
    assert await guidance.attach_search_guidance(_INPUT, [hit]) == [hit]
    assert hit.use_guidance is not None
    assert hit.use_guidance.freshness.source == "unknown"
    assert hit.use_guidance.freshness.code == "changed"
    assert hit.use_guidance.next_step == "recheck_source"


async def test_parked_hit_rejects_replacement_with_same_version(monkeypatch):
    head = _head()

    async def heads(keys):
        return {_KEY: head}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    await guidance.attach_search_guidance(_INPUT, [hit], snapshot_only=True)
    parked = search_pages.unparked(search_pages._parked(hit))
    assert parked is not None
    head.update(content_sha256="b" * 64, verified_sha256="b" * 64)
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == []


async def test_unchanged_superseded_anchor_hit_survives_a_handle_only_continuation(monkeypatch):
    """An anchor hit read under `include_superseded` keeps the status its row had, so the
    continuation (whose request does not repeat the flag) serves it while the row is unchanged."""
    head = _head(status="superseded")

    async def heads(keys):
        return {_KEY: head}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    served = MemorySearchInput(org="o", namespace="n", query="deploy", include_superseded=True)
    hit = _hit()
    hit.source = "anchor"
    hit.status = "superseded"
    await guidance.attach_search_guidance(served, [hit], snapshot_only=True)
    parked = search_pages.unparked(search_pages._parked(hit))
    assert parked is not None and parked.status == "superseded"
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == [parked]
    # A hit parked without its status (the anchor pool did not select it) is dropped.
    parked.status = ""
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == []


@pytest.mark.asyncio
async def test_key_match_superseded_hit_survives_a_handle_only_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key-match arm's own hit, as that arm builds it, carries its row's status through
    the parked page, so an unchanged superseded row stays served."""
    import server.core.ch as ch_mod
    from server.memory.app.search.key_match import _run_key_match_pool

    row = {
        "memory_key": _KEY[-1],
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "version": 42,
        "status": "superseded",
        "content_sha256": "a" * 64,
        "similarity": 0.7,
        "strength": 0.9,
    }

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"any_key": 1}] if "any_key" in sql else [row]

    async def heads(keys: list[tuple[str, ...]]) -> dict[tuple[str, ...], dict[str, Any]]:
        return {_KEY: _head(status="superseded")}

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(guidance, "guidance_heads", heads)
    served = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        query="deploy notes",
        key_match_mode="always",
        include_superseded=True,
    )
    ctx = SimpleNamespace(
        input=served,
        all_hits=[],
        actual_work={},
        namespaces=["n"],
        query_vec=[0.1],
        embedder=SimpleNamespace(model_name="m"),
        endpoint=None,
        query_shape="",
    )
    await _run_key_match_pool(ctx)
    (hit,) = ctx.all_hits
    assert hit.status == "superseded"
    await guidance.attach_search_guidance(served, [hit], snapshot_only=True)
    parked = search_pages.unparked(search_pages._parked(hit))
    assert parked is not None and parked.status == "superseded"
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == [parked]


async def test_previous_build_cursor_cannot_give_folded_passage_parent_approval(monkeypatch):
    async def heads(keys):
        return {_KEY: _head()}

    async def states(org, rows):
        return ["not_applicable"] * len(rows)

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    monkeypatch.setattr(guidance, "guidance_code_states", states)
    monkeypatch.setattr(guidance, "guidance_source_states", states)
    parent = _hit()
    await guidance.attach_search_guidance(_INPUT, [parent])
    assert parent.use_guidance is not None
    assert parent.use_guidance.next_step == "follow_if_applicable"

    # The old ClickHouse cursor retained the identity and version but omitted chunk identity.
    cursor = _hit().model_dump()
    cursor.update(content_excerpt="folded passage", row_identity=["a", "u", "s"])
    hit = search_pages.unparked(cursor)
    assert hit is not None
    assert await guidance.attach_search_guidance(_INPUT, [hit], parked=True) == [hit]
    assert hit.content_excerpt == "folded passage"
    assert hit.use_guidance is not None
    assert hit.use_guidance.review == "unknown"
    assert hit.use_guidance.next_step == "validate_lead"


async def test_parked_hit_keeps_review_when_a_metadata_write_advances_the_version(monkeypatch):
    head = _head()

    async def heads(keys):
        return {_KEY: head}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    hit = _hit()
    await guidance.attach_search_guidance(_INPUT, [hit], snapshot_only=True)
    parked = search_pages.unparked(search_pages._parked(hit))
    assert parked is not None
    head["version"] += 2
    assert await guidance.attach_search_guidance(_INPUT, [parked], parked=True) == [parked]
    assert parked.use_guidance is not None and parked.use_guidance.review == "human"


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("as_bytes", [True, False])
async def test_fresh_vector_uses_current_review_only_for_matching_content(
    monkeypatch, matches, as_bytes
):
    from server.memory.app.retrieval.search import _fact_rows_to_hits
    from server.memory.app.search.vector_legs import _stamp_row_fields
    from server.memory.domain.search_types import SearchQuery

    async def heads(keys):
        return {_KEY: _head(version=43)}

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    digest = ("a" if matches else "b") * 64
    (vector_hit,) = _fact_rows_to_hits(
        [
            {
                "memory_key": _KEY[-1],
                "memory_type": "semantic",
                "content_excerpt": "a deployed excerpt",
                "distance": 0,
                "rank_score": 1,
                "content_sha256": digest.encode() if as_bytes else digest,
            }
        ],
        SearchQuery(
            org="o",
            namespace="n",
            scope="private",
            agent="a",
            embedding_model="test",
            query_vec=[1.0],
            top_k=1,
        ),
        "0.0",
    )
    hit = _stamp_row_fields(vector_hit, _hit())
    assert await guidance.attach_search_guidance(_INPUT, [hit]) == [hit]
    assert hit.use_guidance is not None
    assert hit.use_guidance.review == ("human" if matches else "unknown")
    assert hit._guidance_head_version == (43 if matches else 0)


@pytest.mark.parametrize("history_fails", [False, True])
async def test_replay_rechecks_ttl_and_keeps_confirmed_hits(monkeypatch, history_fails):
    from server.memory.store.history import HistoryTarget

    expired_key = (*_KEY[:-1], "expired")

    async def heads(keys):
        return {
            _KEY: _head(memory_id="m", valid_to="2000-01-01T00:00:00"),
            expired_key: _head(memory_id="expired"),
        }

    async def history(org, memory_id, version):
        assert version == 41
        if history_fails:
            raise TimeoutError
        return HistoryTarget(
            org=org,
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            memory_key=expired_key[-1] if memory_id == "expired" else _KEY[-1],
            version=version,
            memory_id=memory_id,
            content_sha256="a" * 64,
            expires_at="2000-01-01T00:00:00" if memory_id == "expired" else None,
        )

    monkeypatch.setattr(guidance, "guidance_heads", heads)
    monkeypatch.setattr(guidance, "find_history_target", history)
    current, edited, expired = _hit(), _hit(version=41), _hit(version=41)
    expired.memory_key = "expired"
    expired.governed = True
    kept = await guidance.attach_search_guidance(
        _INPUT, [current, edited, expired], parked=True, replay=True
    )
    assert kept == ([current] if history_fails else [current, edited])
