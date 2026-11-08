"""Apply staged SCIP deltas to the compiler-grade tier.

Batches arrive via code.scip_ingest as chunked JSON parts; this loop finds
pending batches whose every part is present, reassembles the delta, applies
it (upsert tier='scip' symbols/edges per touched file, tombstone what the
delta stopped emitting), and marks the batch applied. Re-apply after a
crash is idempotent — apply converges on the same live set.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

import pydantic

from server.core.datetimes import naive_utc_now
from server.core.errors import ValidationError, caller_text
from server.workers import pass_context
from server.workers.status import record_pass

logger = logging.getLogger("loom.workers.code_scip_apply")


@dataclass
class CodeScipApplyResult:
    batches_applied: int = 0
    batches_failed: int = 0
    files: int = 0
    symbols_new: int = 0
    edges_new: int = 0
    errors: list[str] = field(default_factory=list)


def watermark_key_for(producer: str) -> str:
    """scip_commit stays the C++ key (freshness gauge + delta worker read
    it); python producers advance their own — see the comment at the call
    site for why sharing one key regresses windows."""
    low = producer.lower()
    if "python" in low:
        return "scip_commit_py"
    if "typescript" in low:
        return "scip_commit_ts"
    if low in ("go", "scip-go") or low.startswith("scip-go"):
        return "scip_commit_go"
    return "scip_commit"


async def run_pass() -> CodeScipApplyResult:
    result = CodeScipApplyResult()
    with record_pass("code_scip_apply") as rec:
        await _run(result)
        rec.outcome = {
            "batches_applied": result.batches_applied,
            "batches_failed": result.batches_failed,
            "files": result.files,
            "symbols_new": result.symbols_new,
            "edges_new": result.edges_new,
        }
    return result


async def _run(result: CodeScipApplyResult) -> None:
    from server.code.app.index.scip_delta import ScipDelta, apply_delta
    from server.code.store.index import CodeRepo

    repo = CodeRepo()
    # Every namespace with a complete pending batch, not only the registry's:
    # the upload was already gated by a writer token on that namespace, and a
    # namespace indexed by the CLI alone has no other way to receive its
    # compiler tier. One statement for all of them.
    batches = await repo.pending_scip_batches(per_namespace=3)
    for batch in batches:
        org, ns = str(batch["org"]), str(batch["namespace"])
        batch_id = str(batch["batch_id"])
        commit = _fixed_str(batch.get("commit_sha"))
        try:
            parts = await repo.scip_parts(org, ns, batch_id)
            payload = "".join(str(p["payload"]) for p in parts)
            for p in parts:
                got = hashlib.sha256(str(p["payload"]).encode()).hexdigest()
                want = _fixed_str(p.get("payload_hash"))
                if want and got != want:
                    raise ValidationError(f"part {p['seq']} hash mismatch")
            try:
                delta = ScipDelta.model_validate_json(payload)
            except pydantic.ValidationError as e:
                # The uploader reads `code.scip_status`: the first bad field, not its value.
                where = ".".join(str(x) for x in e.errors()[0]["loc"]) or "the top"
                raise ValidationError(f"the batch is not a SCIP delta (at {where})") from e
            if delta.delta_version != 1:
                raise ValidationError(f"unsupported delta_version {delta.delta_version}")
            stats = await apply_delta(repo, org, ns, delta)
            await repo.upsert_scip_batch(
                org,
                ns,
                batch_id,
                commit_sha=commit,
                base_sha=_fixed_str(batch.get("base_sha")),
                total_parts=int(batch.get("total_parts") or 0),
                status="applied",
                producer=str(batch.get("producer") or ""),
                applied_at=naive_utc_now(),
                created_at=batch.get("created_at"),
            )
            # Producer-scoped watermark: the python tier (scip-python) and
            # the C++ tier (scip-clang) run on different toolchains and
            # cadences — a shared key would let a python backfill at an
            # older commit REGRESS the C++ watermark and re-widen its next
            # delta window. "scip_commit" stays the C++ key (the freshness
            # gauge and delta worker read it); python batches advance
            # "scip_commit_py".
            producer = str(batch.get("producer") or "")
            await repo.set_state(org, ns, watermark_key_for(producer), commit, detail=batch_id)
            result.batches_applied += 1
            result.files += stats.files
            result.symbols_new += stats.symbols_new
            result.edges_new += stats.edges_new
        except Exception as e:
            logger.exception("code_scip_apply: batch %s failed", batch_id)
            result.batches_failed += 1
            result.errors.append(f"{batch_id}: {type(e).__name__}: {e}")
            pass_context.note_failure(e, ns)
            try:
                await repo.upsert_scip_batch(
                    org,
                    ns,
                    batch_id,
                    commit_sha=commit,
                    base_sha=_fixed_str(batch.get("base_sha")),
                    total_parts=int(batch.get("total_parts") or 0),
                    status="failed",
                    # `code.scip_status` returns it: the kind, or Loom's words about the
                    # uploader's batch (`VALIDATION_FAILED: ...`); the text is logged above.
                    error=caller_text(e)[:500],
                    producer=str(batch.get("producer") or ""),
                    created_at=batch.get("created_at"),
                )
            except Exception as mark_err:  # noqa: BLE001 — the batch stays pending; the rest apply
                logger.warning("code_scip_apply: marking %s failed: %s", batch_id, mark_err)


def _fixed_str(v: object) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").rstrip("\x00")
    return str(v or "")
