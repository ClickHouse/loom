"""The next page is a point read of what was ranked: the
cursor round-trips and rejects tampering, an expired cursor falls back to
a disclosed re-search, the seen-set prunes the fused list before
diversification, and a continuation page never touches the embedder."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_search import CodeSearchInput
from server.code.app import grep
from server.code.app import search_cursor as paging
from server.code.app.search import handler as h
from server.code.app.search import shared
from server.code.domain.units import CodeUnit
from server.core.errors import ValidationError
from server.settings import settings


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(settings, "code_trace_log_enabled", False)
    monkeypatch.setattr(settings, "code_risk_annotations", False)
    monkeypatch.setattr(settings, "code_search_cursor_enabled", True)


def _inp(**kw: Any) -> CodeSearchInput:
    base: dict[str, Any] = dict(query="how are background merges scheduled", top_k=2)
    base.update(kw)
    return CodeSearchInput(**base)


def _ph(inp: CodeSearchInput) -> str:
    return paging.params_hash(inp, rerank="", embedding_model="m")


# ---- cursor encoding -------------------------------------------------------


def test_cursor_round_trips_and_binds_kind_and_params() -> None:
    inp = _inp()
    c = paging.encode(kind="s", run_id="r1", offset=5, params=_ph(inp), indexed_commit="a" * 40)
    f = paging.decode(c, kind="s", params=_ph(inp))
    assert (f["r"], f["o"], f["c"]) == ("r1", 5, "a" * 12)
    assert not paging.expired(f)
    with pytest.raises(paging.CursorError):
        paging.decode(c, kind="g", params=_ph(inp))  # another operation
    with pytest.raises(paging.CursorError):
        paging.decode(c, kind="s", params=_ph(_inp(query="something else")))
    # page size and body fill are not ranking: the same cursor serves them
    assert _ph(_inp(top_k=10, token_budget=8000, bodies="none")) == _ph(inp)
    assert _ph(_inp(path_prefix="src/")) != _ph(inp)
    assert paging.params_hash(inp, rerank="builtin:openai", embedding_model="m") != _ph(inp)


def test_tampered_cursor_is_rejected() -> None:
    import base64
    import json

    inp = _inp()
    c = paging.encode(kind="s", run_id="r1", offset=5, params=_ph(inp), indexed_commit="")
    raw = json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))
    raw["o"] = 50  # move the offset without re-signing
    forged = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    with pytest.raises(paging.CursorError, match="checksum"):
        paging.decode(forged, kind="s", params=_ph(inp))
    with pytest.raises(paging.CursorError):
        paging.decode("not-a-cursor", kind="s", params=_ph(inp))


def test_cursors_are_signed_with_the_active_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a key map set, `jwt_secret` is not the signing key: a cursor keyed
    on it is not one this server issued, and a rotation retires old cursors."""
    inp = _inp()

    def _cursor() -> str:
        return paging.encode(kind="s", run_id="r1", offset=5, params=_ph(inp), indexed_commit="")

    monkeypatch.setattr(settings, "jwt_secret", "flat-secret")
    monkeypatch.setattr(settings, "jwt_secrets_json", "")
    keyed_on_flat = _cursor()
    monkeypatch.setattr(settings, "jwt_secrets_json", '{"k1": "old-key", "k2": "new-key"}')
    monkeypatch.setattr(settings, "jwt_active_kid", "k2")
    with pytest.raises(paging.CursorError, match="checksum"):
        paging.decode(keyed_on_flat, kind="s", params=_ph(inp))
    issued = _cursor()
    assert paging.decode(issued, kind="s", params=_ph(inp))["o"] == 5
    monkeypatch.setattr(settings, "jwt_active_kid", "k1")
    with pytest.raises(paging.CursorError, match="checksum"):
        paging.decode(issued, kind="s", params=_ph(inp))


def test_expiry_is_anchored_at_issue_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_search_memo_ttl_s", 60)
    f = {"t": int(time.time()) - 120}
    assert paging.expired(f)
    assert not paging.expired({"t": int(time.time())})


# ---- paging over a pool ----------------------------------------------------


def _pool(n: int) -> list[dict[str, Any]]:
    return [{"id": f"c{i}", "path": f"src/f{i}.cpp", "score": 1.0 - i / 100} for i in range(n)]


def test_take_page_skips_excluded_and_keeps_pool_positions() -> None:
    entries, nxt, more = paging.take_page(_pool(10), offset=2, size=3, exclude={"c3"})
    assert [e["id"] for e in entries] == ["c2", "c4", "c5"]
    assert nxt == 6 and more
    entries, nxt, more = paging.take_page(_pool(10), offset=8, size=5, exclude=set())
    assert [e["id"] for e in entries] == ["c8", "c9"] and nxt == 10 and not more


