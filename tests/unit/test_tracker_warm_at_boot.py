"""A fresh process opens each configured tracker's client and
caches its contract report at boot, so the first request's 900 ms step budget
is not spent on them (otherwise `code.backport_gap` answers `unknown` for
the first minutes after a deploy)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.tracker.app import contract
from server.tracker.domain import sources as _sources
from server.tracker.store.source import qa_intel


def _ep(name: str, *, configured: bool = True, mapping_json: str = "") -> Any:
    return SimpleNamespace(
        name=name, configured=configured, database="db", mapping_json=mapping_json
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"client": [], "check": []}

    async def get_client(name: str) -> object:
        if name == "broken":
            raise qa_intel.QaIntelUnavailable("down")
        seen["client"].append(name)
        return object()

    async def check(name: str, database: str) -> Any:
        seen["check"].append(name)
        return SimpleNamespace(ok=name != "drifted")

    monkeypatch.setattr(qa_intel, "_get_client", get_client)
    monkeypatch.setattr(
        "server.tracker.domain.sources.credential_configured", lambda name: name != "nouser"
    )
    monkeypatch.setattr(contract, "check", check)
    monkeypatch.setattr(contract, "enforced", lambda: True)
    monkeypatch.setattr(
        _sources,
        "endpoints",
        lambda: {
            "clickhouse": _ep("clickhouse"),
            "drifted": _ep("drifted"),
            "broken": _ep("broken"),
            "nohost": _ep("nohost", configured=False),
            "nouser": _ep("nouser"),
        },
    )
    return seen


@pytest.mark.asyncio
async def test_each_configured_tracker_is_opened_and_checked(world: dict[str, list[str]]) -> None:
    out = await contract.warm_at_boot()
    assert out == {
        "clickhouse": "ok",
        "drifted": "contract_failed",
        "broken": "failed: QaIntelUnavailable",
    }
    assert world["client"] == ["clickhouse", "drifted"]
    assert world["check"] == ["clickhouse", "drifted"]


@pytest.mark.asyncio
async def test_enforcement_off_opens_the_client_only(
    world: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract, "enforced", lambda: False)
    monkeypatch.setattr(
        _sources,
        "endpoints",
        lambda: {
            "views": _ep("views"),
            "mapped": _ep("mapped", mapping_json="{}x"),
        },
    )
    out = await contract.warm_at_boot()
    assert out == {"views": "client", "mapped": "ok"}  # a mapped tracker needs its report anyway
    assert world["check"] == ["mapped"]


@pytest.mark.asyncio
async def test_a_hung_source_is_bounded(
    world: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def hang(name: str) -> object:
        await asyncio.sleep(60)
        return object()

    monkeypatch.setattr(qa_intel, "_get_client", hang)
    monkeypatch.setattr(_sources, "endpoints", lambda: {"slow": _ep("slow")})
    assert await contract.warm_at_boot(timeout_s=0.05) == {"slow": "failed: TimeoutError"}
