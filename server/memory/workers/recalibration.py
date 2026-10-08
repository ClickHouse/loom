"""Recalibration worker — re-runs the embedder calibration probe so threshold
values stay aligned if the vendor silently revises the embedder.

Cadence via LOOM_RECALIBRATION_INTERVAL_SECONDS (weekly default). A pass is
recorded like every worker's (`record_pass`): a probe that raises fails the
pass and keeps the current calibration; an unavailable calibration (fake
embedder, no key) is a successful pass that changed nothing. Per-namespace
thresholds come from `code.calibrate`, not from this pass.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass
class RecalibrationResult:
    ran: bool = False
    skipped_reason: str = ""
    drift_warnings: list[str] = field(default_factory=list)
    new_calibration_at: datetime | None = None


async def run_pass() -> RecalibrationResult:
    """Re-run the calibration probe, diff percentiles, update the singleton.
    Raises when the probe fails; the current calibration stays."""
    from server.workers.status import record_pass

    with record_pass("recalibration") as rec:
        result = await _recalibrate()
        rec.outcome = {
            "ran": result.ran,
            "skipped_reason": result.skipped_reason,
            "drift_warnings": len(result.drift_warnings),
        }
    return result


async def _recalibrate() -> RecalibrationResult:
    from server.memory.app import embedder_calibration

    result = RecalibrationResult()

    # Capture before the probe so we can diff.
    old = embedder_calibration.get_calibration()

    try:
        new = await embedder_calibration.calibrate_embedder()
    except Exception as e:
        log.warning("recalibration: probe failed (%s); the current calibration stays", e)
        raise

    if new is None:
        # Fake embedder or API key missing — same fallback as boot.
        log.info(
            "recalibration: calibration unavailable (fake embedder or "
            "missing key); leaving singleton unchanged"
        )
        result.skipped_reason = "calibration unavailable"
        return result

    # 5pp drift is enough to materially change which queries trip which gate.
    DRIFT_THRESHOLD = 0.05
    if old is not None:
        for attr, label in (
            ("similar_p25", "P25 of paraphrase pairs"),
            ("similar_p50", "P50 of paraphrase pairs"),
            ("similar_p75", "P75 of paraphrase pairs"),
            ("distractor_p90", "P90 of distractor pairs"),
            ("noise_floor", "derived noise floor"),
            ("rerank_skip_above", "derived rerank skip"),
            ("hyde_fallback_threshold", "derived HyDE fallback"),
            ("conflict_threshold", "derived conflict threshold"),
            ("duplicate_threshold", "derived duplicate threshold"),
        ):
            old_v = getattr(old, attr, 0.0)
            new_v = getattr(new, attr, 0.0)
            if math.fabs(new_v - old_v) >= DRIFT_THRESHOLD:
                msg = (
                    f"{label} drifted {old_v:.3f} → {new_v:.3f} "
                    f"(|Δ| = {math.fabs(new_v - old_v):.3f}). The "
                    f"embedder's similarity distribution shifted; "
                    f"operators should re-bench the affected workload."
                )
                log.warning("recalibration drift: %s", msg)
                result.drift_warnings.append(msg)

    embedder_calibration.set_calibration(new)
    result.ran = True
    result.new_calibration_at = datetime.now(UTC)

    if not result.drift_warnings:
        log.info(
            "recalibration: probe rerun OK; percentiles stable within %.2fpp of prior calibration",
            DRIFT_THRESHOLD * 100,
        )

    # Found per-namespace rows stay cached until cleared: re-read what
    # code.calibrate wrote from another process since.
    embedder_calibration.clear_ns_overrides()
    return result
