"""An answer's support (server/assistant/domain/support.py), from its checked citations and
what its own tool calls found (`ledger.run_rows`).

The calls ran before the model wrote the text, but their ledger rows land in the background
(and a call may land on another replica). Two patterns read them:

- a whole answer (assistant.ask, the chat route asked without `stream`) reads its run only
  when it cites nothing: once and, unless a citable op's call already found data, once more
  after the rows' usual delay (`answer_support`);
- a streamed answer shows what it read under it, so its run is read on every answer, once,
  after the rows' usual delay (`streamed_calls`), and that one read serves both its support
  and its steps (`streamed_support`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from server.assistant.app.citations import Checked
from server.assistant.domain import support as rule
from server.assistant.store import ledger
from server.observability.events import FLUSH_INTERVAL_S
from server.operations.toolsets import ASSISTANT_CITABLE_OPS, ASSISTANT_NO_NAMESPACE_DATA_OPS
from server.settings import settings

log = logging.getLogger(__name__)

# Past the ledger's flush interval and the batched-insert wait, the slack of one insert.
_INSERT_SLACK_S = 0.2  # not a setting: a fraction of one ClickHouse insert
# Every read of a run and the wait before or between them: past it the answer is unchecked
# (and a streamed one shows no steps), and its slot and its last event are not held on a slow
# ClickHouse.
_CALLS_S = 3.0  # not a setting: a fraction of the citation checks' bound


async def answer_support(
    checked: list[Checked],
    text: str,
    *,
    org: str,
    run_id: str,
    subject: str,
    unknown_names: list[str] | None = None,
) -> str:
    """A whole answer's support: `supported`, `unsupported`, `unchecked` or `none`."""
    citation_codes = [c.code for c in checked]
    # A cited answer, an empty one or an abstention is decided without its calls.
    needs_calls = (
        not citation_codes and not unknown_names and text.strip() and not rule.is_abstention(text)
    )
    calls = await _calls(org, run_id, subject) if needs_calls else None
    return rule.support_of(
        citation_codes,
        text=text,
        calls=calls,
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
        unknown_names=unknown_names,
    )


def streamed_support(
    checked: list[Checked],
    text: str,
    rows: list[rule.CallRow] | None,
    *,
    unknown_names: list[str] | None = None,
) -> str:
    """A streamed answer's support, from its citations and the one read of its run (`rows`,
    None when it could not be read)."""
    return rule.support_of(
        [c.code for c in checked],
        text=text,
        calls=None if rows is None else rule.derive(rows),
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
        unknown_names=unknown_names,
    )


async def _wait(seconds: float) -> None:
    """The pause before a read: a seam the tests replace."""
    await asyncio.sleep(seconds)


def _delay() -> float:
    """The rows' usual delay: the ledger's flush interval, the batched-insert wait and the
    slack of one insert."""
    return FLUSH_INTERVAL_S + settings.ch_batched_insert_wait_max_ms / 1000 + _INSERT_SLACK_S


async def _bounded[T](read: Callable[[], Awaitable[T]]) -> T | None:
    """`read`, bounded by _CALLS_S with its waits; None when it fails or runs past it."""
    try:
        async with asyncio.timeout(_CALLS_S):
            return await read()
    except TimeoutError:
        log.warning("reading an answer's tool calls ran past %s s", _CALLS_S)
    except Exception as e:  # noqa: BLE001 — a check that cannot run never fails an answer
        log.warning("reading an answer's tool calls failed: %s", e)
    return None


async def streamed_calls(org: str, run_id: str, subject: str) -> list[rule.CallRow] | None:
    """A streamed answer's rows: one read after the rows' usual delay; None when the run
    cannot be read (no run id, a failed read, past the bound)."""
    if not run_id:
        return None

    async def read() -> list[rule.CallRow]:
        await _wait(_delay())
        return await ledger.run_rows(org, run_id, subject)

    return await _bounded(read)


async def _calls(org: str, run_id: str, subject: str) -> rule.RunCalls | None:
    """The answer's calls, read again after the rows' usual delay unless a citable op's call
    already found data; None when the run cannot be read (no run id, a failed read, past
    the bound)."""
    if not run_id:
        return None

    async def read() -> rule.RunCalls:
        calls = rule.derive(await ledger.run_rows(org, run_id, subject))
        if not calls.returned & ASSISTANT_CITABLE_OPS:
            await _wait(_delay())
            calls = rule.derive(await ledger.run_rows(org, run_id, subject))
        return calls

    return await _bounded(read)
