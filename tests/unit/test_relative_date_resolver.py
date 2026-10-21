"""Table-driven tests for the deterministic relative-date resolver and the
temporal-anchor backstop it powers.

The resolver is the write-side guarantee that closed-class relative
qualifiers ("today", "last weekend", "about a month ago") always yield a
dated memory even when the extraction LLM keeps the phrase verbatim. The
contract — resolve_relative_date(text, observation_date) -> "YYYY-MM-DD"
or None — is kept stable so query-side intent parsing can share it.
"""

from __future__ import annotations

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import (
    extract_from_messages,
    resolve_relative_date,
)

# observation_date 2024-03-21 is a Thursday.
_OBS = "2024-03-21"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # same-day forms
        ("Went to the farmers market today.", "2024-03-21"),
        ("We had pizza tonight.", "2024-03-21"),
        ("Saw my physiotherapist this morning.", "2024-03-21"),
        ("Earlier today I fixed the bug.", "2024-03-21"),
        # yesterday family
        ("I bought a helmet yesterday.", "2024-03-20"),
        ("Watched a movie last night.", "2024-03-20"),
        ("It rained the day before yesterday.", "2024-03-19"),
        # weekend / weekday
        ("We repainted the kitchen last weekend.", "2024-03-16"),
        ("Attended a BBQ last Saturday.", "2024-03-16"),
        # "last thursday" when obs IS Thursday -> strictly previous week
        ("Had a checkup last Thursday.", "2024-03-14"),
        ("Dinner reservation next Friday.", "2024-03-22"),
        # N-units-ago, digits and words, with hedges
        ("Adopted a kitten two days ago.", "2024-03-19"),
        ("Started the course 3 weeks ago.", "2024-02-29"),
        ("I moved here about a month ago.", "2024-02-21"),
        ("Joined the gym a couple of months ago.", "2024-01-21"),
        ("Bought the car a year ago.", "2023-03-21"),
        ("Visited around six months ago.", "2023-09-21"),
        # bare last/next units
        ("Submitted the form last week.", "2024-03-14"),
        ("Quit the job last month.", "2024-02-21"),
        ("Graduated last year.", "2023-03-21"),
        ("Flying out tomorrow.", "2024-03-22"),
        ("The release ships next week.", "2024-03-28"),
        ("Lease renews next month.", "2024-04-21"),
        ("The wedding is next year.", "2025-03-21"),
        # B2 named-month closed class — first of the most recent matching
        # month ON OR BEFORE obs (2024-03-21). January < March -> this year.
        ("My sister had her baby back in January.", "2024-01-01"),
        ("We moved in February.", "2024-02-01"),
        # September is AFTER March -> last year's September (no year drift).
        ("I started the job in September.", "2023-09-01"),
        # abbreviations resolve too
        ("Closed on the house in Jan.", "2024-01-01"),
        # an explicit month+day or month+year is NOT the bare-month case:
        # the prompt resolves those; the backstop must leave them alone.
        ("Flew on March 15, 2023.", None),
        ("It happened in March 2022.", None),
        # a bare month with no temporal preposition is not anchored.
        ("January is my favorite month.", None),
        # no closed-class qualifier -> None (never guess)
        ("I recently picked up climbing.", None),
        ("I did that a while back.", None),
        ("User loves Italian food.", None),
    ],
)
def test_resolver_table(text: str, expected: str | None) -> None:
    assert resolve_relative_date(text, _OBS) == expected


def test_resolver_closed_class_beats_named_month() -> None:
    """A more specific closed-class qualifier wins over a bare month name in
    the same text — "last weekend" resolves to the weekend, not the month."""
    # obs 2024-03-21; "last weekend" -> 2024-03-16 (the specific form), even
    # though "March" also appears.
    assert resolve_relative_date("We painted last weekend in March.", _OBS) == "2024-03-16"


def test_resolver_obs_in_january_named_month_no_year_off() -> None:
    """The Class-D year-off guard: obs in January, "in January" resolves to
    THIS January (same month), never the prior year."""
    assert resolve_relative_date("Started in January.", "2024-01-15") == "2024-01-01"


def test_resolver_month_end_clamps() -> None:
    # 2024-03-31 minus one calendar month clamps to leap-day Feb 29.
    assert resolve_relative_date("paid rent a month ago", "2024-03-31") == "2024-02-29"


def test_resolver_unparseable_observation_date() -> None:
    assert resolve_relative_date("yesterday", "not-a-date") is None
    assert resolve_relative_date("yesterday", "") is None


def test_resolver_does_not_match_inside_words() -> None:
    # "curfew" must not match the "few ... ago" pattern.
    assert resolve_relative_date("the curfew days ago story", _OBS) is None


class _ScriptedClient:
    """LLM stub returning a fixed extraction payload."""

    provider = "scripted"
    model = "scripted-model"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls.append({"system": system, "max_tokens": max_tokens})
        return LLMResult(
            text=self._text,
            tokens_in=10,
            tokens_out=5,
            provider=self.provider,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_backstop_anchors_unanchored_qualifier_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction left temporal_anchor empty on a fact whose value carries
    a closed-class qualifier -> the backstop resolves it deterministically
    against observation_date."""
    payload = (
        '{"facts": ['
        '{"key": "k1", "value": "User repainted the kitchen last weekend."},'
        '{"key": "k2", "value": "User loves Italian food."},'
        '{"key": "k3", "value": "User flew on 2024-02-14.",'
        ' "temporal_anchor": "2024-02-14"}'
        "]}"
    )
    client = _ScriptedClient(payload)
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    result = await extract_from_messages(
        [{"role": "user", "content": "We repainted the kitchen last weekend."}],
        observation_date=_OBS,
        judge=False,
        operator_policy="",
    )

    by_key = {f.memory_key: f for f in result.facts}
    # qualifier + empty anchor -> backstop fires
    assert by_key["k1"].temporal_anchor == "2024-03-16T00:00:00"
    # no qualifier -> untouched
    assert by_key["k2"].temporal_anchor is None
    # model-emitted anchor -> never overridden
    assert by_key["k3"].temporal_anchor == "2024-02-14T00:00:00"


@pytest.mark.asyncio
async def test_backstop_noop_without_observation_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = '{"facts": [{"key": "k1", "value": "Bought a helmet yesterday."}]}'
    client = _ScriptedClient(payload)
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)

    result = await extract_from_messages(
        [{"role": "user", "content": "Bought a helmet yesterday."}],
        judge=False,
        operator_policy="",
    )

    assert result.facts[0].temporal_anchor is None
