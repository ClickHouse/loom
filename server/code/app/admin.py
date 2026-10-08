"""code.reindex / code.calibrate handlers (ADMIN, audited)."""

from __future__ import annotations

import json
import logging

from server.code.api.models_ingest import (
    CodeCalibrateInput,
    CodeCalibrateOutput,
    CodeReindexInput,
    CodeReindexOutput,
)
from server.code.app.common import require_code_intel, resolve_code_identity
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.core.paths import REPO_ROOT

logger = logging.getLogger(__name__)

_PROBES_PATH = REPO_ROOT / "bench" / "fixtures" / "code_calibration_probes.jsonl"


async def handle_code_reindex(input: CodeReindexInput) -> CodeReindexOutput:
    """Operator repair: move the git watermark (e.g. back N commits after a
    bad pass, or to a specific sha). The ingest worker re-walks from there;
    all writes are idempotent upserts, so re-walking is always safe."""
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if not input.git_head or len(input.git_head) != 40:
        raise ValidationError("git_head must be a full 40-char commit sha")
    repo = CodeRepo()
    previous = await repo.get_state(org, ns, "git_head")
    await repo.set_state(
        org,
        ns,
        "git_head",
        input.git_head,
        detail=f"code.reindex (was {previous[:12]})",
    )
    return CodeReindexOutput(previous_head=previous, new_head=input.git_head)


async def handle_code_calibrate(input: CodeCalibrateInput) -> CodeCalibrateOutput:
    """Derive code-specific abstention thresholds from the authored probe
    corpus (query/positive/hard-distractor triples over the indexed repo)
    and persist them as this namespace's calibration. Cosine distributions
    for code differ from prose — the namespace must not inherit
    prose-derived gates."""
    from server.memory.app.embedder_calibration import (
        calibrate_embedder,
        persist_namespace_calibration,
    )

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)

    if not _PROBES_PATH.exists():
        raise ValidationError(f"probe corpus missing: {_PROBES_PATH}")
    positives: list[tuple[str, str]] = []
    distractors: list[tuple[str, str]] = []
    for line in _PROBES_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        probe = json.loads(line)
        q = str(probe["query"])
        positives.append((q, str(probe["positive"])))
        if probe.get("distractor"):
            distractors.append((q, str(probe["distractor"])))
    if len(positives) < 5 or len(distractors) < 3:
        raise ValidationError(
            f"probe corpus too small: {len(positives)} positives, "
            f"{len(distractors)} distractors (need >=5 / >=3)"
        )

    calibration = await calibrate_embedder(
        # code question->chunk pairs play both the paraphrase and the
        # question-fact roles: retrieval here IS query->code, there is no
        # separate paraphrase register.
        paraphrase_pairs=positives,
        distractor_pairs=distractors,
        question_fact_pairs=positives,
    )
    if calibration is None:
        raise ValidationError(
            "calibration probe failed (fake embedder or provider error) — thresholds unchanged"
        )

    await persist_namespace_calibration(org, ns, calibration)
    return CodeCalibrateOutput(
        noise_floor=calibration.noise_floor,
        abstain_threshold=calibration.abstain_threshold,
        embedder_model=calibration.embedder_model,
        positives=len(positives),
        distractors=len(distractors),
    )
