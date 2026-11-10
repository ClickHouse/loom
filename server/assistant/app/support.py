"""An answer's support (server/assistant/domain/support.py), from its checked citations and,
for an answer that cites nothing, what its own tool calls found (`ledger.run_calls`).

The calls ran before the model wrote the text, but their ledger rows land in the background
(and a call may land on another replica): the run is read once and, unless that read
already decides the answer is unsupported, once more after the rows' usual delay.
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


async def answer_support(
    checked: list[Checked], text: str, *, org: str, run_id: str, subject: str
) -> str:
    """The answer's support: `supported`, `unsupported`, `unchecked` or `none`."""
    citation_codes = [c.code for c in checked]
    returned: frozenset[str] | None = frozenset()
    failed = False
    ran: frozenset[str] = frozenset()
    if not citation_codes and text.strip() and not rule.is_abstention(text):
        returned, failed, ran = await _calls(org, run_id, subject)
    return rule.support_of(
        citation_codes,
        text=text,
        returned=returned,
        failed=failed,
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
        ran=ran,
    )


async def _wait(seconds: float) -> None:
    """The pause before the re-read: a seam the tests replace."""
    await asyncio.sleep(seconds)


async def _calls(
    org: str, run_id: str, subject: str
) -> tuple[frozenset[str] | None, bool, frozenset[str]]:
    """(the ops whose calls found data, whether a call failed, the ops whose calls ran); the
    first is None when the run cannot be read (no run id, a failed read)."""
    if not run_id:
        return None, False, frozenset()
    try:
        async with asyncio.timeout(_CALLS_S):
            calls = await ledger.run_calls(org, run_id, subject)
            if not calls.returned & ASSISTANT_CITABLE_OPS:
                delay = FLUSH_INTERVAL_S + settings.ch_batched_insert_wait_max_ms / 1000
                await _wait(delay + _INSERT_SLACK_S)
                calls = await ledger.run_calls(org, run_id, subject)
    except TimeoutError:
        log.warning("reading an answer's tool calls ran past %s s", _CALLS_S)
        return None, False, frozenset()
    except Exception as e:  # noqa: BLE001 — a check that cannot run never fails an answer
        log.warning("reading an answer's tool calls failed: %s", e)
        return None, False, frozenset()
    return calls.returned, calls.failed, calls.ran
