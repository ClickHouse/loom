"""Pure support rules derive labels from bounded tool receipts."""

from __future__ import annotations

import typing
from typing import Any

import pytest

from server.assistant.domain import support as rule
from server.operations import toolsets

CITABLE = toolsets.ASSISTANT_CITABLE_OPS
NO_DATA = toolsets.ASSISTANT_NO_NAMESPACE_DATA_OPS


def _support(
    text: str,
    codes: list[str] | None = None,
    returned: frozenset[str] | None = frozenset(),
    failed: bool = False,
) -> str:
    # Every op that found data ran; None: the calls could not be read.
    calls = None if returned is None else rule.RunCalls(returned, failed, returned)
    return rule.support_of(codes or [], text=text, calls=calls, citable=CITABLE, no_data=NO_DATA)


@pytest.mark.parametrize(
    ("text", "codes", "returned", "failed", "expected"),
    [
        # The common cases.
        (
            "Hello! What would you like to know about this namespace?",
            [],
            frozenset(),
            False,
            "none",
        ),
        (
            "I search memories and documents and count with SQL. What do you need?",
            [],
            frozenset(),
            False,
            "none",
        ),
        (
            "Deploys freeze on Fridays [memory:policy/freeze@v3].",
            [rule.VERIFIED],
            frozenset(),
            False,
            "supported",
        ),
        ("Deploys freeze on Fridays.", [], frozenset({"memory.search"}), False, "unsupported"),
        (
            "Nothing stored in Loom answers this.",
            [],
            frozenset({"memory.search", "knowledge.search"}),
            False,
            "none",
        ),
        # A fact citing only what did not check out; a mixed answer.
        (
            "Deploys freeze on Fridays [memory:policy/other@v3].",
            [rule.UNREAD],
            frozenset(),
            False,
            "unsupported",
        ),
        (
            "See [memory:a@v1] and [doc:b.md § C].",
            [rule.VERIFIED, rule.NO_SECTION],
            frozenset(),
            False,
            "supported",
        ),
        # Checks that could not say.
        ("See [code:server/main.py:4].", [rule.CODE], frozenset(), False, "unchecked"),
        ("See [memory:a@v1].", [rule.NOT_RECORDED], frozenset(), False, "unchecked"),
        ("See [memory:a@v1].", [rule.NOT_RUN], frozenset(), False, "unchecked"),
        (
            "See [memory:a@v1] and [code:x.py:1].",
            [rule.NOT_RUN, rule.CODE],
            frozenset(),
            False,
            "unchecked",
        ),
        # Uncited after reads with no citation form, after a failed call, or unreadable calls.
        (
            "This namespace holds 1,204 memories.",
            [],
            frozenset({"namespace.summary"}),
            False,
            "unchecked",
        ),
        ("It holds 3 users.", [], frozenset({"namespace.inventory"}), False, "unchecked"),
        # An inspection that listed rows the answer could cite, and cites none.
        (
            "Alice wrote the deploy freeze policy.",
            [],
            frozenset({"memory.inspect"}),
            False,
            "unsupported",
        ),
        ("Deploys freeze on Fridays.", [], frozenset(), True, "unchecked"),
        ("Deploys freeze on Fridays.", [], None, False, "unchecked"),
        # Loom's own guide is no namespace data.
        ("Write one claim per key.", [], frozenset({"mentor.guide"}), False, "none"),
        # A code search found data and the answer cites none of it.
        ("It is parsed in the router.", [], frozenset({"code.search"}), False, "unsupported"),
        # No text at all.
        ("", [], frozenset({"memory.search"}), False, "none"),
        # Accepted residuals (docs/architecture/assistant.md#support).
        # A greeting or a question back after a call that found data.
        (
            "Hi! What would you like to know?",
            [],
            frozenset({"memory.search"}),
            False,
            "unsupported",
        ),
        (
            "Do you mean staging or production?",
            [],
            frozenset({"memory.search"}),
            False,
            "unsupported",
        ),
        (
            "Hi! What would you like to know?",
            [],
            frozenset({"namespace.summary"}),
            False,
            "unchecked",
        ),
        # An uncited fact with no call at all (a restated earlier turn).
        ("Your database runs on port 5432.", [], frozenset(), False, "none"),
        # An abstention that also states a fact.
        (
            "It is 5432. Nothing stored in Loom answers this.",
            [],
            frozenset({"memory.search"}),
            False,
            "none",
        ),
    ],
)
def test_the_support_of_an_answer(
    text: str, codes: list[str], returned: frozenset[str] | None, failed: bool, expected: str
) -> None:
    assert _support(text, codes, returned, failed) == expected


