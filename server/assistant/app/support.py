"""An answer's support (server/assistant/domain/support.py), from its checked citations and,
for an answer that cites nothing, what its own tool calls found (`ledger.run_calls`).

The calls ran before the model wrote the text, but their ledger rows land in the background
(and a call may land on another replica): the run is read once and, unless a citable op's
call already found data, once more after the rows' usual delay.
"""

from __future__ import annotations

import asyncio
import logging

from server.assistant.app.citations import Checked
from server.assistant.domain import support as rule
from server.assistant.store import ledger
from server.observability.events import FLUSH_INTERVAL_S
from server.operations.toolsets import ASSISTANT_CITABLE_OPS, ASSISTANT_NO_NAMESPACE_DATA_OPS
from server.settings import settings

log = logging.getLogger(__name__)

# Past the ledger's flush interval and the batched-insert wait, the slack of one insert.
_INSERT_SLACK_S = 0.2  # not a setting: a fraction of one ClickHouse insert
# Both reads and the wait between them: past it the answer is unchecked, and its slot and
# its last event are not held on a slow ClickHouse.
_CALLS_S = 3.0  # not a setting: a fraction of the citation checks' bound
# The calls of an answer whose support needs none of them.
NO_CALLS = ledger.RunCalls(frozenset(), False, frozenset())


async def answer_support(
    checked: list[Checked], text: str, *, org: str, run_id: str, subject: str
) -> str:
    """The answer's support: `supported`, `unsupported`, `unchecked` or `none`."""
    citation_codes = [c.code for c in checked]
    # A cited answer, an empty one or an abstention needs no read of its calls.
    calls: ledger.RunCalls | None = NO_CALLS
    if not citation_codes and text.strip() and not rule.is_abstention(text):
        calls = await _calls(org, run_id, subject)
    return rule.support_of(
        citation_codes,
        text=text,
        returned=None if calls is None else calls.returned,
        failed=calls is not None and calls.failed,
        ran=frozenset() if calls is None else calls.ran,
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
    )


async def _wait(seconds: float) -> None:
    """The pause before the re-read: a seam the tests replace."""
    await asyncio.sleep(seconds)


async def _calls(org: str, run_id: str, subject: str) -> ledger.RunCalls | None:
    """The answer's calls; None when the run cannot be read (no run id, a failed read, past
    the bound)."""
    if not run_id:
        return None
    try:
        async with asyncio.timeout(_CALLS_S):
            calls = await ledger.run_calls(org, run_id, subject)
            if not calls.returned & ASSISTANT_CITABLE_OPS:
                delay = FLUSH_INTERVAL_S + settings.ch_batched_insert_wait_max_ms / 1000
                await _wait(delay + _INSERT_SLACK_S)
                calls = await ledger.run_calls(org, run_id, subject)
    except TimeoutError:
        log.warning("reading an answer's tool calls ran past %s s", _CALLS_S)
        return None
    except Exception as e:  # noqa: BLE001 — a check that cannot run never fails an answer
        log.warning("reading an answer's tool calls failed: %s", e)
        return None
    return calls
