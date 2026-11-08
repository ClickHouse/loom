"""The offline gate the quickstart smoke runs inside the server container (the smoke
checks the stack has no route out first): it fails on an embedder other than the local
one and on a paraphrase that misses."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from scripts.gates import docker_offline_search as gate
from scripts.gates import docker_quickstart_smoke as smoke


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A stack on the local embedder whose search finds the fact."""
    state: dict[str, Any] = {"model": "local", "first": "offline/refunds"}

    def request(path: str, body: dict | None = None) -> dict:
        if path == "/v1/admin/models":
            return {"embedding_model": state["model"]}
        return {"results": [{"memory_key": state["first"]}]} if path.endswith("search") else {}

    monkeypatch.setattr(gate, "request", request)
    return state


def test_an_offline_stack_that_finds_the_paraphrase_passes(stack: dict[str, Any]) -> None:
    gate.main()


@pytest.mark.parametrize(
    ("field", "value", "said"),
    [("model", "fake-ngram-3072", "fake-ngram"), ("first", "offline/deploys", "offline/deploys")],
)
def test_the_gate_fails_on_another_embedder_or_a_miss(
    stack: dict[str, Any], field: str, value: Any, said: str
) -> None:
    stack[field] = value
    with pytest.raises(AssertionError, match=said):
        gate.main()


def test_a_refused_connection_is_not_no_route_out() -> None:
    """A closed local port answers, so the probe names an errno the offline check fails on."""
    reach = subprocess.run(
        [sys.executable, "-c", smoke.EGRESS_PROBE, "plain", "127.0.0.1", "9"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert reach.stdout.strip() == "ECONNREFUSED" and "ECONNREFUSED" not in smoke.NO_ROUTE
