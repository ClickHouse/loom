"""`memory.search` hands back the envelope signals a caller acts on, not just
the hits: a pending extraction, a degraded provider, the run's next step."""

from __future__ import annotations

from loom.memory import _search_result


def test_envelope_signals_ride_the_result() -> None:
    res = _search_result(
        {
            "results": [],
            "pending_extraction": 2,
            "degraded": ["rerank: input_order_kept (ReadTimeout)"],
            "next": "report the hits you used in run.complete",
        }
    )
    assert res.pending_extraction == 2
    assert res.degraded == ("rerank: input_order_kept (ReadTimeout)",)
    assert res.next == "report the hits you used in run.complete"


def test_an_older_server_leaves_the_defaults() -> None:
    res = _search_result({"results": []})
    assert (res.pending_extraction, res.degraded, res.next) == (0, (), "")
