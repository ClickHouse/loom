"""memory.search page handles: the ranked tail, parked once."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app import search_pages as sp
from tests.unit._memory_search_pkg import patch_handler_name


def _hit(key: str, rank: float, ns: str = "ns", session: str = "") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"about {key}",
        distance=1 - rank,
        similarity=rank,
        rank_score=rank,
        namespace=ns,
        session_id=session,
    )


def _encode(top_k: int, candidates_total: int, tail: list[_MemorySearchHit], **lists: Any) -> str:
    lists = {"namespaces_searched": ["n"], "namespaces_skipped": [], **lists}
    return sp.encode_page(
        query="q", top_k=top_k, candidates_total=candidates_total, tail=tail, **lists
    )


def test_split_tail_counts_distinct_keys_and_ranks_the_remainder() -> None:
    pool = [_hit("a", 0.9), _hit("b", 0.8), _hit("a", 0.7), _hit("c", 0.6), _hit("d", 0.95)]
    final = [_hit("d", 0.95), _hit("a", 0.9)]
    total, tail = sp.split_tail(pool, final)
    assert total == 4  # a, b, c, d — the duplicate a is one memory
    assert [h.memory_key for h in tail] == ["b", "c"]  # best rank first, served keys gone


def test_split_tail_keeps_the_rows_one_key_holds_in_other_sessions() -> None:
    pool = [_hit("a", 0.9, session="s1"), _hit("a", 0.8, session="s2")]
    total, tail = sp.split_tail(pool, [_hit("a", 0.9, session="s1")])
    assert total == 2 and [h.session_id for h in tail] == ["s2"]


def test_split_tail_counts_a_served_hit_the_pool_never_held() -> None:
    # A recency prepend or an anchor hit can reach the block from outside the pool.
    total, tail = sp.split_tail([_hit("a", 0.9)], [_hit("a", 0.9), _hit("z", 0.1)])
    assert total == 2 and tail == []


def test_encode_page_and_handles_round_trip() -> None:
    content = _encode(2, 7, [_hit("b", 0.8), _hit("c", 0.6)])
    payload = json.loads(content)
    assert payload["v"] == sp.PAGE_VERSION and payload["candidates_total"] == 7
    assert [h["memory_key"] for h in payload["hits"]] == ["b", "c"]
    key = sp.page_blob_key(content)
    assert key.startswith("page/") and len(key) == len("page/") + 20
    assert sp.parse_handle(sp.make_handle(key, 4)) == (key, 4)
    assert sp.parse_handle("blob/abc") is None
    assert sp.parse_handle("page/@3") is None
    assert sp.parse_handle("page/abc@x") is None
    assert sp.parse_handle("") is None


def test_slice_page_reports_the_next_offset_or_exhaustion() -> None:
    payload = {"hits": [{"k": i} for i in range(5)]}
    page, nxt = sp.slice_page(payload, offset=0, top_k=2)
    assert [h["k"] for h in page] == [0, 1] and nxt == 2
    page, nxt = sp.slice_page(payload, offset=4, top_k=2)
    assert [h["k"] for h in page] == [4] and nxt == -1
    page, nxt = sp.slice_page(payload, offset=9, top_k=2)
    assert page == [] and nxt == -1


class _CH:
    def __init__(self, blob: str | None = None) -> None:
        self.blob = blob
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserts.append((table, rows))
        self.settings = settings

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.queries.append((sql, params or {}))
        return {"content": self.blob} if self.blob is not None else None


@pytest.mark.asyncio
async def test_store_page_writes_a_namespace_scoped_json_blob_with_a_ttl() -> None:
    ch = _CH()
    content = _encode(2, 3, [_hit("b", 0.8)])
    key = await sp.store_page(
        ch,
        org="o",
        namespace="n",
        agent="*",
        user_id="-",
        session_id="-",
        run_id="-",
        content=content,
        ttl_s=60,
    )
    table, rows = ch.inserts[0]
    row = rows[0]
    assert table == "loom.context_blobs" and row["blob_key"] == key
    assert (
        row["content_type"] == "application/json" and row["org"] == "o" and row["namespace"] == "n"
    )
    assert (row["expires_at"] - row["created_at"]).total_seconds() == 60
    # The park is fire-and-forget; the key is known before the row lands.
    assert ch.settings == sp.PARK_INSERT_SETTINGS == {"async_insert": 1, "wait_for_async_insert": 0}
    # Content-addressed: the same tail parks under the same key.
    assert (
        await sp.store_page(
            ch,
            org="o",
            namespace="n",
            agent="*",
            user_id="-",
            session_id="-",
            run_id="-",
            content=content,
        )
        == key
    )


@pytest.mark.asyncio
async def test_load_page_reads_by_namespace_and_rejects_foreign_shapes() -> None:
    good = _encode(1, 2, [_hit("b", 0.8)])
    ch = _CH(good)
    payload = await sp.load_page(ch, org="o", namespace="n", blob_key="page/x")
    assert payload and payload["candidates_total"] == 2
    sql, params = ch.queries[0]
    assert "expires_at > now()" in sql and params == {"org": "o", "ns": "n", "key": "page/x"}
    assert await sp.load_page(_CH(None), org="o", namespace="n", blob_key="page/x") is None
    assert await sp.load_page(_CH("not json"), org="o", namespace="n", blob_key="page/x") is None
    assert (
        await sp.load_page(_CH(json.dumps({"v": 99})), org="o", namespace="n", blob_key="page/x")
        is None
    )


@pytest.mark.asyncio
async def test_serve_parked_page_slices_and_hands_back_the_next_handle(monkeypatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search as h

    tail = [_hit("b", 0.8, ns="n"), _hit("c", 0.6, ns="n"), _hit("d", 0.5, ns="n")]
    _callers_namespaces(monkeypatch, ["work", "home"])
    content = _encode(2, 5, tail, namespaces_searched=["n", "work"], namespaces_skipped=["home"])
    key = sp.page_blob_key(content)
    ch = _CH(content)
    patch_handler_name(monkeypatch, "get_client", lambda: ch)

    inp = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q", top_k=2, page=sp.make_handle(key, 0)
    )
    out = await h.handle_memory_search(inp)
    assert [r.memory_key for r in out.results] == ["b", "c"]
    assert out.candidates_total == 5 and out.page_offset == 0
    assert out.next_page == sp.make_handle(key, 2) and out.abstain is False
    # Every page says what the first call read and what the cap left out.
    assert out.namespaces_searched == ["n", "work"] and out.namespaces_skipped == ["home"]
    # No retrieval ran: the only statement was the blob read.
    assert len(ch.queries) == 1 and ch.inserts == []

    out2 = await h.handle_memory_search(inp.model_copy(update={"page": out.next_page}))
    assert (
        [r.memory_key for r in out2.results] == ["d"]
        and out2.next_page == ""
        and out2.page_offset == 2
    )
    assert out2.namespaces_skipped == ["home"]

    # Unknown / expired handle: an empty, clearly labelled page — not a 500.
    gone = await h.handle_memory_search(
        inp.model_copy(update={"page": "page/deadbeefdeadbeefdead@0"})
    )
    ch.blob = None
    gone = await h.handle_memory_search(
        inp.model_copy(update={"page": "page/deadbeefdeadbeefdead@0"})
    )
    assert gone.results == [] and gone.abstain and gone.abstain_code == "page_expired"
    bad = await h.handle_memory_search(inp.model_copy(update={"page": "nonsense"}))
    assert bad.abstain_code == "page_expired"


def _callers_namespaces(monkeypatch: pytest.MonkeyPatch, mine: list[str]) -> list[str]:
    """A caller whose fan-out is `mine` now; returns the orgs each read of it
    was made for."""
    import server.auth.fanout as fanout

    reads: list[str] = []

    async def _mine(org: str, *_: object) -> fanout.Fanout:
        reads.append(org)
        return fanout.Fanout(org=org, namespaces=tuple(mine), cap=32)

    monkeypatch.setattr(fanout, "resolve_fanout", _mine)
    return reads


@pytest.mark.asyncio
async def test_a_parked_page_drops_hits_the_caller_can_no_longer_read(monkeypatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search as h

    tail = [_hit("b", 0.8, ns="n"), _hit("c", 0.7, ns="team"), _hit("d", 0.6, ns="home")]
    content = _encode(
        3, 3, tail, namespaces_searched=["n", "team", "home"], namespaces_skipped=["team", "far"]
    )
    key = sp.page_blob_key(content)
    patch_handler_name(monkeypatch, "get_client", lambda: _CH(content))
    reads = _callers_namespaces(monkeypatch, ["n", "home"])
    page = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q", top_k=3, page=sp.make_handle(key, 0)
    )
    out = await h.handle_memory_search(page)
    assert [(r.memory_key, r.namespace) for r in out.results] == [("b", "n"), ("d", "home")]
    assert out.candidates_total == 2
    assert out.namespaces_searched == ["n", "home"]
    assert out.namespaces_skipped == [], "neither a lost namespace nor one never readable"
    assert reads == ["o"], "one read of the caller's namespaces per page, however many"


@pytest.mark.asyncio
async def test_a_page_reads_on_past_withheld_hits(monkeypatch) -> None:
    """A page whose rows all came from a namespace the caller lost is not the
    end of the list: the page slices on until it has readable hits."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search as h

    tail = [_hit("a", 0.9, ns="team"), _hit("b", 0.8, ns="team"), _hit("c", 0.7, ns="n")]
    tail += [_hit("d", 0.6, ns="n")]
    content = _encode(2, 4, tail)
    key = sp.page_blob_key(content)
    patch_handler_name(monkeypatch, "get_client", lambda: _CH(content))
    _callers_namespaces(monkeypatch, ["n"])
    page = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q", top_k=1, page=sp.make_handle(key, 0)
    )
    out = await h.handle_memory_search(page)
    assert [r.memory_key for r in out.results] == ["c"] and not out.abstain
    assert out.next_page == sp.make_handle(key, 3)
    last = await h.handle_memory_search(page.model_copy(update={"page": out.next_page}))
    assert [r.memory_key for r in last.results] == ["d"] and last.next_page == ""