def test_rebuild_drops_units_whose_chunk_is_gone() -> None:
    entries = _pool(3)
    rows = [
        {
            "chunk_id": "c0",
            "path": "src/f0.cpp",
            "content": "int a;",
            "start_line": 1,
            "end_line": 2,
            "scope": "a",
            "kind": "function",
        },
        {
            "chunk_id": "c2",
            "path": "src/f2.cpp",
            "content": "int c;",
            "start_line": 5,
            "end_line": 6,
            "scope": "",
            "kind": "other",
        },
    ]
    units = paging.rebuild_units(entries, rows)
    assert [u.unit_id for u in units] == ["c0", "c2"]
    assert units[0].score == 1.0 and units[0].body == "int a;" and units[1].symbol == ""


def test_disclose_marks_drift_and_fallback() -> None:
    d = paging.disclose({"commit": "b" * 40}, page_commit="a" * 40, offset=5, fallback="")
    assert d["page_stale"] is True and d["page_offset"] == 5 and "page_fallback" not in d
    d = paging.disclose({"commit": "a" * 40}, page_commit="a" * 40, offset=0, fallback="expired")
    assert d["page_stale"] is False and d["page_fallback"] == "expired"


# ---- exclude_unit_ids in the search service -------------------------------


def test_exclude_filters_before_diversify() -> None:
    from server.code.app.search.window import _exclude

    units = [CodeUnit(unit_id=f"c{i}", path="p", score=1.0) for i in range(5)]
    work: dict[str, int] = {}
    kept = _exclude(units, ["c1", "c3", "zzz"], work)
    assert [u.unit_id for u in kept] == ["c0", "c2", "c4"]
    assert work["excluded_units"] == 2


# ---- handler: page 2 is a point read with zero embeddings ------------------


class _Repo:
    def __init__(self) -> None:
        self.key_reads: list[list[tuple[str, str]]] = []

    async def chunks_by_keys(self, org, ns, model, keys):
        self.key_reads.append(list(keys))
        return [
            {
                "chunk_id": cid,
                "path": path,
                "content": f"body of {cid}",
                "start_line": 1,
                "end_line": 3,
                "scope": f"fn_{cid}",
                "kind": "function",
            }
            for path, cid in keys
        ]

    async def all_state(self, org, ns):
        return {}


class _Embedder:
    model_name = "m"
    calls = 0

    async def embed(self, text):
        _Embedder.calls += 1
        return [0.0]


def _payload(n: int) -> dict[str, Any]:
    return {
        "v": 1,
        "kind": "s",
        "query": "q",
        "route": "hybrid",
        "model": "m",
        "params": "x",
        "served": 2,
        "pool": _pool(n),
        "calib": {
            "retrieval_risk": "medium",
            "retrieval_confidence": 0.7,
            "route_agreement": 0.5,
            "calibration_explanation": "e",
            "top_similarity": 0.61,
            "score_explanation": "rrf",
        },
    }


def _drive(monkeypatch: pytest.MonkeyPatch, inp: CodeSearchInput, row: dict[str, Any] | None):
    repo = _Repo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "_embedding_model_name", lambda: "m")

    async def _as_of(repo_, org, ns):
        return {"commit": "b" * 40}

    async def _get_pool(org, ns, key):
        _get_pool.keys.append((org, ns, key))  # type: ignore[attr-defined]
        return row

    _get_pool.keys = []  # type: ignore[attr-defined]
    monkeypatch.setattr(h, "index_as_of", _as_of)
    monkeypatch.setattr(paging, "get_pool", _get_pool)

    async def _no_search(*a, **k):
        raise AssertionError("a continuation page must not run the legs")

    import server.code.app.search as svc

    monkeypatch.setattr(svc, "search", _no_search)
    from server.embedding import embedder as emb

    monkeypatch.setattr(emb, "get_embedder", lambda: _Embedder())
    _Embedder.calls = 0
    return repo, _get_pool


def test_page_two_is_a_point_read_with_zero_embedding_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inp = _inp(top_k=2, exclude_unit_ids=["c3"])
    ph = paging.params_hash(inp, rerank="", embedding_model="m")
    cursor = paging.encode(kind="s", run_id="run1", offset=2, params=ph, indexed_commit="a" * 40)
    row = {"payload": _payload(6), "indexed_commit": "a" * 40, "created_at": None}
    repo, get_pool = _drive(monkeypatch, inp.model_copy(update={"cursor": cursor}), row)

    out = asyncio.run(h.handle_code_search(inp.model_copy(update={"cursor": cursor})))

    assert get_pool.keys == [("o", "n", "cursor:s:run1")]  # one point read, identity-scoped
    assert repo.key_reads == [[("src/f2.cpp", "c2"), ("src/f4.cpp", "c4")]]  # by full key
    assert [u.unit_id for u in out.results] == ["c2", "c4"]
    assert out.results[0].body == "body of c2" and out.results[0].score == 0.98
    assert out.route == "hybrid+page" and out.retrieval_risk == "medium"
    assert out.retrieval_work["embedding_calls"] == 0 and _Embedder.calls == 0
    assert out.as_of["page_offset"] == 2 and out.as_of["page_stale"] is True
    assert out.as_of["page_commit"] == "a" * 40
    assert out.has_more and out.next_cursor
    nxt = paging.decode(out.next_cursor, kind="s", params=ph)
    assert nxt["o"] == 5 and nxt["r"] == "run1"


