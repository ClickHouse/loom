"""Pure support rules derive labels from bounded tool receipts."""

from __future__ import annotations

import typing
from typing import Any

import pytest

from server.assistant.domain import support as rule
from server.assistant.domain.steps import Step, steps_of
from server.operations import toolsets

CITABLE = toolsets.ASSISTANT_CITABLE_OPS
NO_DATA = toolsets.ASSISTANT_NO_NAMESPACE_DATA_OPS


def _support(
    text: str,
    codes: list[str] | None = None,
    returned: frozenset[str] = frozenset(),
) -> str:
    calls = rule.RunCalls(returned, returned)
    return rule.support_of(codes or [], text=text, calls=calls, citable=CITABLE, no_data=NO_DATA)


@pytest.mark.parametrize(
    ("text", "codes", "returned", "expected"),
    [
        # The common cases.
        (
            "Hello! What would you like to know about this namespace?",
            [],
            frozenset(),
            "none",
        ),
        (
            "I search memories and documents and count with SQL. What do you need?",
            [],
            frozenset(),
            "none",
        ),
        (
            "Deploys freeze on Fridays [memory:policy/freeze@v3].",
            [rule.VERIFIED],
            frozenset(),
            "supported",
        ),
        ("Deploys freeze on Fridays.", [], frozenset({"memory.search"}), "unsupported"),
        (
            "Nothing stored in Loom answers this.",
            [],
            frozenset({"memory.search", "knowledge.search"}),
            "none",
        ),
        # A fact citing only what did not check out; a mixed answer.
        (
            "Deploys freeze on Fridays [memory:policy/other@v3].",
            [rule.UNREAD],
            frozenset(),
            "unsupported",
        ),
        (
            "See [memory:a@v1] and [doc:b.md § C].",
            [rule.VERIFIED, rule.UNREAD],
            frozenset(),
            "supported",
        ),
        # Native receipts verify code citations as well as memory and document citations.
        ("See [code:server/main.py:4].", [rule.VERIFIED], frozenset(), "supported"),
        ("See [code:server/main.py:4].", [rule.UNREAD], frozenset(), "unsupported"),
        ("See [memory:a@v1].", [rule.UNREAD], frozenset(), "unsupported"),
        ("See [doc:b.md § C].", [rule.UNREAD], frozenset(), "unsupported"),
        (
            "See [memory:a@v1] and [code:x.py:1].",
            [rule.UNREAD, rule.UNREAD],
            frozenset(),
            "unsupported",
        ),
        # Uncited after reads with no citation form.
        (
            "This namespace holds 1,204 memories.",
            [],
            frozenset({"namespace.summary"}),
            "unchecked",
        ),
        ("It holds 3 users.", [], frozenset({"namespace.inventory"}), "unchecked"),
        # An inspection that listed rows the answer could cite, and cites none.
        (
            "Alice wrote the deploy freeze policy.",
            [],
            frozenset({"memory.inspect"}),
            "unsupported",
        ),
        # Loom's own guide is no namespace data.
        ("Write one claim per key.", [], frozenset({"mentor.guide"}), "none"),
        # A code search found data and the answer cites none of it.
        ("It is parsed in the router.", [], frozenset({"code.search"}), "unsupported"),
        # No text at all.
        ("", [], frozenset({"memory.search"}), "none"),
        # Accepted residuals (docs/architecture/assistant.md#support).
        # A greeting or a question back after a call that found data.
        (
            "Hi! What would you like to know?",
            [],
            frozenset({"memory.search"}),
            "unsupported",
        ),
        (
            "Do you mean staging or production?",
            [],
            frozenset({"memory.search"}),
            "unsupported",
        ),
        (
            "Hi! What would you like to know?",
            [],
            frozenset({"namespace.summary"}),
            "unchecked",
        ),
        # An uncited fact with no call at all (a restated earlier turn).
        ("Your database runs on port 5432.", [], frozenset(), "none"),
        # An abstention that also states a fact.
        (
            "It is 5432. Nothing stored in Loom answers this.",
            [],
            frozenset({"memory.search"}),
            "none",
        ),
    ],
)
def test_the_support_of_an_answer(
    text: str, codes: list[str], returned: frozenset[str], expected: str
) -> None:
    assert _support(text, codes, returned) == expected


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
        calls=rule.RunCalls(frozenset(), ran),
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
        calls=rule.RunCalls(frozenset(), frozenset()),
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
        # Namespace rows were returned but the answer cites none of them.
        (frozenset({"help.search", "help.get", "memory.search"}), "unsupported"),
    ],
)
def test_an_uncited_answer_from_the_docs(returned: frozenset[str], expected: str) -> None:
    got = rule.support_of(
        [],
        text="Share a collection by granting its members a role.",
        calls=rule.RunCalls(returned, returned),
        citable=CITABLE,
        no_data=NO_DATA,
    )
    assert got == expected


def test_an_empty_search_beside_uncitable_data_is_unchecked() -> None:
    got = rule.support_of(
        [],
        text="This namespace holds 1,204 memories.",
        calls=rule.RunCalls(
            frozenset({"namespace.summary"}),
            frozenset({"namespace.summary", "memory.search"}),
        ),
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
            calls=rule.RunCalls(frozenset(), frozenset({op})),
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
def test_every_assistant_op_says_how_its_receipt_shows_it_found_nothing(op: Any) -> None:
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
            rule.CallRow("memory.search", out_hits="2"),
            rule.CallRow("namespace.inventory"),
            rule.CallRow("memory.get", out_found="true"),
            rule.CallRow("code.search", out_hits="4"),
            rule.CallRow("knowledge.search", out_hits="0"),
            rule.CallRow("knowledge.get", out_found="false"),
            rule.CallRow("memory.ask", out_hits="0"),
            rule.CallRow("namespace.audit", out_hits="0"),
        ]
    )
    found = frozenset({"memory.search", "namespace.inventory", "memory.get", "code.search"})
    assert calls.returned == found
    nothing = {"knowledge.search", "knowledge.get", "memory.ask", "namespace.audit"}
    assert calls.ran == found | nothing


def test_derive_reads_a_row_as_found_unless_it_says_it_found_nothing() -> None:
    calls = rule.derive(
        [
            rule.CallRow("memory.search", out_hits="3"),
            rule.CallRow("memory.get", out_found="false"),
            rule.CallRow("knowledge.search", out_hits="0"),
        ]
    )
    assert calls == rule.RunCalls(
        frozenset({"memory.search"}),
        frozenset({"memory.search", "memory.get", "knowledge.search"}),
    )


@pytest.mark.parametrize(
    ("operation", "label"),
    [("knowledge.search", "Searched documents"), ("knowledge.get", "Read a document")],
)
def test_empty_read_receipts_keep_their_step_and_support(operation: str, label: str) -> None:
    rows = [rule.CallRow(operation, out_found="false", out_hits="0")]
    assert steps_of([operation]) == [Step(operation, label, True)]
    assert (
        rule.support_of(
            [], text="An answer", calls=rule.derive(rows), citable=CITABLE, no_data=NO_DATA
        )
        == "unsupported"
    )
