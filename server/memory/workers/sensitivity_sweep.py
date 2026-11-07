"""Daily sensitivity sweep.

`resolve_sensitivity` runs on `memory.set`, `memory.bulk_set`, `memory.update` and the
extractor's entries only. `observe.record`, `copy_to`, `namespace.restore` and the
imports write `sensitivity = ''` whatever the value holds, so a high value (a credential,
card number, SSN or stated PIN) or a contact detail that entered by one of those paths is
embedded and searchable. This pass runs the detector late over rows created in the
trailing window (`sensitivity_sweep_window_days`, 2) and labels what it finds the way the
write path would have — a new version per row, the `high` rows' embedding rows
tombstoned. Bounded per pass and limited to the shapes in `sensitivity_sweep_shapes`
(`credential`, which covers every high shape, and `email` by default — the phone shape
matches run ids and integer limits); the historical backlog is the operator's
`loom sensitivity-sweep --apply`, not this worker's.

A row behind more than `sensitivity_sweep_pages_per_pass` x
`sensitivity_sweep_max_rows_per_pass` selected-but-unlabelled keys of its own agent is not
reached by this worker until those keys leave the window: the cursor names no key inside an
agent, so each pass reads the same pages of it. `loom sensitivity-sweep --apply` reaches it.
The paging and the cursor rules are in docs/architecture/security.md, "Sensitivity".
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from server.core.repo_base import _CHClient
from server.memory.store.sensitivity_sweep_sql import AGENT_KEY, SORT_KEY, past_agent
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

if TYPE_CHECKING:
    from server.memory.app.sensitivity_sweep import ApplyResult, SweepReport

log = logging.getLogger(__name__)


async def run_pass(ch: _CHClient | None = None) -> dict[str, Any]:
    from server.core.repo_base import _default_client
    from server.memory.app import sensitivity_sweep as sweep
    from server.settings import settings
    from server.workers.registry import pass_deadline_seconds

    ch = ch or _default_client()
    window = max(1, int(settings.sensitivity_sweep_window_days))
    limit = int(settings.sensitivity_sweep_max_rows_per_pass)
    shapes = [x.strip() for x in str(settings.sensitivity_sweep_shapes).split(",") if x.strip()]
    start = cursor_key(pass_context.resume_cursor())
    with record_pass("sensitivity_sweep") as rec:
        report = await sweep.scan(
            since_days=window,
            limit=limit,
            page_size=limit,
            pages=int(settings.sensitivity_sweep_pages_per_pass),
            only_shapes=shapes or None,
            start_after=start,
            skip_noisy_agents=True,
            # The other half applies what the scan found and stores the cursor before the
            # supervisor cancels the pass.
            # not a setting: half the pass deadline, derived from it
            seconds=pass_deadline_seconds("sensitivity_sweep") / 2,
            ch=ch,
        )
        result = await sweep.apply(report.candidates, ch=ch)
        pass_context.set_cursor(
            cursor_text(report.resume_after, past=_resume_past_agent(report, result))
        )
        outcome = {"scanned": report.scanned, "truncated": report.truncated, **result.as_dict()}
        rec.outcome = outcome
        if result.relabelled or result.left or result.errors:
            log.info(
                "sensitivity_sweep: %d relabelled (%d high, %d medium), %d embedding rows "
                "tombstoned before the relabel (ClickHouse only), %d tagged, %d left (head "
                "gone or moved), %d errors",
                result.relabelled,
                result.high,
                result.medium,
                result.embeddings_tombstoned,
                result.embeddings_tagged,
                len(result.left),
                len(result.errors),
            )
        log_pass_errors(log, "sensitivity_sweep", result.errors)
    return outcome


def _resume_past_agent(report: SweepReport, result: ApplyResult) -> bool:
    """Whether the next pass starts past the agent this one stopped in (the cursor rules in
    docs/architecture/security.md, "Sensitivity")."""
    if result.relabelled or not report.resume_after:
        return False
    if len(report.resume_after) < len(SORT_KEY):
        return False  # the scan already stopped past an agent, a namespace or an org
    if not report.candidates:
        return report.last_page_had_stale_key
    stopped_in = past_agent(report.resume_after)
    if any(past_agent(c.sort_key) == stopped_in for c in result.left):
        return False  # a head gone or moved in this agent: the next pass reads the new one
    return bool(result.failed) and all(
        past_agent(c.sort_key) == stopped_in for c, _ in result.failed
    )


def cursor_text(key: dict[str, Any] | None, *, past: bool = False) -> str:
    """The agent of `key` as the pass's cursor: a JSON list of its org, namespace and agent,
    `[]` for the first key (never empty, which would keep the previous cursor).

    With `past`, or when `key` holds the first columns of the sort key only (`past_agent`),
    or when the agent does not fit the cursor store, the cursor names the place just past
    the agent instead, `{"past": [org, namespace, agent]}`, or past the namespace or the
    org when the longer cursor does not fit, or the first key when none does. A cursor
    before an agent too long to name would have the next pass read the same pages again."""
    if not key:
        return "[]"
    if not past and len(key) == len(SORT_KEY):
        # The cursor keeps the agent columns only; the user and memory key would outlive an erasure.
        text = json.dumps([str(key[col]) for col in AGENT_KEY], ensure_ascii=False)
        if len(text) <= pass_context.MAX_CURSOR:
            return text
    columns = [col for col in AGENT_KEY if col in key]
    while columns:
        text = json.dumps({"past": [str(key[col]) for col in columns]}, ensure_ascii=False)
        if len(text) <= pass_context.MAX_CURSOR:
            return text
        columns.pop()
    return "[]"


def cursor_key(text: str) -> dict[str, Any] | None:
    """The sort key a pass starts after: the agent `cursor_text` wrote, with the user,
    session and memory key empty, which sorts before every key in that agent that has a
    memory key; for a `past` cursor, its columns alone (`past_agent`). None for `[]`, no
    cursor or one it did not write."""
    try:
        parts = json.loads(text)
    except ValueError:
        return None
    if isinstance(parts, dict) and list(parts) == ["past"]:
        past = parts["past"]
        if not isinstance(past, list) or not 1 <= len(past) <= len(AGENT_KEY):
            return None
        return dict(zip(AGENT_KEY, map(str, past), strict=False))
    if not isinstance(parts, list) or len(parts) != len(AGENT_KEY):
        return None
    key = dict.fromkeys(SORT_KEY, "")
    key.update(zip(AGENT_KEY, map(str, parts), strict=True))
    return key


__all__ = ["cursor_key", "cursor_text", "run_pass"]
