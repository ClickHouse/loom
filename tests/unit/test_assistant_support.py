"""An assistant answer's support: the rule over its citation codes and its own tool calls
(server/assistant/domain/support.py), the op sets and found fields it reads
(server/operations/toolsets.py), the run read (ledger.run_calls, on clickhouse-local against
the production table) and the one re-read (server/assistant/app/support.py)."""

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
    # Every op that found data ran.
    ran = returned or frozenset()
    return rule.support_of(
        codes or [],
        text=text,
        returned=returned,
        failed=failed,
        ran=ran,
        citable=CITABLE,
        no_data=NO_DATA,
    )


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
        returned=frozenset(),
        failed=False,
        citable=CITABLE,
        no_data=NO_DATA,
        ran=ran,
    )
    assert got == expected


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


def _calls(monkeypatch: pytest.MonkeyPatch, *reads: ledger.RunCalls | Exception) -> list[str]:
    """The run read answers `reads` in turn; returns the run ids it was asked for."""
    asked: list[str] = []
    answers = list(reads)

    async def run_calls(org: str, run_id: str, subject: str) -> ledger.RunCalls:
        asked.append(run_id)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(app_support.ledger, "run_calls", run_calls)
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


def test_an_uncited_answer_reads_its_calls_again_until_a_citable_op_found_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The summary's row is in; the search's lands late: the second read decides.
    first = ledger.RunCalls(
        frozenset({"namespace.summary"}), False, frozenset({"namespace.summary"})
    )
    second = ledger.RunCalls(
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
        ledger.RunCalls(frozenset({"memory.search"}), False, frozenset({"memory.search"})),
    )
    assert _answer([], "Deploys freeze on Fridays.") == "unsupported"
    assert asked == ["r1"]


def test_a_greeting_that_read_nothing_has_no_label(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = ledger.RunCalls(frozenset(), False, frozenset())
    _calls(monkeypatch, empty, empty)
    assert _answer([], "Hello! What would you like to know?") == "none"


def test_an_uncited_claim_after_a_search_that_found_nothing_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searched = ledger.RunCalls(frozenset(), False, frozenset({"memory.search"}))
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
    async def hang(*_: Any) -> ledger.RunCalls:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(app_support.ledger, "run_calls", hang)
    monkeypatch.setattr(app_support, "_CALLS_S", 0.05)
    assert _answer([], "Deploys freeze on Fridays.") == "unchecked"


def test_a_second_read_that_hangs_after_uncitable_data_leaves_the_answer_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []

    async def first_then_hang(*_: Any) -> ledger.RunCalls:
        reads.append(1)
        if len(reads) == 1:
            return ledger.RunCalls(
                frozenset({"namespace.summary"}), False, frozenset({"namespace.summary"})
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(app_support.ledger, "run_calls", first_then_hang)
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


def _run_calls(*rows: str) -> ledger.RunCalls:
    ch = LocalCH(ddl("agent_events"), *rows)
    return asyncio.run(ledger.run_calls("acme", "r1", "alice", ch=ch))  # type: ignore[arg-type]


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
        _event("memory.ask", {"out_abstain": "true", "out_hits": "0"}),
        _event("memory.bulk_get", {"out_found_n": "0"}),
        _event("search.query", {"out_section_hits": "0"}),
    )
    found = frozenset({"memory.search", "namespace.inventory", "memory.get", "code.search"})
    assert calls.returned == found and calls.failed is False
    # Every successful call ran, found or not.
    nothing = {"knowledge.search", "knowledge.get", "memory.ask", "memory.bulk_get", "search.query"}
    assert calls.ran == found | nothing


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_run_read_sees_a_refused_call_under_the_sentinel_namespace() -> None:
    calls = _run_calls(_event("memory.search", {}, success=0, namespace="-"))
    assert calls == ledger.RunCalls(frozenset(), True, frozenset())


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_run_read_reads_only_the_answers_own_calls() -> None:
    found = {"out_hits": "3"}
    calls = _run_calls(
        _event("memory.search", found, run_id="r2"),
        _event("memory.search", found, subject="bob"),
        _event("memory.search", found, consumer="my-script"),
        _event("memory.search", found, org="other"),
    )
    assert calls == ledger.RunCalls(frozenset(), False, frozenset())
