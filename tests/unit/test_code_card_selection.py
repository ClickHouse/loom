"""Which files get an orientation card.

Selection merge (pagerank / churn / mixed), the per-model dedupe, the
setting, and the worker's candidate order end to end with a fake repo.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.code.domain.index.card_selection import (
    churn_eligible,
    select_card_paths,
    split_csv,
    twin_rows,
)
from server.settings import settings

# ---- pure selection ---------------------------------------------------------


def test_churn_eligible_source_tree_only():
    src, vendored = ("src/",), ("base/", "contrib/")
    assert churn_eligible("src/Access/QuotaCache.cpp", prefixes=src, exclude=vendored)
    assert not churn_eligible("base/base/types.h", prefixes=src, exclude=vendored)
    assert not churn_eligible("contrib/poco/x.h", prefixes=src, exclude=vendored)
    assert not churn_eligible("tests/queries/x.sql", prefixes=src, exclude=vendored)
    # vendored at depth
    assert not churn_eligible("src/Foo/contrib/y.cpp", prefixes=src, exclude=vendored)
    # empty prefixes = any path, exclusions still hold
    assert churn_eligible("programs/server/Server.cpp", prefixes=(), exclude=vendored)
    assert not churn_eligible("base/x.h", prefixes=(), exclude=vendored)
    # no filter at all by default
    assert churn_eligible("contrib/poco/x.h")


def test_split_csv():
    assert split_csv("src/, programs/ ,") == ("src/", "programs/")
    assert split_csv("") == ()


def test_select_churn_falls_back_to_pagerank_when_thin():
    out = select_card_paths(pagerank_order=["p1", "c1", "p2"], churn_order=["c1"], limit=3)
    assert out == ["c1", "p1", "p2"]


def test_select_zero_limit_is_empty():
    assert select_card_paths(pagerank_order=["a"], churn_order=[], limit=0) == []


# ---- dedupe ------------------------------------------------------------------


def _row(model: str, path: str, **kw):
    return {"embedding_model": model, "scope_type": "file", "scope_path": path, **kw}


def test_twin_rows_only_where_the_active_model_has_the_file():
    rows = [
        _row("active", "a.cpp"),
        _row("active", "b.cpp"),
        _row("old", "a.cpp"),
        _row("old", "c.cpp"),
        _row("older", "b.cpp"),
    ]
    twins = twin_rows(rows, active_model="active")
    assert sorted((t["embedding_model"], t["scope_path"]) for t in twins) == [
        ("old", "a.cpp"),
        ("older", "b.cpp"),
    ]


def test_twin_rows_nothing_without_an_active_row():
    assert twin_rows([_row("old", "a.cpp")], active_model="active") == []


# ---- setting -----------------------------------------------------------------


def test_setting_default_and_shape():
    assert settings.code_summary_churn_days == 365
    # no default filter: any path may be proposed
    assert split_csv(settings.code_summary_churn_prefixes) == ()
    assert split_csv(settings.code_summary_churn_exclude) == ()


# ---- worker ------------------------------------------------------------------


class _Cfg:
    org = "clickhouse"
    namespace = "code-clickhouse"


def _fake_repo(*, hashes, live, others, churn):
    repo = AsyncMock()
    repo.live_file_hashes.return_value = hashes
    repo.live_summary_rows.return_value = live
    repo.live_summary_rows_other_models.return_value = others
    repo.pr_churn_ranked.return_value = churn
    repo.live_chunks_for_path.side_effect = lambda org, ns, path, model: [
        {"scope": "f", "content": f"int {path}();", "start_line": 1}
    ]
    repo.insert_rows = AsyncMock()
    return repo


def _live(path, *, model="m", pagerank=0.0, summary="", inputs_hash=""):
    return {
        "org": "clickhouse",
        "namespace": "code-clickhouse",
        "embedding_model": model,
        "scope_type": "file",
        "scope_path": path,
        "summary": summary,
        "summary_model": "",
        "inputs_hash": inputs_hash,
        "pagerank": pagerank,
        "in_degree": 0,
        "out_degree": 0,
        "key_symbols": [],
        "embedding": [],
        "embedding_dim": 0,
        "embed_attempts": 0,
        "embed_last_error": "",
        "embed_failed_at": None,
        "tombstoned_at": None,
        "created_at": None,
        "version": 1,
    }


async def _run_worker(repo, monkeypatch, **overrides):
    from server.code.workers import summary as code_summary

    for k, v in overrides.items():
        monkeypatch.setattr(settings, k, v)
    embedder = MagicMock()
    embedder.model_name = "m"
    embedder.embed = AsyncMock(return_value=[0.1, 0.2])
    llm = MagicMock()
    llm.model = "llm"
    llm.complete = AsyncMock(return_value=MagicMock(text="card text"))
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.store.index.CodeRepo.tombstone_of",
            side_effect=lambda table, live: {**live, "tombstoned_at": "now"},
        ),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
        patch("server.embedding.embedder.get_embedder", return_value=embedder),
        patch("server.llm.client.get_llm_client", return_value=llm),
        patch("server.observability.budget.namespace_over_cap", AsyncMock(return_value=False)),
        patch("server.workers.cost_event.emit_worker_cost_event_for"),
    ):
        return await code_summary.run_pass()


def _written(repo):
    """(path, embedding_model, tombstoned?) per row inserted, in order."""
    out = []
    for call in repo.insert_rows.await_args_list:
        table, rows = call.args
        assert table == "code_summaries"
        for r in rows:
            out.append((r["scope_path"], r["embedding_model"], r["tombstoned_at"] is not None))
    return out


@pytest.mark.asyncio
async def test_worker_churn_order_and_fill(monkeypatch):
    monkeypatch.setattr(settings, "code_summary_churn_prefixes", "src/")
    monkeypatch.setattr(settings, "code_summary_churn_exclude", "base/,contrib/")
    hashes = {"base/types.h": "h1", "src/hot.cpp": "h2", "src/cold.cpp": "h3"}
    live = [
        _live("base/types.h", pagerank=0.9),
        _live("src/cold.cpp", pagerank=0.5),
        _live("src/hot.cpp", pagerank=0.1),
    ]
    repo = _fake_repo(
        hashes=hashes, live=live, others=[], churn=[("src/hot.cpp", 40), ("src/gone.cpp", 9)]
    )
    res = await _run_worker(repo, monkeypatch, code_summary_max_scopes_per_pass=2)
    assert res.cards_written == 2 and not res.errors
    # churn first (a path the index no longer has is dropped), then pagerank fill
    assert [p for p, _, _ in _written(repo)] == ["src/hot.cpp", "base/types.h"]
    repo.pr_churn_ranked.assert_awaited_once()
    kw = repo.pr_churn_ranked.await_args.kwargs
    assert kw["days"] == 365 and kw["prefixes"] == ("src/",)
    assert kw["exclude"] == ("base/", "contrib/")


@pytest.mark.asyncio
async def test_worker_writes_card_for_churn_file_without_a_rank_row(monkeypatch):
    hashes = {"src/new.cpp": "h9"}
    repo = _fake_repo(hashes=hashes, live=[], others=[], churn=[("src/new.cpp", 12)])
    res = await _run_worker(
        repo, monkeypatch, code_summary_max_scopes_per_pass=5, code_summary_concurrency=2
    )
    assert res.cards_written == 1
    rows = repo.insert_rows.await_args.args[1]
    assert rows[0]["scope_path"] == "src/new.cpp" and rows[0]["embedding_model"] == "m"
    assert rows[0]["summary"] == "card text" and rows[0]["embedding_dim"] == 2
    assert rows[0]["inputs_hash"] == "h9"


@pytest.mark.asyncio
async def test_worker_skips_unchanged_cards(monkeypatch):
    hashes = {"src/hot.cpp": "h2"}
    live = [_live("src/hot.cpp", summary="old card", inputs_hash="h2")]
    repo = _fake_repo(hashes=hashes, live=live, others=[], churn=[("src/hot.cpp", 40)])
    res = await _run_worker(repo, monkeypatch)
    assert res.cards_written == 0 and res.skipped_unchanged == 1
    repo.insert_rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_dedupe_tombstones_twins_and_carries_text(monkeypatch):
    hashes = {"src/a.cpp": "ha", "src/b.cpp": "hb", "src/c.cpp": "hc"}
    live = [_live("src/a.cpp"), _live("src/b.cpp"), _live("src/c.cpp")]
    others = [
        # matching text: carried (embed only), then tombstoned
        _live("src/a.cpp", model="gemini", summary="a card", inputs_hash="ha"),
        # stale text: tombstoned, not carried
        _live("src/b.cpp", model="gemini", summary="stale", inputs_hash="old"),
        # no active twin: untouched
        _live("src/z.cpp", model="gemini"),
        # empty card under another model: tombstoned
        _live("src/c.cpp", model="small"),
    ]
    repo = _fake_repo(hashes=hashes, live=live, others=others, churn=[])
    res = await _run_worker(repo, monkeypatch, code_summary_max_scopes_per_pass=1)
    assert res.cards_carried == 1 and res.twins_tombstoned == 3
    # the carried card is now current: the LLM loop skips it and spends its
    # one slot on the next file
    assert res.skipped_unchanged == 1 and res.cards_written == 1
    written = _written(repo)
    assert ("src/a.cpp", "m", False) in written  # the carried card
    assert ("src/a.cpp", "gemini", True) in written
    assert ("src/b.cpp", "gemini", True) in written
    assert ("src/c.cpp", "small", True) in written
    assert all(p != "src/z.cpp" for p, _, _ in written)
    carried = next(
        r
        for c in repo.insert_rows.await_args_list
        for r in c.args[1]
        if r["scope_path"] == "src/a.cpp" and r["embedding_model"] == "m"
    )
    assert carried["summary"] == "a card" and carried["embedding_dim"] == 2


@pytest.mark.asyncio
async def test_the_card_cap_is_per_pass_across_repositories(monkeypatch):
    """`code_summary_max_scopes_per_pass` bounds the pass's LLM calls, not
    each repository's, so a pass over N repositories cannot write (and pay
    for) N x the cap. The cap is shared out, so the first repository cannot
    spend all of it."""
    from types import SimpleNamespace

    hashes = {"a.cpp": "h1", "b.cpp": "h2"}
    live = [_live("a.cpp", pagerank=0.9), _live("b.cpp", pagerank=0.5)]
    repo = _fake_repo(hashes=hashes, live=live, others=[], churn=[])
    repos = [SimpleNamespace(org="clickhouse", namespace=f"code-{i}") for i in range(3)]
    from server.code.workers import summary as code_summary

    monkeypatch.setattr(code_summary, "_run", _with_repos(code_summary._run, repos))
    res = await _run_worker(repo, monkeypatch, code_summary_max_scopes_per_pass=3)
    assert res.cards_written == 3 and len(_written(repo)) == 3
    # one card each: every repository got its share of the pass
    namespaces = [c.args[1] for c in repo.live_file_hashes.await_args_list]
    assert namespaces == ["code-0", "code-1", "code-2"]


def _with_repos(run, repos):
    """`_run_worker` patches configured_repos to one repository; re-patch it
    to `repos` inside the pass."""

    async def _run(result):
        with patch("server.code.domain.index.repos.configured_repos", return_value=repos):
            await run(result)

    return _run


@pytest.mark.asyncio
async def test_a_share_one_repository_does_not_use_rolls_to_the_next(monkeypatch):
    from types import SimpleNamespace

    from server.code.workers import summary as code_summary

    got: list[tuple[str, int]] = []

    async def fake_repo(org, ns, result, *, cap):
        got.append((ns, cap - result.cards_written))
        result.cards_written += 0 if ns == "quiet" else min(5, cap - result.cards_written)

    repos = [SimpleNamespace(org="o", namespace=n) for n in ("quiet", "busy-1", "busy-2")]
    monkeypatch.setattr(code_summary, "_summarize_repo", fake_repo)
    monkeypatch.setattr(settings, "code_summary_max_scopes_per_pass", 9)
    with patch("server.code.domain.index.repos.configured_repos", return_value=repos):
        res = code_summary.CodeSummaryResult()
        await code_summary._run(res)
    # 9 over 3 = 3 each; quiet used none, so busy-1 may take 9/2 -> 5, busy-2 the last 4
    assert got == [("quiet", 3), ("busy-1", 5), ("busy-2", 4)] and res.cards_written == 9
