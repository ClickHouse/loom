"""memory.apply and memory.sweep_expired: the writes that can touch more than one
row (wire models in server/memory/api/models_crud.py)."""

from __future__ import annotations

from server.memory.api.models_apply import MemoryApplyInput, MemoryApplyOutput
from server.memory.api.models_crud import MemorySweepExpiredInput, MemorySweepExpiredOutput


async def handle_memory_sweep_expired(inp: MemorySweepExpiredInput) -> MemorySweepExpiredOutput:
    from server.memory.app.expiry import ExpiryConfig, run_pass
    from server.settings import settings as _settings

    res = await run_pass(
        ExpiryConfig(
            batch_size=inp.limit,
            demote_factor=_settings.memory_expiry_demote_factor,
            demote_floor=_settings.memory_expiry_demote_floor,
            org_filter=inp.org,
            namespace_filter=inp.namespace,
            dry_run=inp.dry_run,
        )
    )
    return MemorySweepExpiredOutput(**res.as_dict())


async def handle_memory_apply(inp: MemoryApplyInput) -> MemoryApplyOutput:
    from server.memory.app.apply import apply_one, resolve

    rows, truncated = await resolve(inp)
    out = MemoryApplyOutput(
        action=inp.action,
        dry_run=inp.dry_run,
        target=inp.target.kind,
        matched=len(rows),
        truncated=truncated,
    )
    for row in rows:
        r = await apply_one(row, inp)
        out.rows.append(r)
        if r.status == "applied":
            out.applied += 1
        elif r.status == "would_apply":
            out.would_apply += 1
        elif r.status == "refused":
            out.refused += 1
        elif r.status == "noop":
            out.noop += 1
        else:
            out.failed += 1
    return out
