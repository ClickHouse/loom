"""The ClickGap migration validator reads without touching and never scores a
failed search as a miss."""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import pytest

from bench import clickgap_migration_validate as V


def _args(**kw: object) -> argparse.Namespace:
    base = {"top_k": 10, "min_recall": 0.0, "min_recallw": 0.0, "min_tau": 0.0}
    base.update(kw)
    return argparse.Namespace(**base)


def _probe(hit: bool, tau: float | None = None, error: str = "") -> V.ProbeResult:
    return V.ProbeResult(
        kind="lesson",
        entity_kinds=set(),
        recall_count=3,
        recall_hit=hit,
        loom_empty=not hit,
        tau=tau,
        error=error,
    )


def test_a_failed_probe_refuses_the_gates_and_is_not_a_miss(capsys) -> None:
    rc = V.report([_probe(True), _probe(False, error="503")], _args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "recall@10 (known-item):        1.000" in out
    assert "searches failed, excluded: 1" in out


def test_no_computable_tau_fails_a_tau_gate_and_reads_na(capsys) -> None:
    assert V.report([_probe(True)], _args()) == 0
    assert "n/a" in capsys.readouterr().out
    assert V.report([_probe(True)], _args(min_tau=0.1)) == 1


def test_search_body_does_not_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"memory_key": "lesson_1"}]})

    monkeypatch.setattr(V, "LOOM_BASE_URL", "http://bench.invalid")

    async def go() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await V.loom_search(c, "t", "o", "ns", "a", "q", 10)

    assert asyncio.run(go()) == ["lesson_1"]
    assert seen[0]["auto_touch"] is False
