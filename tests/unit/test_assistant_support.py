"""An assistant answer's support: the rule over its citation codes and its own tool calls
(server/assistant/domain/support.py), the op sets and found fields it reads
(server/operations/toolsets.py), the run read (ledger.run_rows, on clickhouse-local against
the production table, derived by domain/support.derive) and the two read patterns of
server/assistant/app/support.py: a whole answer's read and re-read, a streamed answer's one
read after the wait."""

from __future__ import annotations

import asyncio
import typing
from typing import Any

import pytest

from server.assistant.api.models import AssistantCitation
from server.assistant.app import support as app_support
from server.assistant.app.citations import Checked
from server.assistant.domain import support as rule
from server.assistant.store import ledger
from server.observability.consumer import ASSISTANT_CONSUMER
from server.operations import toolsets
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

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


def _checked(code: str) -> Checked:
    citation = AssistantCitation(
        kind="memory", text="[memory:a@v1]", verified=code == rule.VERIFIED
    )
    return Checked(citation, code)


def _rows_of(calls: rule.RunCalls) -> list[rule.CallRow]:
    """Rows that derive to `calls` (every op that found data ran)."""
    rows = [
        rule.CallRow(op, True, out_hits="" if op in calls.returned else "0")
        for op in sorted(calls.ran)
    ]
    return [*rows, rule.CallRow("memory.search", False)] if calls.failed else rows


def _calls(monkeypatch: pytest.MonkeyPatch, *reads: rule.RunCalls | Exception) -> list[str]:
    """The run read answers `reads` in turn; returns the run ids it was asked for."""
    asked: list[str] = []
    answers = list(reads)

    async def run_rows(org: str, run_id: str, subject: str) -> list[rule.CallRow]:
        asked.append(run_id)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _rows_of(answer)

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(app_support.ledger, "run_rows", run_rows)
    monkeypatch.setattr(app_support, "_wait", no_wait)
    return asked


def _answer(checked: list[Checked], text: str, run_id: str = "r1") -> str:
    return asyncio.run(
        app_support.answer_support(checked, text, org="acme", run_id=run_id, subject="alice")
    )


def test_a_cited_answer_or_an_abstention_reads_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _calls(monkeypatch)
    assert _answer([_checked(rule.VERIFIED)], "See [memory:a@v1].") == "supported"
    assert _answer([], "Nothing stored in Loom answers this.") == "none"
    assert _answer([], "") == "none"
    assert asked == []


def test_an_answer_naming_an_unknown_name_reads_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _calls(monkeypatch)
    got = asyncio.run(
        app_support.answer_support(
            [],
            "Call `memory.frobnicate`.",
            org="acme",
            run_id="r1",
            subject="alice",
            unknown_names=["memory.frobnicate"],
        )
    )
    assert (got, asked) == ("unsupported", [])


def test_an_uncited_answer_reads_its_calls_again_until_a_citable_op_found_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The summary's row is in; the search's lands late: the second read decides.
    first = rule.RunCalls(frozenset({"namespace.summary"}), False, frozenset({"namespace.summary"}))
    second = rule.RunCalls(
        frozenset({"namespace.summary", "memory.search"}),
        False,
        frozenset({"namespace.summary", "memory.search"}),
    )
    asked = _calls(monkeypatch, first, second)
    assert _answer([], "Deploys freeze on Fridays.") == "unsupported"
    assert asked == ["r1", "r1"]


def test_a_first_read_that_decides_is_not_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _calls(
        monkeypatch,
        rule.RunCalls(frozenset({"memory.search"}), False, frozenset({"memory.search"})),
    )
    assert _answer([], "Deploys freeze on Fridays.") == "unsupported"
    assert asked == ["r1"]


