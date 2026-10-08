"""Reflection distillation worker (default-off).

Supervisor adapter around `server.memory.app.session_outcomes`: label recent
sessions that carry no outcome (one cheap model call per session), then pair
each confidently failed session with its nearest confidently successful
neighbour and distil the difference into one `reflection` memory. Both passes
are bounded per interval by settings. Default-off because it makes LLM calls;
the read path never calls it — reflections stay hidden from unfiltered recall
 and are read with memory_type="reflection".
"""

from __future__ import annotations

import logging

from server.memory.app.session_outcomes import (
    DistillConfig,
    DistillResult,
    run_distillation,
)
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)


def _config_from_settings() -> DistillConfig:
    return DistillConfig(
        lookback_days=int(settings.reflection_distill_lookback_days),
        max_judged_per_pass=int(settings.reflection_distill_max_judged_per_pass),
        max_contrasts_per_pass=int(settings.reflection_distill_max_contrasts_per_pass),
        min_confidence=float(settings.reflection_distill_min_confidence),
        min_pair_similarity=float(settings.reflection_distill_min_pair_similarity),
        model=str(settings.reflection_distill_model),
    )


async def run_pass(cfg: DistillConfig | None = None) -> DistillResult:
    cfg = cfg or _config_from_settings()
    with record_pass("reflection_distill") as rec:
        result = await run_distillation(cfg)
        rec.outcome = {
            "sessions_scanned": result.sessions_scanned,
            "labelled": result.labelled,
            "by_outcome": dict(result.by_outcome),
            "contrast_candidates": result.contrast_candidates,
            "reflections_written": result.reflections_written,
            "unpaired": result.unpaired,
            "failures": len(result.failures),
        }
    log.info(
        "reflection distill pass: scanned=%d labelled=%d %s candidates=%d written=%d "
        "unpaired=%d failures=%d",
        result.sessions_scanned,
        result.labelled,
        result.by_outcome,
        result.contrast_candidates,
        result.reflections_written,
        result.unpaired,
        len(result.failures),
    )
    return result


__all__ = ["run_pass"]
