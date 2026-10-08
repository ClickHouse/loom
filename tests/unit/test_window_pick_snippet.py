"""Span capsules."""

from types import SimpleNamespace

import pytest

from server.memory.app.search import payload as h


class _FakeCH:
    def __init__(self, links, passages):
        self.links, self.passages = links, passages

    async def query_all(self, sql, params=None):
        return self.links if "memory_belief_sources" in sql else self.passages


@pytest.mark.asyncio
async def test_span_capsule_is_a_window_around_the_linked_span(monkeypatch):
    text = "A" * 1000 + "SPAN" + "B" * 1000
    links = [
        {
            "namespace": "ns",
            "memory_key": "k",
            "evidence_id": "e1",
            "char_start": 1000,
            "char_end": 1004,
        }
    ]
    monkeypatch.setattr(h, "get_client", lambda: _FakeCH(links, [{"eid": "e1", "body": text}]))
    hit = SimpleNamespace(
        memory_key="k",
        namespace="ns",
        source="fact",
        entities=[],
        content_excerpt="fact",
        score_components={},
        body_truncated=False,
    )
    inp = SimpleNamespace(
        org="o",
        namespace="ns",
        bodies="source",
        body_budget_tokens=32000,
        run_id="",
        source_span_chars=200,
    )
    served, over = await h._expand_source_bodies([hit], inp)
    assert served == 1 and len(hit.content_excerpt) == 200 and "SPAN" in hit.content_excerpt
    # 0 = whole passage
    hit2 = SimpleNamespace(
        memory_key="k",
        namespace="ns",
        source="fact",
        entities=[],
        content_excerpt="fact",
        score_components={},
        body_truncated=False,
    )
    inp0 = SimpleNamespace(
        org="o",
        namespace="ns",
        bodies="source",
        body_budget_tokens=32000,
        run_id="",
        source_span_chars=0,
    )
    monkeypatch.setattr(h, "get_client", lambda: _FakeCH(links, [{"eid": "e1", "body": text}]))
    await h._expand_source_bodies([hit2], inp0)
    assert len(hit2.content_excerpt) == len(text)