def test_a_greeting_that_read_nothing_has_no_label(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = rule.RunCalls(frozenset(), False, frozenset())
    _calls(monkeypatch, empty, empty)
    assert _answer([], "Hello! What would you like to know?") == "none"


def test_an_uncited_claim_after_a_search_that_found_nothing_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searched = rule.RunCalls(frozenset(), False, frozenset({"memory.search"}))
    asked = _calls(monkeypatch, searched, searched)
    assert _answer([], "Your database runs on port 5432.") == "unsupported"
    assert asked == ["r1", "r1"]


def test_calls_that_cannot_be_read_leave_an_uncited_answer_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _calls(monkeypatch, RuntimeError("ClickHouse is down"))
    assert _answer([], "Deploys freeze on Fridays.") == "unchecked"
    asked = _calls(monkeypatch)
    assert _answer([], "Deploys freeze on Fridays.", run_id="") == "unchecked"
    assert asked == []


def test_a_run_read_that_hangs_leaves_an_uncited_answer_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(*_: Any) -> list[rule.CallRow]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(app_support.ledger, "run_rows", hang)
    monkeypatch.setattr(app_support, "_CALLS_S", 0.05)
    assert _answer([], "Deploys freeze on Fridays.") == "unchecked"


def test_a_second_read_that_hangs_after_uncitable_data_leaves_the_answer_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []

    async def first_then_hang(*_: Any) -> list[rule.CallRow]:
        reads.append(1)
        if len(reads) == 1:
            return [rule.CallRow("namespace.summary", True)]
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(app_support.ledger, "run_rows", first_then_hang)
    monkeypatch.setattr(app_support, "_wait", no_wait)
    monkeypatch.setattr(app_support, "_CALLS_S", 0.05)
    assert _answer([], "This namespace holds 1,204 memories.") == "unchecked"
    assert len(reads) == 2


def _event(operation: str, args: dict[str, str], **kw: Any) -> str:
    """One `loom.agent_events` row, by default a successful call of the answer's run."""
    cols = {
        "org": "acme",
        "namespace": "ns",
        "run_id": "r1",
        "subject": "alice",
        "consumer": ASSISTANT_CONSUMER,
        "operation": operation,
        "success": 1,
        **kw,
    }
    pairs = ", ".join(f"'{k}', '{v}'" for k, v in args.items())
    values = [f"'{v}'" if isinstance(v, str) else str(v) for v in cols.values()]
    return (
        f"INSERT INTO loom.agent_events ({', '.join([*cols, 'args'])}) "
        f"VALUES ({', '.join([*values, f'map({pairs})'])})"
    )


def _run_rows(*rows: str) -> list[rule.CallRow]:
    ch = LocalCH(ddl("agent_events"), *rows)
    return asyncio.run(ledger.run_rows("acme", "r1", "alice", ch=ch))  # type: ignore[arg-type]


def _run_calls(*rows: str) -> rule.RunCalls:
    return rule.derive(_run_rows(*rows))


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_run_read_names_the_ops_that_found_data() -> None:
    calls = _run_calls(
        _event("memory.search", {"out_hits": "2"}),
        _event("namespace.inventory", {}),
        _event("memory.get", {"out_found": "true"}),
        # Under the code namespace: a code call is not filed under the answer's.
        _event("code.search", {"out_hits": "4"}, namespace="code-loom"),
        # Found nothing.
        _event("knowledge.search", {"out_hits": "0"}),
        _event("knowledge.get", {"out_found": "false"}),
        # An abstention's hits are written as "0" (event_args).
        _event("memory.ask", {"out_abstain": "true", "out_hits": "0"}),
        _event("namespace.audit", {"out_hits": "0"}),
    )
    found = frozenset({"memory.search", "namespace.inventory", "memory.get", "code.search"})
    assert calls.returned == found and calls.failed is False
    # Every successful call ran, found or not.
    nothing = {"knowledge.search", "knowledge.get", "memory.ask", "namespace.audit"}
    assert calls.ran == found | nothing


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_run_read_sees_a_refused_call_under_the_sentinel_namespace() -> None:
    calls = _run_calls(_event("memory.search", {}, success=0, namespace="-"))
    assert calls == rule.RunCalls(frozenset(), True, frozenset())


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_run_read_reads_only_the_answers_own_calls() -> None:
    found = {"out_hits": "3"}
    calls = _run_calls(
        _event("memory.search", found, run_id="r2"),
        _event("memory.search", found, subject="bob"),
        _event("memory.search", found, consumer="my-script"),
        _event("memory.search", found, org="other"),
    )
    assert calls == rule.RunCalls(frozenset(), False, frozenset())


def _rows(n: int) -> str:
    """`n` successful calls of the answer's run, each its own request."""
    return (
        "INSERT INTO loom.agent_events (org, namespace, run_id, subject, consumer, operation, "
        "success, request_id) SELECT 'acme', 'ns', 'r1', 'alice', "
        f"'{ASSISTANT_CONSUMER}', 'namespace.summary', 1, toString(number) FROM numbers({n})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
@pytest.mark.parametrize(("n", "cut"), [(200, False), (201, True)])
def test_a_run_past_one_reads_rows_is_cut(n: int, cut: bool) -> None:
    rows = _run_rows(_rows(n))
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


def _streamed(monkeypatch: pytest.MonkeyPatch, *reads: list[rule.CallRow] | Exception) -> list[str]:
    """The streamed pattern's seams: the wait and the run read record themselves, the read
    answering `reads` in turn; returns what happened, in order."""
    happened: list[str] = []
    answers = list(reads)

    async def wait(seconds: float) -> None:
        happened.append("wait")

    async def run_rows(org: str, run_id: str, subject: str) -> list[rule.CallRow]:
        happened.append(f"read {run_id}")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(app_support, "_wait", wait)
    monkeypatch.setattr(app_support.ledger, "run_rows", run_rows)
    return happened


def test_a_streamed_answer_reads_its_run_once_after_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [rule.CallRow("memory.search", True, out_hits="2")]
    happened = _streamed(monkeypatch, found)
    rows = asyncio.run(app_support.streamed_calls("acme", "r1", "alice"))
    assert (rows, happened) == (found, ["wait", "read r1"])
    # A cited answer is decided by its citation; an uncited one by the same read.
    assert app_support.streamed_support([_checked(rule.VERIFIED)], "See it.", rows) == "supported"
    assert app_support.streamed_support([], "Deploys freeze on Fridays.", rows) == "unsupported"


def test_a_streamed_run_that_cannot_be_read_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    happened = _streamed(monkeypatch, RuntimeError("ClickHouse is down"))
    assert asyncio.run(app_support.streamed_calls("acme", "r1", "alice")) is None
    assert asyncio.run(app_support.streamed_calls("acme", "", "alice")) is None
    assert happened == ["wait", "read r1"]
    assert app_support.streamed_support([], "Deploys freeze on Fridays.", None) == "unchecked"


def test_a_streamed_run_read_that_hangs_is_cut_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(*_: Any) -> list[rule.CallRow]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    _streamed(monkeypatch)
    monkeypatch.setattr(app_support.ledger, "run_rows", hang)
    monkeypatch.setattr(app_support, "_CALLS_S", 0.05)
    assert asyncio.run(app_support.streamed_calls("acme", "r1", "alice")) is None


# The calls a starter's answer makes as the agent's instructions have it make them: the
# audit starter reads the audit, the mentor brief, the drafts, the guide and Loom's guide.
_AUDIT_STARTER_RUN = [
    rule.CallRow("namespace.audit", True, out_hits="3"),
    rule.CallRow("mentor.brief", True),
    rule.CallRow("namespace.tune", True, out_hits="2"),
    rule.CallRow("namespace.guide_get", True, out_found="true"),
    rule.CallRow("mentor.guide", True),
]


def test_a_recorded_starter_run_is_labelled_as_any_answer() -> None:
    text = "The audit found three exact-key reads; see the card above."
    # Uncited, over Loom's own reads with no citation form.
    assert app_support.streamed_support([], text, _AUDIT_STARTER_RUN) == "unchecked"
    # The model also read a finding's row, and cited it or did not.
    read_row = [*_AUDIT_STARTER_RUN, rule.CallRow("memory.get", True, out_found="true")]
    assert app_support.streamed_support([], text, read_row) == "unsupported"
    assert app_support.streamed_support([_checked(rule.VERIFIED)], text, read_row) == "supported"
