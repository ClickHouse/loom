"""The ingest surface: code.refresh (CI merge trigger), code.scip_ingest /
code.scip_status (compiler-tier upload), and the admin ops code.reindex /
code.calibrate.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from server.code.api.models_common import _CodeIdentity, _git_sha

# ============================================================
# code.refresh (CI merge trigger)
# ============================================================


class CodeRefreshInput(_CodeIdentity):
    """CI calls this right after a merge lands: run one ingest pass now
    instead of waiting for the poll interval. The pass covers ONLY the
    addressed namespace, which must be a configured repo entry (else 404) —
    the branch indexed is whatever that entry's git_branch says, never the
    pusher's choice. expected_head is advisory (logged; the pass always
    indexes to the fetched branch tip)."""

    # The commit CI just merged; advisory, recorded in `last_refresh_detail` and never checked.
    expected_head: str = ""
    # wait=True blocks until the pass completes (CI wants a sync signal);
    # default fire-and-forget returns immediately.
    wait: bool = False
    # With `wait`: seconds to wait for the pass (10..900); the call fails when the pass outlasts it.
    wait_timeout_seconds: int = Field(default=300, ge=10, le=900)


class CodeRefreshOutput(BaseModel):
    # True when this call started an ingest pass; false when one was already running.
    triggered: bool
    # True when a pass for this namespace was already in flight; this call started none.
    already_running: bool = False
    # The fast-tier watermark (`git_head`) before the pass; '' when the namespace has none.
    head_before: str = ""
    # With `wait`: the fast-tier watermark after the pass; '' otherwise.
    head_after: str = ""


# ============================================================
# code.scip_ingest / code.scip_status (compiler-tier upload)
# ============================================================


class CodeScipIngestInput(_CodeIdentity):
    # The staging tables key on a UUID column: a malformed id is refused
    # here, not by the insert. Dashed and bare-hex spellings both pass.
    batch_id: str = Field(min_length=8, max_length=64, json_schema_extra={"format": "uuid"})
    # This part's position in the batch, 0-based (`0 <= seq < total_parts`).
    seq: int = Field(ge=0)
    # How many parts the batch has; capped by `code_scip_max_parts`.
    total_parts: int = Field(ge=1)
    # The commit the index was built at; once applied it becomes the producer's tier watermark.
    commit_sha: str = ""
    # The commit the delta was computed from; recorded on the batch row.
    base_sha: str = ""
    # The indexer that built the delta (scip-clang, scip-python, scip-typescript, scip-go); it
    # picks the watermark key the batch advances, '' = the C++ key `scip_commit`.
    producer: str = ""
    payload: str  # one part of the JSON-encoded ScipDelta
    # The holder of the entry's lease the delta was built under (`loom code entry-lock`
    # hands it to its command); when set, the part is refused unless that holder still
    # holds the lease. '' = unfenced (CI pushes).
    entry_lease_holder: str = Field(default="", max_length=200)

    # The applied batch's commit becomes a tier watermark, which later
    # bounds `git log` / `rev-list` windows.
    @field_validator("commit_sha", "base_sha")
    @classmethod
    def _commit_shas(cls, v: str) -> str:
        return _git_sha(v)

    @field_validator("batch_id")
    @classmethod
    def _batch_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("must be a UUID") from None
        return v


class CodeScipIngestOutput(BaseModel):
    # The batch this part was stored under, as sent.
    batch_id: str
    # Distinct parts of the batch stored so far.
    parts_received: int
    # The batch's part count, as sent.
    total_parts: int
    # True once every part is stored; the apply worker then picks the batch up.
    complete: bool


class CodeScipStatusInput(_CodeIdentity):
    # The UUID the parts were uploaded under.
    batch_id: str


class CodeScipStatusOutput(BaseModel):
    # The batch asked about.
    batch_id: str
    status: str  # pending | applied | failed
    # Distinct parts of the batch stored so far.
    parts_received: int
    # The batch's part count, as uploaded.
    total_parts: int
    # Why the apply failed (exception type and message); '' unless `status` is failed.
    error: str = ""
    # The commit the batch was built at.
    commit_sha: str = ""
    # When the apply worker applied the batch; '' until then.
    applied_at: str = ""


# ============================================================
# code.reindex / code.calibrate (admin)
# ============================================================


class CodeReindexInput(_CodeIdentity):
    git_head: str  # full 40-char sha to move the ingest watermark to

    @field_validator("git_head")
    @classmethod
    def _git_head_sha(cls, v: str) -> str:
        return _git_sha(v)


class CodeReindexOutput(BaseModel):
    # The fast-tier watermark before the move; '' when it was unset.
    previous_head: str
    # The watermark now set; the ingest worker re-walks from it.
    new_head: str


class CodeCalibrateInput(_CodeIdentity):
    pass


class CodeCalibrateOutput(BaseModel):
    # Cosine below which a hit is noise: the distractors' 90th percentile plus 0.02.
    noise_floor: float
    # Cosine below which the search abstains: `noise_floor` plus 0.05.
    abstain_threshold: float
    # The embedding model the probes ran against; the calibration applies to it.
    embedder_model: str
    # Query/positive pairs read from the probe corpus.
    positives: int
    # Query/distractor pairs read from the probe corpus.
    distractors: int
