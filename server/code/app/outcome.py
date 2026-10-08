"""code.outcome: an agent's verdict on a served answer, recorded and folded back into
the cached answer it names."""

from __future__ import annotations

import json
import logging
from typing import Any

from server.code.api.models_outcome import (
    SHUNT_OUTCOME_ACTIONS,
    CodeOutcomeInput,
    CodeOutcomeOutput,
)
from server.code.app.common import (
    record_code_outcome,
    record_code_shunt,
    require_code_intel,
    resolve_code_identity,
)
from server.code.app.spans import set_code_span_attributes
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def handle_code_outcome(input: CodeOutcomeInput) -> CodeOutcomeOutput:
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    is_shunt = input.action in SHUNT_OUTCOME_ACTIONS
    set_code_span_attributes(
        kind=input.action,
        results=len(input.targets),
        tokens_returned=input.tokens_saved if is_shunt else 0,
        # shunt_pass: the pass reason (fixed vocabulary);
        # fell_back_grep / ignored: the outcome reason;
        # otherwise the client. All closed sets.
        source=input.pass_reason or input.outcome_reason or input.source,
    )
    if not is_shunt:
        # The table row is the durable counter; the OTel counter puts the
        # same series next to latency, a negative outcome split by its reason.
        record_code_outcome(action=input.action, reason=input.outcome_reason, namespace=ns)
    if is_shunt:
        # The plugin batches decisions: one target per decision. The table
        # row is the durable counter; the OTel counters mirror it, a pass
        # split by its reason.
        record_code_shunt(
            decision="hit" if input.action == "shunt_hit" else "pass",
            count=max(1, len(input.targets)),
            tokens_saved=input.tokens_saved,
            namespace=ns,
            reason=input.pass_reason,
        )
    if not settings.code_trace_log_enabled:
        return CodeOutcomeOutput(recorded=False)

    targets = input.targets[:50]
    spans = (input.spans + [""] * len(targets))[: len(targets)]  # aligned, padded
    row: dict[str, Any] = {
        "org": org,
        "namespace": ns,
        "trace_run_id": input.trace_run_id,
        # fell_back_grep / ignored with a `reason`: the reason rides `source` (as a shunt
        # pass's reason does); every other row keeps the client's
        "action": input.action,
        "targets": targets,
        "source": input.stored_source,
        "tokens_saved": input.tokens_saved,
        "spans": spans,
    }
    from server.core.ch import get_client

    try:
        await get_client().client.insert(
            "loom.code_search_outcome",
            [list(row.values())],
            column_names=list(row.keys()),
            settings={"async_insert": 1, "wait_for_async_insert": 0},
        )
    except Exception as e:  # noqa: BLE001 — outcome telemetry is best-effort
        logger.debug("code.outcome write failed: %s", e)
        return CodeOutcomeOutput(recorded=False)

    if is_shunt:
        return CodeOutcomeOutput(recorded=True)  # no trace to fold onto

    # Fold the outcome into per-unit ranking signal (automatic). Best-effort: a failed fold
    # loses one vote, never the op. A negative outcome on a cached code.ask answer also
    # tombstones the entry it was served from.
    try:
        await _fold_outcome_signal(org, ns, input)
    except Exception as e:  # noqa: BLE001
        logger.debug("code.outcome signal fold failed: %s", e)
    return CodeOutcomeOutput(recorded=True)


# Ops whose trace `returned_ids` are unit ids (symbol/chunk ids). The other
# traced ops (grep, history, context_pack, map, verify) return paths / PR
# numbers / pack ids there, which must not become unit-signal rows.
_FOLD_OPS: frozenset[str] = frozenset(
    {
        "code.search",
        "code.ask",
        "code.symbol",
        "code.trace",
        "code.callers",
        "code.callees",
        "code.impact",
        "code.examples",
    }
)


async def _fold_outcome_signal(org: str, ns: str, input: CodeOutcomeInput) -> None:
    """Map the outcome onto the trace's returned units:

    opened_file     +1.0 to the unit(s) whose path the agent opened
    used_in_answer  +0.6 to every returned unit (the set served the answer)
    fell_back_grep  -0.5 to every returned unit — unless the search
                    ABSTAINED, in which case grep was exactly what we told
                    the agent to do and no unit is punished
    ignored         -0.2 to every returned unit
    """
    repo = CodeRepo()
    trace = await repo.trace_by_run_id(org, ns, input.trace_run_id)
    if trace is None:
        return
    if input.action in _NEGATIVE_OUTCOMES:
        await _invalidate_cached_answer(repo, org, trace)
    if str(trace.get("op") or "code.search") not in _FOLD_OPS:
        return
    ids = [str(x) for x in (trace.get("returned_ids") or [])]
    paths = [str(x) for x in (trace.get("returned_paths") or [])]
    if not ids:
        return
    deltas: list[tuple[str, float, float]] = []
    if input.action == "opened_file":
        opened = set(input.targets)
        for uid, path in zip(ids, paths, strict=False):
            if any(t.endswith(path) or path.endswith(t) for t in opened):
                deltas.append((uid, 1.0, 0.0))
    elif input.action == "used_in_answer":
        deltas = [(uid, 0.6, 0.0) for uid in ids]
    elif input.action == "fell_back_grep":
        if not bool(trace.get("abstain")):
            deltas = [(uid, 0.0, 0.5) for uid in ids]
    elif input.action == "ignored":
        deltas = [(uid, 0.0, 0.2) for uid in ids]
    await repo.add_unit_signals(org, ns, deltas)


# The outcomes that say a served answer did not serve.
_NEGATIVE_OUTCOMES: frozenset[str] = frozenset({"fell_back_grep", "ignored"})


async def _invalidate_cached_answer(repo: CodeRepo, org: str, trace: dict[str, Any]) -> int:
    """Tombstone the `code.ask` cache entry a negative outcome's trace was
    served from — a cached answer an agent had to grep past is not served to
    the next one. Only a trace whose route is `cached` names an entry (a
    negative-cache abstain told the agent to grep; nothing to punish)."""
    if str(trace.get("op") or "") != "code.ask" or str(trace.get("route") or "") != "cached":
        return 0
    try:
        args = json.loads(str(trace.get("args_json") or "") or "{}")
    except ValueError:
        return 0
    entry = args.get("cache_entry") if isinstance(args, dict) else None
    if not isinstance(entry, dict):
        return 0
    from server.code.app.answers_cache import tombstone_served_entry

    return await tombstone_served_entry(repo, org, entry)
