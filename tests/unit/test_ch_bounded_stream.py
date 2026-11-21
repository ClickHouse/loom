"""The canonical byte reader rejects malformed or truncated raw streams."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest

from server.core.ch_bounded import BoundedReadError, read_rows
from server.insights.domain.cell_observations import Attempt
from server.insights.store.cell_observations import _Reads


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = 0

    @asynccontextmanager
    async def raw_stream(self, sql, params=None, *, settings=None):
        async def chunks():
            for chunk in self.chunks:
                yield chunk

        try:
            yield chunks()
        finally:
            self.closed += 1


@pytest.mark.parametrize(
    "chunks",
    [
        [b'{"x":1,"x":2}\n'],
        [b'{"x":NaN}\n'],
        [b'{"x":1e999}\n'],
        [b'{"x":1}'],
        [b'{"x":"\xff"}\n'],
        [b"[]\n"],
        [b"{}\n{}\n"],
    ],
)
async def test_malformed_incomplete_or_extra_rows_fail_closed_and_close(chunks):
    ch = Stream(chunks)
    with pytest.raises(BoundedReadError):
        await read_rows(
            "SELECT 1",
            {},
            ch=ch,
            deadline=time.monotonic() + 1,
            row_bytes=32,
            total_bytes=64,
            max_rows=1,
            tag="cell.attach.census",
        )
    assert ch.closed == 1


async def test_chunk_boundaries_and_exact_byte_allowances_preserve_rows():
    ch = Stream([b'{"', b'x":1}', b"\n"])
    assert await read_rows(
        "SELECT 1",
        {},
        ch=ch,
        deadline=time.monotonic() + 1,
        row_bytes=7,
        total_bytes=8,
        max_rows=1,
        tag="cell.attach.census",
    ) == [{"x": 1}]
    assert ch.closed == 1


async def test_complete_read_aggregates_stream_bytes_but_next_poll_retains_only_obligations(
    monkeypatch,
):
    from server.insights.domain import cell_observations as domain

    deadline = time.monotonic() + 1
    attempt = Attempt()
    monkeypatch.setattr(domain, "METADATA_BYTES", 8)
    ch = Stream([b'{"x":1}\n'])
    reader = _Reads(ch, attempt, deadline)
    assert await reader.query_all("SELECT 1") == [{"x": 1}]
    with pytest.raises(BoundedReadError):
        await reader.query_all("SELECT 1")
    next_poll = _Reads(ch, attempt, deadline)
    assert await next_poll.query_all("SELECT 1") == [{"x": 1}]
    assert attempt.deadline == deadline and next_poll.raw_bytes_used == 8
    assert attempt.retained_bytes == 0


async def test_next_poll_cannot_reset_the_original_workflow_allowance():
    from server.core.cell_ch import Refused

    attempt = Attempt()
    deadline = time.monotonic() + 1
    _Reads(Stream([]), attempt, deadline)
    with pytest.raises(Refused, match="reset"):
        _Reads(Stream([]), attempt, deadline + 1)
