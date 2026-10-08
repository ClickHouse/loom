"""The packed evidence block's order and date labels.

* Retrieval returns the caller-requested top_k in rank order (no adaptive-k
  cutoff, which starves questions that need breadth).
* The packed block keeps RELEVANCE (score-descending) order, never a
  chronological reorder that would bury the top item mid-context.
* The EVENT date (temporal_anchor) rides each line as a label
  (date=YYYY-MM-DD) without changing order.
"""

from __future__ import annotations

from server.memory.domain.evidence_pack import EvidenceItem, pack_evidence


def _ev(citation: str, score: float, anchor: str) -> EvidenceItem:
    return EvidenceItem(
        citation=citation,
        text=f"fact {citation}",
        source="fact",
        group=citation,
        score=score,
        temporal_anchor=anchor,
    )


def test_pack_keeps_relevance_order() -> None:
    # No chronological reorder: the packed block is score-descending, so the
    # top-relevance item leads (it is NOT buried mid-context by a date sort).
    items = [
        _ev("c", score=0.9, anchor="2024-06-01"),  # strongest
        _ev("a", score=0.5, anchor="2022-01-01"),
        _ev("b", score=0.7, anchor="2023-03-15"),
    ]
    packed = pack_evidence(items, char_budget=2000)
    lines = packed.rendered.splitlines()
    assert "fact c" in lines[0]
    assert "fact b" in lines[1]
    assert "fact a" in lines[2]


def test_pack_renders_event_date_label_without_reordering() -> None:
    # B1 (kept): the event date is rendered on the line (date=YYYY-MM-DD) as a
    # label so the reader sees WHEN each fact happened — but it is a label, not
    # a sort key. Relevance order is preserved.
    items = [
        _ev("strong", score=0.9, anchor="2023-04-29"),
        _ev("weak", score=0.4, anchor="2024-12-31"),
    ]
    packed = pack_evidence(items, char_budget=2000)
    lines = packed.rendered.splitlines()
    # Date label present for both.
    assert "date=2023-04-29" in lines[0]
    assert "date=2024-12-31" in lines[1]
    # Relevance order held despite the later event date on the weaker item.
    assert "fact strong" in lines[0]
    assert "fact weak" in lines[1]