def test_last_page_has_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    inp = _inp(top_k=5)
    ph = paging.params_hash(inp, rerank="", embedding_model="m")
    cursor = paging.encode(kind="s", run_id="run1", offset=4, params=ph, indexed_commit="")
    row = {"payload": _payload(6), "indexed_commit": "", "created_at": None}
    _drive(monkeypatch, inp, row)
    out = asyncio.run(h.handle_code_search(inp.model_copy(update={"cursor": cursor})))
    assert [u.unit_id for u in out.results] == ["c4", "c5"]
    assert not out.has_more and out.next_cursor == "" and "page_stale" not in out.as_of


def test_page_plan_is_route_aware_and_honours_an_explicit_budget() -> None:
    assert shared.page_plan(_inp(query="MergeTreeData::loadDataParts")).token_budget == 1200
    assert shared.page_plan(_inp()).token_budget == 3000
    assert shared.page_plan(_inp(query="MergeTreeData::loadDataParts", token_budget=500)) == (
        shared.PagePlan(token_budget=500, bodies="auto", annotate=False, reserve=0)
    )


def test_page_two_of_a_symbol_query_is_budgeted_as_page_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symbol-shaped query that fell through to hybrid was budgeted at 1200 on page 1;
    its continuation page fills bodies under the same budget."""
    from server.code.domain import units as units_mod

    inp = _inp(query="MergeTreeData::loadDataParts", top_k=2)
    cursor = paging.encode(kind="s", run_id="run1", offset=2, params=_ph(inp), indexed_commit="")
    row = {"payload": _payload(6), "indexed_commit": "", "created_at": None}
    _drive(monkeypatch, inp, row)
    budgets: list[int] = []
    real = units_mod.budget_units

    def spy(units: Any, token_budget: int, **kw: Any) -> Any:
        budgets.append(token_budget)
        return real(units, token_budget, **kw)

    monkeypatch.setattr(units_mod, "budget_units", spy)
    asyncio.run(h.handle_code_search(inp.model_copy(update={"cursor": cursor})))
    assert budgets == [shared.page_plan(inp).token_budget] == [1200]


def test_cursor_for_another_query_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _inp(query="something else entirely")
    cursor = paging.encode(
        kind="s",
        run_id="run1",
        offset=2,
        params=paging.params_hash(other, rerank="", embedding_model="m"),
        indexed_commit="",
    )
    inp = _inp(cursor=cursor)
    _drive(monkeypatch, inp, {"payload": _payload(6), "indexed_commit": "", "created_at": None})
    with pytest.raises(ValidationError, match="invalid cursor"):
        asyncio.run(h.handle_code_search(inp))


def test_missing_pool_row_falls_back_to_a_disclosed_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inp = _inp(top_k=2)
    ph = paging.params_hash(inp, rerank="", embedding_model="m")
    cursor = paging.encode(kind="s", run_id="gone", offset=2, params=ph, indexed_commit="")
    _drive(monkeypatch, inp, None)
    seen: list[CodeSearchInput] = []

    async def _fresh(fresh_inp: CodeSearchInput):
        from server.code.api.models_search import CodeSearchOutput

        seen.append(fresh_inp)
        units = [CodeUnit(unit_id=f"c{i}", path="p", score=1.0, body="b") for i in range(4)]
        return CodeSearchOutput(
            results=units,
            route="hybrid",
            next_cursor="n",
            has_more=True,
            as_of={"commit": "b" * 40},
        )

    monkeypatch.setattr(h, "handle_code_search", _fresh)
    out = asyncio.run(
        h._page_by_research(
            inp.model_copy(update={"cursor": cursor}), offset=2, reason="pool_not_found"
        )
    )
    assert seen[0].cursor == "" and seen[0].top_k == 4 and seen[0].fresh is True
    assert seen[0].token_budget == 6000  # scaled with the depth
    assert [u.unit_id for u in out.results] == ["c2", "c3"]
    assert out.route == "hybrid+page_fallback"
    assert out.as_of["page_fallback"] == "pool_not_found" and out.as_of["page_offset"] == 2
    assert out.next_cursor == "n"


def test_issue_skips_abstain_symbol_routes_and_exhausted_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.search import CodeSearchResult

    puts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        paging, "put_pool", lambda org, ns, key, payload, **kw: puts.append((key, payload))
    )
    units = [CodeUnit(unit_id=f"c{i}", path=f"p{i}", score=1.0, body="b") for i in range(6)]
    res = CodeSearchResult(units=units[:2], pool=units, route="hybrid")
    inp = _inp(top_k=2)
    c = h._issue_search_cursor(
        inp, "o", "n", res, run_id="r", embedding_model="m", indexed_commit="a" * 40
    )
    assert c and puts[0][0] == "cursor:s:r"
    assert [e["id"] for e in puts[0][1]["pool"]] == [f"c{i}" for i in range(6)]
    assert all("body" not in e for e in puts[0][1]["pool"])  # ids and headers, never bodies
    f = paging.decode(
        c,
        kind="s",
        params=paging.params_hash(inp, rerank="", embedding_model="m"),
    )
    assert f["o"] == 2
    # nothing past the page / abstained / symbol route (no pool) / tier off
    assert (
        h._issue_search_cursor(
            inp,
            "o",
            "n",
            CodeSearchResult(units=units, pool=units),
            run_id="r",
            embedding_model="m",
            indexed_commit="",
        )
        == ""
    )
    assert (
        h._issue_search_cursor(
            inp,
            "o",
            "n",
            CodeSearchResult(units=[], pool=units, abstain=True),
            run_id="r",
            embedding_model="m",
            indexed_commit="",
        )
        == ""
    )
    assert (
        h._issue_search_cursor(
            inp,
            "o",
            "n",
            CodeSearchResult(units=units[:2], route="symbol_exact"),
            run_id="r",
            embedding_model="m",
            indexed_commit="",
        )
        == ""
    )
    monkeypatch.setattr(settings, "code_search_cursor_enabled", False)
    assert (
        h._issue_search_cursor(
            inp, "o", "n", res, run_id="r", embedding_model="m", indexed_commit=""
        )
        == ""
    )
    assert len(puts) == 1


# ---- grep ------------------------------------------------------------------


class _GrepRepo:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    async def grep_chunks(self, org, ns, pattern, **kw):
        return self._chunks

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def all_state(self, org, ns):
        return {}


def _chunk(path: str, n_lines: int, start: int = 1) -> dict[str, Any]:
    return {
        "path": path,
        "start_line": start,
        "end_line": start + n_lines - 1,
        "scope": "",
        "symbol_ids": [],
        "content": "\n".join(f"needle {i}" for i in range(n_lines)),
    }


def test_grep_excludes_paths_and_issues_a_cursor_for_the_extracted_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _GrepRepo([_chunk("a.cpp", 3), _chunk("skip.cpp", 3), _chunk("b.cpp", 3)])
    monkeypatch.setattr(grep, "CodeRepo", lambda: repo)

    async def _as_of(repo_, org, ns):
        return {}

    monkeypatch.setattr(grep, "index_as_of", _as_of)
    puts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        paging, "put_pool", lambda org, ns, key, payload, **kw: puts.append(payload)
    )

    inp = CodeGrepInput(pattern="needle", limit=4, exclude_unit_ids=["skip.cpp"])
    out = asyncio.run(grep.handle_code_grep(inp))
    assert len(out.matches) == 4 and {m["path"] for m in out.matches} == {"a.cpp", "b.cpp"}
    assert out.has_more and out.next_cursor and out.truncated
    assert len(puts) == 1 and len(puts[0]["matches"]) == 6  # every extracted line, none skipped

    # page 2 from the stored lines: one point read, no chunk scan
    async def _get_pool(org, ns, key):
        assert key.startswith("cursor:g:")
        return {"payload": puts[0], "indexed_commit": "", "created_at": None}

    monkeypatch.setattr(paging, "get_pool", _get_pool)

    async def _no_grep(*a, **k):
        raise AssertionError("a grep page must not rescan chunks")

    repo.grep_chunks = _no_grep  # type: ignore[method-assign]
    page = asyncio.run(grep.handle_code_grep(inp.model_copy(update={"cursor": out.next_cursor})))
    assert [m["line"] for m in page.matches] == [2, 3] and page.matches[0]["path"] == "b.cpp"
    assert not page.has_more and page.as_of["page_offset"] == 4
    assert page.chunks_matched == 3


def test_grep_cursor_for_another_pattern_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grep, "CodeRepo", lambda: _GrepRepo([]))
    c = paging.encode(
        kind="g",
        run_id="r",
        offset=4,
        params=grep._grep_params_hash(CodeGrepInput(pattern="other")),
        indexed_commit="",
    )
    with pytest.raises(ValidationError, match="invalid cursor"):
        asyncio.run(grep.handle_code_grep(CodeGrepInput(pattern="needle", cursor=c)))
