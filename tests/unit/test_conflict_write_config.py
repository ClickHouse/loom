"""The conflict check's write-time config resolves with one precedence
whether it is prefetched (`prefetch_write_config`) or read on its own
(`_resolve_threshold`, `_resolve_duplicate_threshold`,
`namespace_conflict_policy`): per-type dedup threshold > namespace threshold >
embedder calibration; namespace duplicate threshold > calibration; namespace
default policy > `auto_reinforce`."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import conflicts as cf
from server.memory.app.namespace_types import TypeConfig

_CALIBRATED_CONFLICT = 0.81
_CALIBRATED_DUP = 0.955


class _NamespaceCH:
    """Answers the namespace-row and allowlist reads; records every statement."""

    def __init__(self, ns_row: dict[str, Any] | None) -> None:
        self.ns_row = ns_row
        self.sql: list[str] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> Any:
        self.sql.append(sql)
        assert "loom.namespaces FINAL" in sql, sql
        return self.ns_row

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        assert "loom.conflict_allowlist" in sql, sql
        return [{"memory_key_a": "a", "memory_key_b": params["k"]}]


def _env(monkeypatch: pytest.MonkeyPatch, *, type_dedup: float | None, ns_row: Any) -> Any:
    ch = _NamespaceCH(ns_row)
    monkeypatch.setattr(cf, "get_client", lambda: ch)

    async def _type_config(org: str, namespace: str, memory_type: str) -> TypeConfig:
        dedup = type_dedup if memory_type else None
        return TypeConfig(retention_days=0, dedup_threshold=dedup, hot_quota=0, conflict_policy="")

    async def _conflict(default: float, *a: Any) -> float:
        return _CALIBRATED_CONFLICT

    async def _dup(default: float, *a: Any) -> float:
        return _CALIBRATED_DUP

    monkeypatch.setattr("server.memory.app.namespace_types.get_type_config", _type_config)
    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_conflict_threshold_for_ns", _conflict
    )
    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_duplicate_threshold_for_ns", _dup
    )
    return ch


_FULL_ROW = {
    "conflict_threshold": 0.88,
    "duplicate_threshold": 0.97,
    "default_conflict_policy": "newest_wins",
}
_EMPTY_ROW = {
    "conflict_threshold": None,
    "duplicate_threshold": None,
    "default_conflict_policy": "",
}


@pytest.mark.parametrize(
    "type_dedup,ns_row,threshold,dup,policy",
    [
        (0.93, _FULL_ROW, 0.93, 0.97, "newest_wins"),  # per-type beats namespace
        (None, _FULL_ROW, 0.88, 0.97, "newest_wins"),  # namespace beats calibration
        (None, _EMPTY_ROW, _CALIBRATED_CONFLICT, _CALIBRATED_DUP, "auto_reinforce"),
        (None, None, _CALIBRATED_CONFLICT, _CALIBRATED_DUP, "auto_reinforce"),  # no row
        (0.9, None, 0.9, _CALIBRATED_DUP, "auto_reinforce"),
    ],
)
async def test_prefetch_and_per_read_resolve_the_same_precedence(
    monkeypatch: pytest.MonkeyPatch,
    type_dedup: float | None,
    ns_row: Any,
    threshold: float,
    dup: float,
    policy: str,
) -> None:
    ch = _env(monkeypatch, type_dedup=type_dedup, ns_row=ns_row)
    cfg = await cf.prefetch_write_config("o", "n", "semantic", "k1")
    assert (cfg.threshold, cfg.duplicate_threshold, cfg.policy) == (threshold, dup, policy)
    assert cfg.allowlisted == {"a"}

    assert await cf._resolve_threshold("o", "n", memory_type="semantic") == threshold
    assert await cf._resolve_duplicate_threshold("o", "n") == dup
    assert await cf.namespace_conflict_policy("o", "n", ch=ch) == policy
    assert await cf._allowlisted_keys("o", "n", "k1") == {"a"}


async def test_resolve_conflict_floor_without_a_type_skips_the_type_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, type_dedup=0.99, ns_row=_FULL_ROW)
    assert await cf._resolve_threshold("o", "n") == 0.88
    threshold, max_distance = await cf.resolve_conflict_floor("o", "n", "semantic")
    assert threshold == 0.99 and max_distance > 0