@pytest.mark.parametrize(
    ("ran", "expected"),
    [
        # Searched, found nothing, and states a fact anyway: nothing backs it.
        (frozenset({"memory.search", "knowledge.search"}), "unsupported"),
        # Only Loom's own guide ran: no namespace data was asked for.
        (frozenset({"mentor.guide"}), "none"),
        # Only an uncitable op ran and found nothing.
        (frozenset({"namespace.audit"}), "none"),
    ],
)
def test_an_uncited_fact_after_calls_that_found_nothing(ran: frozenset[str], expected: str) -> None:
    got = rule.support_of(
        [],
        text="Your database runs on port 5432.",
        calls=rule.RunCalls(frozenset(), False, ran),
        citable=CITABLE,
        no_data=NO_DATA,
    )
    assert got == expected


@pytest.mark.parametrize(
    ("codes", "text", "unknown"),
    [
        (
            [rule.VERIFIED],
            "Set it with `memory.frobnicate` [memory:policy/freeze@v3].",
            "memory.frobnicate",
        ),
        ([], "Set LOOM_MADE_UP to true.", "LOOM_MADE_UP"),
    ],
)
def test_an_answer_naming_an_unknown_name_is_unsupported_whatever_it_cites(
    codes: list[str], text: str, unknown: str
) -> None:
    got = rule.support_of(
        codes,
        text=text,
        calls=rule.RunCalls(frozenset(), False, frozenset()),
        citable=CITABLE,
        no_data=NO_DATA,
        unknown_names=[unknown],
    )
    assert got == "unsupported"


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        # Only the docs were read: they are not the namespace's data.
        (frozenset({"help.search", "help.get"}), "none"),
        # A search of the namespace found rows the answer did not cite: the residual an
        # uncited answer from the docs carries until help citations are checked.
        (frozenset({"help.search", "help.get", "memory.search"}), "unsupported"),
    ],
)
def test_an_uncited_answer_from_the_docs(returned: frozenset[str], expected: str) -> None:
    got = rule.support_of(
        [],
        text="Share a collection by granting its members a role.",
        calls=rule.RunCalls(returned, False, returned),
        citable=CITABLE,
        no_data=NO_DATA,
    )
    assert got == expected


@pytest.mark.parametrize(
    ("returned", "failed"),
    [
        # An empty search beside counts that came back: the claim may rest on the counts.
        (frozenset({"namespace.summary"}), False),
        # An empty search beside a call that failed: the check cannot say.
        (frozenset(), True),
    ],
)
def test_an_empty_search_beside_other_data_or_a_failure_is_unchecked(
    returned: frozenset[str], failed: bool
) -> None:
    got = rule.support_of(
        [],
        text="This namespace holds 1,204 memories.",
        calls=rule.RunCalls(returned, failed, returned | {"memory.search"}),
        citable=CITABLE,
        no_data=NO_DATA,
    )
    assert got == "unchecked"


@pytest.mark.parametrize("op", ["memory.get", "code.symbol"])
def test_a_lookup_that_missed_is_none_with_the_sentence_and_unsupported_without(op: str) -> None:
    def support(text: str) -> str:
        return rule.support_of(
            [],
            text=text,
            calls=rule.RunCalls(frozenset(), False, frozenset({op})),
            citable=CITABLE,
            no_data=NO_DATA,
        )

    assert (
        support("No memory is stored under policy/freeze. Nothing stored in Loom answers this.")
        == "none"
    )
    assert support("No memory is stored under policy/freeze.") == "unsupported"


def test_a_question_back_that_cites_each_match_is_supported() -> None:
    text = "Two match: [memory:deploy/staging@v2] and [memory:deploy/prod@v4]. Which one?"
    assert (
        _support(text, [rule.VERIFIED, rule.VERIFIED], frozenset({"memory.search"})) == "supported"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Nothing stored in Loom answers this.",
        "Nothing stored in Loom answers this",
        "**Nothing stored in Loom answers this.**",
        "I looked for 'v2 rollout'.  NOTHING stored in   Loom answers this. Want me to look in code?",
        "Nada sobre eso. Nothing stored in Loom answers this.",
    ],
)
def test_the_abstention_is_read_in_any_case_emphasis_or_spacing(text: str) -> None:
    assert rule.is_abstention(text)


def test_an_answer_without_the_sentence_is_no_abstention() -> None:
    assert not rule.is_abstention("It is not stored in Loom.")


def test_every_assistant_op_is_in_exactly_one_set() -> None:
    sets = [
        toolsets.ASSISTANT_CITABLE_OPS,
        toolsets.ASSISTANT_UNCITABLE_OPS,
        toolsets.ASSISTANT_NO_NAMESPACE_DATA_OPS,
    ]
    for op in toolsets.MCP_ASSISTANT_OPS:
        assert sum(op in s for s in sets) == 1, op
    assert set().union(*sets) == set(toolsets.MCP_ASSISTANT_OPS)


