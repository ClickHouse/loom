"""The tier and body readers read the user axis the way the
similarity legs do — a request that names no user reads every user's rows."""

from types import SimpleNamespace

import pytest

from server.memory.app.search.tiers import _prepend_anchor, _prepend_recent, _reader_user_id


def test_reader_user_id_is_the_wildcard_when_no_user_is_named():
    assert _reader_user_id(SimpleNamespace(user_id="-")) == "*"
    assert _reader_user_id(SimpleNamespace(user_id=None)) == "*"
    assert _reader_user_id(SimpleNamespace()) == "*"
    assert _reader_user_id(SimpleNamespace(user_id=" eric ")) == "eric"


@pytest.mark.asyncio
async def test_anchor_tier_reads_rows_written_under_actor_user_ids(monkeypatch):
    from server.memory import store as repo

    seen: list[str] = []

    async def fake_list(ident, **kw):
        seen.append(ident.user_id)
        return [
            SimpleNamespace(
                memory_key="jennifer_plan",
                value="the base plan",
                memory_type="semantic",
                run_id="r1",
                created_at="2026-01-01",
            )
        ]

    monkeypatch.setattr(repo, "list_by_namespace", fake_list)
    inp = SimpleNamespace(
        anchor_first=1,
        org="o",
        namespace="ns",
        agent="*",
        user_id="-",
        session_id="-",
        run_id="r1",
        top_k=3,
    )
    out, _ = await _prepend_anchor([SimpleNamespace(memory_key="sim", rank_score=0.9)], inp, {})
    assert seen == ["*"]
    assert [x.memory_key for x in out] == ["jennifer_plan", "sim"]


@pytest.mark.asyncio
async def test_named_user_stays_pinned_for_the_recent_tier(monkeypatch):
    from server.memory import store as repo

    seen: list[str] = []

    async def fake_list(ident, **kw):
        seen.append(ident.user_id)
        return []

    monkeypatch.setattr(repo, "list_by_namespace", fake_list)
    inp = SimpleNamespace(
        recent_first=1,
        org="o",
        namespace="ns",
        agent="*",
        user_id="eric",
        session_id="-",
        run_id="",
        top_k=3,
    )
    await _prepend_recent([], inp, {})
    assert seen == ["eric"]
