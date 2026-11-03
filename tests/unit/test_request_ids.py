"""A caller's request id never breaks the request or its ClickHouse attribution.

The request id is the second word of the query tag and the first field of every
statement's `query_id`. A caller id with a space split the tag (the query_id took
the first word only), and two requests sent at once with one id sent the same
`query_id`, which ClickHouse refuses while the first runs (code 216), so the
second answered 500.
"""

from __future__ import annotations

import contextvars
import uuid

import pytest

from server.core import request_work as rw
from server.core.ch_request_cost import request_query_id
from server.transport._common import request_id_from


@pytest.mark.parametrize("given", ["dbg-0001", "rid_7.retry", "a" * 64, str(uuid.uuid4())])
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
