"""The day written into a row is not the value of an attribute."""

from __future__ import annotations

from server.memory.app.value_updates import is_value_update
from server.memory.domain.conflicts import date_tokens

START = "User started a solo camping trip to Yosemite National Park on 2023-05-15 and is excited."
BACK = (
    "User just got back from an amazing solo camping trip to Yosemite National Park on 2023-05-17."
)


def test_two_dated_events_are_not_an_update() -> None:
    assert date_tokens(START) == {"2023", "05", "15"}
    assert not is_value_update(START, BACK)


def test_a_real_value_update_still_links() -> None:
    a = "User's personal best in the 5k is 27:12 (on 2023-05-23)."
    b = "User's personal best in the 5k is 25:50 (on 2023-05-30)."
    assert is_value_update(a, b)


def test_a_value_update_with_no_dates_is_unchanged() -> None:
    assert is_value_update("User's rent is $1,200 a month.", "User's rent is $1,350 a month.")
    assert not is_value_update("User's rent is $1,200 a month.", "User's rent is $1,200 a month.")
