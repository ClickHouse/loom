"""`caller_text` shows a caller a Loom error's code and message and any other error's
kind; `stored_caller_text` cuts a stored error to that shape. CH-free."""

from __future__ import annotations

from server.core.errors import (
    BackendUnavailableError,
    LoomServerError,
    OverloadedError,
    caller_text,
    stored_caller_text,
)

_DRIVER_TEXT = "Code: 210. Connection refused (ch-internal-7:9440) ALTER TABLE loom.memory"


def test_a_loom_error_reads_as_its_code_and_message() -> None:
    assert caller_text(OverloadedError("the server is busy; retry")) == (
        "OVERLOADED: the server is busy; retry"
    )
    assert caller_text(BackendUnavailableError()) == "BACKEND_UNAVAILABLE"
    assert caller_text(LoomServerError("x")) == "INTERNAL_ERROR: x"


def test_any_other_error_reads_as_its_kind_only() -> None:
    assert caller_text(RuntimeError(_DRIVER_TEXT)) == "RuntimeError"
    assert caller_text(KeyError("k")) == "KeyError"
    assert caller_text(TimeoutError()) == "TimeoutError"


def test_a_stored_error_is_cut_to_the_same_shape() -> None:
    assert {
        text: stored_caller_text(text)
        for text in (
            "",
            "RuntimeError",
            f"RuntimeError: {_DRIVER_TEXT}",
            "OVERLOADED: the server is busy; retry",
            "BACKEND_UNAVAILABLE",
            "Connection refused (ch-internal-7:9440)",
            "DB::Exception: Table loom.memory does not exist",
            "Code: 241. DB::Exception: Memory limit (total) exceeded (ch-internal-7:9440)",
            "Connection: refused",
            "ConnectTimeout",
            "gaierror",
            "ConnectTimeout: timed out connecting to ch-internal-7:9440",
            "ERROR: connection to ch-internal-7:9440 refused",
        )
    } == {
        "": "",
        "RuntimeError": "RuntimeError",
        f"RuntimeError: {_DRIVER_TEXT}": "RuntimeError",
        "OVERLOADED: the server is busy; retry": "OVERLOADED: the server is busy; retry",
        "BACKEND_UNAVAILABLE": "BACKEND_UNAVAILABLE",
        "Connection refused (ch-internal-7:9440)": "",
        "DB::Exception: Table loom.memory does not exist": "",
        "Code: 241. DB::Exception: Memory limit (total) exceeded (ch-internal-7:9440)": "",
        "Connection: refused": "",
        # A bare identifier is one word, whatever its case: kept.
        "ConnectTimeout": "ConnectTimeout",
        "gaierror": "gaierror",
        # Before text, only a class name ending in Error or Exception is a kind.
        "ConnectTimeout: timed out connecting to ch-internal-7:9440": "",
        # Before text, an all-caps head is kept only when it is a Loom error's code.
        "ERROR: connection to ch-internal-7:9440 refused": "",
    }


def test_what_caller_text_writes_reads_back_unchanged() -> None:
    for e in (OverloadedError("busy; retry"), BackendUnavailableError(), RuntimeError("x")):
        assert stored_caller_text(caller_text(e)) == caller_text(e)


def test_a_consolidation_reason_an_older_build_stored_is_cut() -> None:
    from server.memory.app.consolidate import _shown_failure

    lossy = "merge lost distinguishing values: 2024-03-01"
    assert _shown_failure(lossy) == lossy
    assert _shown_failure("merge response lacked canonical_key/value") == (
        "merge response lacked canonical_key/value"
    )
    assert _shown_failure(f"ReadTimeout: {_DRIVER_TEXT}") == (
        "the merge failed; the error is in the server log"
    )
    unread = "no JSON object in merge response: 'sure, here you go'"
    assert _shown_failure(unread) == unread  # Loom's words about the model's answer
    assert _shown_failure(f"ValueError: {unread}") == unread
    # A reason that only mentions the merge response is not the parser's.
    assert _shown_failure(f"HTTPStatusError: merge response {_DRIVER_TEXT}") == "HTTPStatusError"
    assert _shown_failure("ReadTimeout") == "ReadTimeout"
    assert _shown_failure(f"HTTPStatusError: {_DRIVER_TEXT}") == "HTTPStatusError"
    assert _shown_failure("") == ""


def test_a_malformed_merge_response_is_a_fixed_reason() -> None:
    import pytest

    from server.memory.app.consolidate import _parse_merge_response

    with pytest.raises(ValueError) as ei:
        _parse_merge_response('{"canonical_key": "k", "canonical_value": }')
    assert str(ei.value) == "merge response is not valid JSON"
    with pytest.raises(ValueError) as deep:
        _parse_merge_response("{" + '"a": [' * 100_000 + "]" * 100_000 + "}")
    assert str(deep.value) == "merge response is not valid JSON"


def test_a_merge_answer_that_cannot_be_parsed_is_recorded_not_raised() -> None:
    import asyncio
    from types import SimpleNamespace

    from server.memory.app import consolidate

    class _Llm:
        async def complete(self, **_kw: object) -> object:
            return SimpleNamespace(text=["not", "text"])

    cluster = [{"memory_key": "a", "value": "x"}, {"memory_key": "b", "value": "y"}]
    _res, key, value, reason, _lossy = asyncio.run(consolidate._ask_merge(_Llm(), cluster))
    assert (key, value, reason) == ("", "", "AttributeError")