# The assistant ops with no found list: a miss is their `found: false` (`out_found`), or
# they always return data (and why). A new assistant op names its signal here or in
# ASSISTANT_FOUND_FIELD, or the test below fails.
FOUND_BOOL_OPS: frozenset[str] = frozenset(
    {
        "memory.get",
        "memory.provenance",
        "knowledge.get",
        "namespace.get",
        "namespace.guide_get",
        "facts.path",
        "help.get",
    }
)
ALWAYS_RETURNS: dict[str, str] = {
    "namespace.inventory": "one section per plane, its counts zero or not",
    "mentor.brief": "the namespace's usage counts and hints, zero or not",
    "mentor.guide": "Loom's own writing guide",
    "insights.catalog": "the tables an analysis may read",
}


def _field_type(model: type[Any], name: str) -> Any:
    return typing.get_type_hints(model)[name]


@pytest.mark.parametrize("op", toolsets.assistant_ops(), ids=lambda op: op.name)
def test_every_assistant_op_says_how_its_ledger_row_shows_it_found_nothing(op: Any) -> None:
    # A new op in the toolset must say which output list is its rows, that a miss is its
    # `found: false`, or why it always returns data: else an empty answer reads as data.
    name = op.name
    marks = [
        name in toolsets.ASSISTANT_FOUND_FIELD,
        name in FOUND_BOOL_OPS,
        name in ALWAYS_RETURNS,
    ]
    assert sum(marks) == 1, name
    for field in toolsets.ASSISTANT_FOUND_FIELD.get(name, ()):
        model = op.output_model
        *parents, last = field.split(".")
        for step in parents:
            model = _field_type(model, step)
        hint = _field_type(model, last)
        lists = [hint, *typing.get_args(hint)]
        assert any(typing.get_origin(h) is list for h in lists), (name, field, hint)
    if name in FOUND_BOOL_OPS:
        assert _field_type(op.output_model, "found") is bool, name


def test_call_receipts_name_the_ops_that_found_data() -> None:
    calls = rule.derive(
        [
            rule.CallRow("memory.search", True, out_hits="2"),
            rule.CallRow("namespace.inventory", True),
            rule.CallRow("memory.get", True, out_found="true"),
            rule.CallRow("code.search", True, out_hits="4"),
            rule.CallRow("knowledge.search", True, out_hits="0"),
            rule.CallRow("knowledge.get", True, out_found="false"),
            rule.CallRow("memory.ask", True, out_hits="0"),
            rule.CallRow("namespace.audit", True, out_hits="0"),
        ]
    )
    found = frozenset({"memory.search", "namespace.inventory", "memory.get", "code.search"})
    assert calls.returned == found and calls.failed is False
    nothing = {"knowledge.search", "knowledge.get", "memory.ask", "namespace.audit"}
    assert calls.ran == found | nothing


def test_a_refused_call_receipt_does_not_claim_data() -> None:
    calls = rule.derive([rule.CallRow("memory.search", False)])
    assert calls == rule.RunCalls(frozenset(), True, frozenset())


@pytest.mark.parametrize(("n", "cut"), [(200, False), (201, True)])
def test_a_run_past_the_receipt_bound_is_cut(n: int, cut: bool) -> None:
    rows = [rule.CallRow("namespace.summary", True)] * n
    assert len(rows) == n
    assert rule.derive(rows).cut is cut


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # A citable op found data: decided whatever the rest of the run held.
        ([rule.CallRow("memory.search", True, out_hits="2")], "unsupported"),
        # Nothing citable found in the part read: the rest is not known.
        ([rule.CallRow("memory.search", True, out_hits="0")], "unchecked"),
        ([rule.CallRow("mentor.guide", True)], "unchecked"),
    ],
)
def test_a_cut_run_keeps_only_a_decided_outcome(rows: list[rule.CallRow], expected: str) -> None:
    padding = [rule.CallRow("help.get", True)] * rule.RUN_ROWS
    calls = rule.derive([*rows, *padding])
    assert calls.cut
    got = rule.support_of(
        [], text="Deploys freeze on Fridays.", calls=calls, citable=CITABLE, no_data=NO_DATA
    )
    assert got == expected


def test_derive_reads_a_row_as_found_unless_it_says_it_found_nothing() -> None:
    calls = rule.derive(
        [
            rule.CallRow("memory.search", True, out_hits="3"),
            rule.CallRow("memory.get", True, out_found="false"),
            rule.CallRow("knowledge.search", True, out_hits="0"),
            rule.CallRow("code.search", False),
        ]
    )
    assert calls == rule.RunCalls(
        frozenset({"memory.search"}),
        True,
        frozenset({"memory.search", "memory.get", "knowledge.search"}),
    )