@pytest.mark.asyncio
async def test_a_page_of_the_addressed_namespace_alone_reads_no_fan_out(monkeypatch) -> None:
    """A `this` search's page names only the namespace the transport just
    authorized: serving it reads no grants, so a failing lookup cannot fail it."""
    import server.auth.fanout as fanout
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search as h

    content = _encode(1, 2, [_hit("b", 0.8, ns="n"), _hit("c", 0.7, ns="n")])
    key = sp.page_blob_key(content)
    patch_handler_name(monkeypatch, "get_client", lambda: _CH(content))

    async def _down(org: str, *_: object) -> fanout.Fanout:
        raise AssertionError("no fan-out read for an addressed-only page")

    monkeypatch.setattr(fanout, "resolve_fanout", _down)
    page = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q", top_k=2, page=sp.make_handle(key, 0)
    )
    out = await h.handle_memory_search(page)
    assert [r.memory_key for r in out.results] == ["b", "c"]


@pytest.mark.asyncio
async def test_a_page_whose_fan_out_cannot_be_read_serves_the_addressed_namespace(
    monkeypatch,
) -> None:
    """Fails closed: this page serves the addressed namespace's hits only, and
    later pages carry on past the withheld ones."""
    import server.auth.fanout as fanout
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search as h

    content = _encode(1, 2, [_hit("b", 0.8, ns="n"), _hit("c", 0.7, ns="team")])
    key = sp.page_blob_key(content)
    patch_handler_name(monkeypatch, "get_client", lambda: _CH(content))

    async def _down(org: str, *_: object) -> fanout.Fanout:
        raise RuntimeError("grants unreadable")

    monkeypatch.setattr(fanout, "resolve_fanout", _down)
    page = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q", top_k=2, page=sp.make_handle(key, 0)
    )
    out = await h.handle_memory_search(page)
    assert [r.memory_key for r in out.results] == ["b"]
