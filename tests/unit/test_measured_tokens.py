"""The measured tokens a value-update link compares."""

from server.memory.app.value_updates import measured_tokens


def test_measured_tokens_read_spelled_measures_and_months() -> None:
    t = measured_tokens(
        "User finished 'The Nightingale', which took them three weeks; paid $40 on 2023-05-24 in March"
    )
    assert "three_weeks" in t and "$40" in t and "mar" in t and "2023-05-24" in t or "2023" in t
