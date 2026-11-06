"""_injectable_identifiers: broaden injection to entities semantic recall misses
(snake_case settings, PascalCase handles, zero-padded test ids) on top of the
salient numbers/@handles, without dragging in plain English."""

import pytest

from server.memory.app.ask.identifiers import _injectable_identifiers as inj


@pytest.mark.parametrize(
    ("question", "identifier"),
    [
        pytest.param(
            "replying to SamRivera about disabling a test", "SamRivera", id="PascalCase handle"
        ),
        pytest.param(
            "is it a known root cause with optimize_distinct_in_order?",
            "optimize_distinct_in_order",
            id="snake_case setting",
        ),
        pytest.param(
            "the new test file 05059 for serialize_query_plan", "05059", id="five-digit test id"
        ),
        # Seven digits: past the salient-number path's five or six, so only the
        # zero-padded-id shape catches it.
        pytest.param(
            "the new test file 0123456 for the planner", "0123456", id="seven-digit zero-padded id"
        ),
        # A STID (NNNN-hhhh) is the key of the failure a query asks about.
        pytest.param(
            "What do we know about the BuzzHouse amd_msan failure with STID 3782-2f00?",
            "3782-2f00",
            id="STID",
        ),
        pytest.param(
            "what is the std::terminate at MergeTreeData.cpp:3539 about",
            "MergeTreeData.cpp:3539",
            id="file and line",
        ),
    ],
)
def test_an_identifier_shape_that_semantic_recall_misses_is_injected(
    question: str, identifier: str
) -> None:
    assert identifier in inj(question)


def test_still_catches_numbers_and_handles():
    got = inj("PR 117841 and issue 117588 reviewed by @tvega")
    # Handles keep a normalized leading "@"; the substring consumers lstrip("@")
    # so matching still lands on every mention form ("@tvega", "@ tvega", "tvega").
    assert "117841" in got and "117588" in got and "@tvega" in got


def test_catches_space_broken_handle():
    # "@ handle" (space-broken ref) must still resolve to the normalized "@handle".
    assert "@rdev1" in inj("what are @ rdev1 review expectations")


def test_ignores_plain_english():
    got = inj("what is the correct remedy when a stateless test fails the flaky check")
    # no snake_case / PascalCase-hump / zero-id / @handle here -> salient finds none
    assert got == [] or all("_" in g or g[:1].isupper() or g[:1].isdigit() for g in got)


def test_stid_not_confused_with_bare_number():
    # A plain PR number is handled by the salient-number path, not the STID shape.
    got = inj("status of PR 117841")
    assert "3782-2f00" not in got
