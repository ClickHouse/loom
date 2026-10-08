"""Audit checkpoints: a chain over closed windows.

CH-free: the pure window/digest/classification helpers, and the verifier's
checkpoint leg against a stub client that plays back a log.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.insights.domain import audit_checkpoints as svc

H = 3600
T0 = datetime(2026, 9, 11, 10, 0, 0)

# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_floor_window_and_windows_to_seal_respect_grace_and_cap() -> None:
    assert svc.floor_window(datetime(2026, 9, 11, 10, 42, 7), H) == T0
    now = T0 + timedelta(hours=3, minutes=5)
    # No checkpoint yet: start at the first row's window; the window ending
    # at 13:00 is only 5 minutes old, inside the 10-minute grace, so it waits.
    wins = svc.windows_to_seal(
        None, T0 + timedelta(minutes=3), now, window_seconds=H, grace_seconds=600, max_windows=10
    )
    assert wins == [
        (T0, T0 + timedelta(hours=1)),
        (T0 + timedelta(hours=1), T0 + timedelta(hours=2)),
    ]
    # With a checkpoint, continue from its end; the cap bounds a pass.
    wins = svc.windows_to_seal(
        T0 + timedelta(hours=1),
        None,
        now + timedelta(hours=10),
        window_seconds=H,
        grace_seconds=600,
        max_windows=3,
    )
    assert len(wins) == 3 and wins[0][0] == T0 + timedelta(hours=1)
    # Empty log, no checkpoint: nothing to seal.
    assert (
        svc.windows_to_seal(None, None, now, window_seconds=H, grace_seconds=0, max_windows=5) == []
    )


def test_rows_digest_is_order_independent_and_matches_the_sql_shape() -> None:
    a, b = hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest()
    assert svc.rows_digest([a, b]) == svc.rows_digest([b, a])
    assert svc.rows_digest([a + "\x00\x00", b]) == svc.rows_digest([a, b])  # FixedString padding
    assert svc.rows_digest([a, b]) == hashlib.sha256("".join(sorted([a, b])).encode()).hexdigest()
    assert svc.rows_digest([]) == svc.EMPTY_DIGEST
    sql = svc.rows_digest_sql()
    for piece in (
        "SHA256",
        "arrayStringConcat",
        "arraySort",
        "groupArray(toString(row_hash))",
        "lower(hex(",
    ):
        assert piece in sql, piece


def test_seal_chains_and_chain_breaks_detects_tampering_with_the_chain() -> None:
    c1 = svc.seal(T0, T0 + timedelta(hours=1), 3, "d1", "")
    c2 = svc.seal(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 0, "", c1.checkpoint_hash)
    assert c2.rows_digest == svc.EMPTY_DIGEST and c2.prev_checkpoint_hash == c1.checkpoint_hash
    assert svc.chain_breaks([c1, c2]) == []
    # Rewriting a sealed count without re-hashing shows as a hash mismatch;
    # re-hashing it consistently breaks the link from its successor.
    forged = svc.Checkpoint(c1.window_start, c1.window_end, 2, "d1", "", c1.checkpoint_hash)
    assert [b["reason"] for b in svc.chain_breaks([forged, c2])] == ["checkpoint_hash mismatch"]
    rehashed = svc.seal(T0, T0 + timedelta(hours=1), 2, "d1", "")
    assert [b["reason"] for b in svc.chain_breaks([rehashed, c2])] == ["chain link mismatch"]


def test_classify_names_deletion_modification_and_late_insert() -> None:
    cp = svc.seal(T0, T0 + timedelta(hours=1), 3, "d1", "")
    assert svc.classify(cp, 3, "d1") is None
    assert svc.classify(cp, 2, "x") == "deletion"
    assert svc.classify(cp, 3, "d2") == "modification"
    assert svc.classify(cp, 4, "d1") == "late_insert"


# ----------------------------------------------------------------------
# Verifier leg against a played-back log
# ----------------------------------------------------------------------


class _CH:
    """A log of (ts, row_hash) rows and a checkpoint table, answered in SQL shape."""

    def __init__(self, rows: list[tuple[datetime, str]]) -> None:
        self.rows = list(rows)
        self.checkpoints: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        params = params or {}
        if "min(ts)" in sql:
            return {"first_ts": min((t for t, _ in self.rows), default=None), "n": len(self.rows)}
        if "FROM loom.audit_log" in sql:
            ws, we = params["ws"], params["we"]
            hashes = [h for t, h in self.rows if ws <= t < we]
            return {"n": len(hashes), "digest": svc.rows_digest(hashes) if hashes else ""}
        raise AssertionError(sql)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        assert "FROM loom.audit_checkpoints" in sql
        rows = sorted(self.checkpoints, key=lambda r: r["window_start"], reverse=True)
        return rows[: int((params or {}).get("lim") or 10)]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings=None):
        assert table == "loom.audit_checkpoints"
        self.checkpoints.extend(rows)


def _hash(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


def _cfg(**kw: Any):
    from server.insights.workers.audit_verifier import AuditVerifierConfig

    return AuditVerifierConfig(
        window_seconds=H, grace_seconds=600, verify_windows=24, max_seal_per_pass=48, **kw
    )


@pytest.mark.asyncio
async def test_first_pass_seals_closed_windows_and_second_pass_finds_them_intact() -> None:
    from server.insights.workers.audit_verifier import AuditVerifierResult, run_checkpoints

    rows = [(T0 + timedelta(minutes=m), _hash(m)) for m in (1, 30, 61, 62, 125)]
    ch = _CH(rows)
    now = T0 + timedelta(hours=3, minutes=20)
    r1 = AuditVerifierResult()
    await run_checkpoints(_cfg(), r1, ch=ch, now=now)
    assert (r1.windows_sealed, r1.windows_verified, r1.checkpoint_breaks) == (3, 0, [])
    counts = [c["row_count"] for c in ch.checkpoints]
    assert counts == [2, 2, 1]
    assert ch.checkpoints[1]["prev_checkpoint_hash"] == ch.checkpoints[0]["checkpoint_hash"]
    r2 = AuditVerifierResult()
    await run_checkpoints(_cfg(), r2, ch=ch, now=now)
    assert (r2.windows_sealed, r2.windows_verified, r2.checkpoint_breaks) == (0, 3, [])


@pytest.mark.asyncio
async def test_recount_reports_deletion_modification_and_late_insert() -> None:
    from server.insights.workers.audit_verifier import AuditVerifierResult, run_checkpoints

    rows = [(T0 + timedelta(minutes=m), _hash(m)) for m in (1, 30, 61, 62, 125, 130)]
    ch = _CH(rows)
    now = T0 + timedelta(hours=3, minutes=20)
    await run_checkpoints(_cfg(), AuditVerifierResult(), ch=ch, now=now)
    # Delete a row from window 0, alter a row's hash in window 1, add one to window 2.
    ch.rows = [r for r in ch.rows if r[0] != T0 + timedelta(minutes=30)]
    ch.rows = [(t, ("f" * 64 if t == T0 + timedelta(minutes=61) else h)) for t, h in ch.rows]
    ch.rows.append((T0 + timedelta(minutes=140), _hash(999)))
    res = AuditVerifierResult()
    await run_checkpoints(_cfg(), res, ch=ch, now=now)
    kinds = {b["window_start"]: b["kind"] for b in res.checkpoint_breaks}
    assert kinds == {
        T0.isoformat(): "deletion",
        (T0 + timedelta(hours=1)).isoformat(): "modification",
        (T0 + timedelta(hours=2)).isoformat(): "late_insert",
    }
    deletion = next(b for b in res.checkpoint_breaks if b["kind"] == "deletion")
    assert (deletion["sealed_count"], deletion["observed_count"]) == (2, 1)


@pytest.mark.asyncio
async def test_run_pass_emits_tamper_for_deletions_but_not_late_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.workers import audit_verifier as mod

    emitted: list[dict[str, Any]] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        emitted.append({"event_type": event_type, **kw})
        return "h"

    async def _verify_window(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(mod.audit, "emit", _emit)

    async def _reported(*_: Any, **__: Any) -> set[str]:
        return set()

    monkeypatch.setattr("server.insights.store.audit.verify_window", _verify_window)
    monkeypatch.setattr("server.insights.store.audit.reported_tamper_keys", _reported)
    calls: list[str] = []

    async def _fake_checkpoints(cfg, result, **_):
        calls.append("ran")
        result.checkpoint_breaks.append({"window_start": "w", "kind": "late_insert"})

    monkeypatch.setattr(mod, "run_checkpoints", _fake_checkpoints)
    out = await mod.run_pass(_cfg())
    assert calls == ["ran"] and out.checkpoint_breaks and emitted == []

    async def _fake_deletion(cfg, result, **_):
        result.checkpoint_breaks.append({"window_start": "w", "kind": "deletion"})

    monkeypatch.setattr(mod, "run_checkpoints", _fake_deletion)
    await mod.run_pass(_cfg())
    assert emitted and emitted[0]["event_type"] == "audit.tamper_detected"
    assert emitted[0]["subject"]["checkpoint_break_count"] == 1


def test_settings_default_on_with_hourly_windows() -> None:
    from server.settings import settings

    assert settings.audit_checkpoint_window_seconds == 3600
    assert settings.audit_checkpoint_grace_seconds == 600
