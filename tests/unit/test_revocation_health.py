"""/v1/health says when this process's revocation snapshot is missing or
stale — the two states the fail-closed list otherwise shows only in logs."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from server.auth import revocation
from server.observability.problems import HealthState, collect_problems
from server.settings import settings


def _codes(age: float | None, *, auth_on: bool = True) -> set[str]:
    cfg = SimpleNamespace(**{**settings.__dict__})
    cfg.auth_actually_required = lambda: auth_on
    cfg.jwt_revocation_cache_ttl_seconds = 30.0
    for name in (
        "jwt_secret_is_dev_default",
        "jwt_sealing_secret_is_dev_default",
        "llm_provider_inferred",
        "llm_provider_source",
    ):
        setattr(cfg, name, getattr(settings, name))
    return {p.code for p in collect_problems(cfg, HealthState(revocation_age_s=age))}


@pytest.mark.parametrize(
    ("age", "want"),
    [
        (None, set()),
        (5.0, set()),
        (299.0, set()),
        (301.0, {"revocation_stale"}),
        (math.inf, {"revocation_not_loaded"}),
    ],
)
def test_revocation_problems(age: float | None, want: set[str]) -> None:
    assert _codes(age) & {"revocation_stale", "revocation_not_loaded"} == want


def test_auth_off_reports_nothing() -> None:
    assert not _codes(math.inf, auth_on=False) & {"revocation_stale", "revocation_not_loaded"}


def test_snapshot_age_tracks_the_last_good_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(revocation, "_loaded", False)
    assert revocation.snapshot_age_seconds() == math.inf
    monkeypatch.setattr(revocation, "_loaded", True)
    monkeypatch.setattr(revocation, "_loaded_at", revocation.time.monotonic() - 42)
    assert 41 <= revocation.snapshot_age_seconds() < 60
