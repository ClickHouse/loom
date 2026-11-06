"""A caller's request id never breaks the request or its ClickHouse attribution.

The request id is the second word of the query tag and the first field of a request
statement's `query_id`. A caller id is kept only as one short token, and two requests
that carry the same id send different `query_id`s: ClickHouse refuses one that is
already running (code 216).
"""

from __future__ import annotations

import contextvars
import uuid
from pathlib import Path

import pytest
from loom import transport as py_sdk

from server.core import request_work as rw
from server.core.ch import _QUERY_TAG_MAX
from server.core.ch_request_cost import request_query_id
from server.operations.registry import ALL_OPERATIONS
from server.transport._common import _CALLER_REQUEST_ID, request_id_from


@pytest.mark.parametrize(
    "given", ["dbg-0001", "rid_7.retry", "a" * 64, "a09a91b4-3492-4ab2-afa3-4be022d4dde6"]
)
def test_a_short_token_is_used_as_sent(given: str) -> None:
    assert request_id_from(given) == given


@pytest.mark.parametrize("given", [None, "", "two words", "a" * 65, "x:y", "id\nnext", "é"])
def test_anything_else_gets_a_generated_uuid(given: str | None) -> None:
    made = request_id_from(given)
    assert made != given
    assert str(uuid.UUID(made)) == made


def test_two_requests_with_one_id_send_different_query_ids() -> None:
    def statements() -> list[str]:
        rw.begin()
        return [request_query_id("memory.search dbg-0001") or "" for _ in range(2)]

    first = contextvars.copy_context().run(statements)
    second = contextvars.copy_context().run(statements)
    assert not set(first) & set(second)
    for qid in first + second:
        assert qid.split(":")[0] == "dbg-0001"
    assert [q.split(":")[1] for q in first] == ["1", "2"]
    assert len({q.split(":")[2] for q in first}) == 1


def test_the_longest_op_and_a_kept_id_fit_the_query_tag() -> None:
    """A kept id must fit whole in the tag, or the cut drops its tail and the
    query_id no longer matches log_comment."""
    longest = max(len(op.name) for op in ALL_OPERATIONS)
    assert longest + 1 + 64 <= _QUERY_TAG_MAX


def test_the_sdks_keep_the_ids_the_server_keeps() -> None:
    """Each SDK replaces an id the server would replace, so both name one id."""

    assert py_sdk._REQUEST_ID.pattern == _CALLER_REQUEST_ID.pattern
    ts = Path(__file__).parents[2] / "sdk/typescript/src/transport.ts"
    assert f"REQUEST_ID = /^{_CALLER_REQUEST_ID.pattern}$/;" in ts.read_text()
