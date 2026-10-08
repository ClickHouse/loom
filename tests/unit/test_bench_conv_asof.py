"""The bench's conv_asof parses a conversation's last session date."""

from __future__ import annotations


def test_bench_conv_asof_parses_last_session() -> None:
    from bench.locomo_qa import conv_asof

    conv = {
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_2_date_time": "12:19 am on 4 January, 2024",
        "session_1": [],
    }
    assert conv_asof(conv) == "2024-01-04T00:19:00"
    assert conv_asof({}) is None
    assert conv_asof(None) is None
