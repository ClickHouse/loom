"""Bodies="source" serves a fact hit its linked evidence
passage — once per evidence id, by rank, within the body budget, under the
request's run."""

from types import SimpleNamespace

import pytest

from server.memory.app.search import payload as h


class _FakeCH:
    def __init__(self, links, passages):
        self.links, self.passages, self.sql = links, passages, []

    async def query_all(self, sql, params=None):
        self.sql.append((sql, params))
        if "memory_belief_sources" in sql:
            return self.links
        return self.passages


def _hit(key, ns="ns", ents=None):
    return SimpleNamespace(
        memory_key=key,
        namespace=ns,
        source="fact",
        entities=ents or [],
        content_excerpt=f"fact {key}",
        score_components={},
        body_truncated=False,
    )


def _inp(**kw):
    base = dict(org="o", namespace="ns", bodies="source", body_budget_tokens=100, run_id="r1")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_one_passage_per_evidence_by_rank_within_budget(monkeypatch):
    links = [
        {"namespace": "ns", "memory_key": "a", "evidence_id": "e1"},
        {"namespace": "ns", "memory_key": "b", "evidence_id": "e1"},
        {"namespace": "ns", "memory_key": "c", "evidence_id": "e2"},
        {"namespace": "ns", "memory_key": "d", "evidence_id": "e3"},
    ]
    passages = [
        {"eid": "e1", "body": "T" * 200},
        {"eid": "e2", "body": "U" * 200},
        {"eid": "e3", "body": "V" * 200},
    ]
    ch = _FakeCH(links, passages)
    monkeypatch.setattr(h, "get_client", lambda: ch)
    hits = [_hit("a"), _hit("b"), _hit("c"), _hit("d")]
    served, over = await h._expand_source_bodies(hits, _inp())  # budget 100 tokens = 400 chars
    assert served == 2 and over == 1
    assert hits[0].content_excerpt == "T" * 200 and hits[0].score_components["source_body"] == 1.0
    assert hits[1].content_excerpt == "fact b", "second hit on the same passage keeps its fact"
    assert hits[2].content_excerpt == "U" * 200
    assert hits[3].content_excerpt == "fact d", "over budget: fact text stays"
    ev_sql, ev_params = ch.sql[1]
    assert "run_id = {run_id:String}" in ev_sql and ev_params["run_id"] == "r1"
    # No FINAL on either read; the version column decides
    link_sql, _ = ch.sql[0]
    assert "FINAL" not in link_sql and "FINAL" not in ev_sql
    assert "argMax(content, created_at)" in ev_sql and "HAVING tomb_latest IS NULL" in ev_sql
    assert "Array(UUID)" in ev_sql


@pytest.mark.asyncio
async def test_cited_entities_hits_spend_first_and_other_modes_are_no_ops(monkeypatch):
    links = [
        {"namespace": "ns", "memory_key": "a", "evidence_id": "e1"},
        {"namespace": "ns", "memory_key": "z", "evidence_id": "e9"},
    ]
    passages = [
        {"eid": "e1", "body": "T" * 200},
        {"eid": "e9", "body": "Z" * 200},
    ]
    ch = _FakeCH(links, passages)
    monkeypatch.setattr(h, "get_client", lambda: ch)
    hits = [_hit("a"), _hit("z", ents=["zoey"])]
    served, over = await h._expand_source_bodies(
        hits, _inp(body_budget_tokens=50), prefer_entities=["zoey"]
    )
    assert served == 1 and over == 1 and hits[1].content_excerpt == "Z" * 200
    assert await h._expand_source_bodies([_hit("a")], _inp(bodies="unit")) == (0, 0)
    assert await h._expand_source_bodies([], _inp()) == (0, 0)


@pytest.mark.asyncio
async def test_no_links_or_read_failure_leaves_hits_untouched(monkeypatch):
    monkeypatch.setattr(h, "get_client", lambda: _FakeCH([], []))
    hits = [_hit("a")]
    assert await h._expand_source_bodies(hits, _inp()) == (0, 0)
    assert hits[0].content_excerpt == "fact a"

    class _Boom:
        async def query_all(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(h, "get_client", lambda: _Boom())
    assert await h._expand_source_bodies(hits, _inp()) == (0, 0)


@pytest.mark.asyncio
async def test_a_scoped_read_serves_its_own_users_passage_not_the_oldest_link(monkeypatch):
    """memory_belief_sources has no user or session column: key K's oldest link may be
    another user's transcript turn. Under a user / session read scope the passage read is
    bound to that scope and the key's next link stands in."""
    links = [
        {"namespace": "ns", "memory_key": "k", "evidence_id": "e-other", "first_at": 1},
        {"namespace": "ns", "memory_key": "k", "evidence_id": "e-mine", "first_at": 2},
    ]

    class _Scoped(_FakeCH):
        async def query_all(self, sql, params=None):
            self.sql.append((sql, params))
            if "memory_belief_sources" in sql:
                return self.links
            # the passage read answers only rows of the bound user
            assert params.get("uid") == "alice" and params.get("sid") == "s1"
            return [{"eid": "e-mine", "body": "mine"}]

    ch = _Scoped(links, [])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    hits = [_hit("k")]
    served, _ = await h._expand_source_bodies(hits, _inp(user_id="alice", session_id="s1"))
    assert served == 1 and hits[0].content_excerpt == "mine"
    ev_sql, _ = ch.sql[1]
    assert "user_id = {uid:String}" in ev_sql and "session_id = {sid:String}" in ev_sql
