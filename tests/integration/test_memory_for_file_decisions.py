"""memory.for_file against a live server: a decision row written through memory.set is
rendered in the pre-edit block with its head, its pin state and its source."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from tests.integration._resources import unique_org

ORG = unique_org("for-file-decisions")
SOURCE = "knowledge:design-docs/spilling.pdf#Slide 2"


def _post(base: str, op: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{base}/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=10.0
    )
    assert r.status_code == 200, f"{op} {r.status_code}: {r.text}"
    return r.json()


def _poll(
    base: str, token: str, request: dict[str, Any], done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """memory.for_file until `done(out)` holds (the write is visible), or the last answer."""
    out: dict[str, Any] = {}
    for _ in range(50):
        out = _post(base, "memory.for_file", token, request)
        if done(out):
            break
        time.sleep(0.2)
    return out


def test_decision_rows_name_their_source_and_whether_they_are_pinned(
    control_plane_url: str, mint_test_token: Any
) -> None:
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"])
    own, team = "dev", "team-rules"
    base = {"org": ORG, "agent": "claude-code", "memory_type": "decision", "files": ["src/a.cpp"]}
    # pinned with memory.pin
    _post(
        control_plane_url,
        "memory.pin",
        token,
        base
        | {
            "namespace": team,
            "memory_key": "design/spill",
            "value": "Decided to spill the final distinct because memory is bounded.",
            "tags": ["decision"],
            "source_resource_id": SOURCE,
        },
    )
    _post(
        control_plane_url,
        "memory.set",
        token,
        base
        | {"namespace": own, "memory_key": "design/draft", "value": "Decided to keep one pass."},
    )
    request = {"org": ORG, "namespace": own, "path": "src/a.cpp", "extra_namespaces": [team]}
    out = _poll(control_plane_url, token, request, lambda out: out["total"] == 2)
    assert out["total"] == 2, out
    lines = out["rendered"].splitlines()
    # mint_test_token's default subject is the author
    assert lines[1].startswith("- Decision (by test-suite, ")
    assert ") (pinned): " in lines[1]
    assert "bounded" in lines[1]
    assert lines[1].endswith(f" — source: {SOURCE} [id: design/spill]")
    assert lines[2].startswith("- design/draft [decision]: ") and "source:" not in lines[2]
    assert [h["source_resource_id"] for h in out["hits"]] == [SOURCE, ""]

    # an attestation through memory.verify is read into the hit and adds no mark
    _post(
        control_plane_url,
        "memory.verify",
        token,
        {"org": ORG, "namespace": own, "agent": "claude-code", "memory_key": "design/draft"},
    )
    out = _poll(
        control_plane_url, token, request, lambda out: out["hits"][1]["verification"] == "process"
    )
    assert out["hits"][1]["verification"] == "process", out
    assert out["rendered"].splitlines()[2].startswith("- design/draft [decision]: ")


def test_a_recorded_decision_reads_as_its_statement_before_an_edit(
    control_plane_url: str, mint_test_token: Any
) -> None:
    # decision.record writes the row's files, source and statement; for_file prints them.
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"])
    recorded = _post(
        control_plane_url,
        "decision.record",
        token,
        {
            "org": ORG,
            "namespace": "recorded",
            "agent": "claude-code",
            "category": "design",
            "scenario": "Spill the final distinct?",
            "outcome": "spill it",
            "reasoning": "memory is bounded",
            "files": ["src/b.cpp"],
            "source_resource_id": SOURCE,
        },
    )
    key = recorded["memory_key"]
    request = {"org": ORG, "namespace": "recorded", "path": "src/b.cpp"}
    out = _poll(control_plane_url, token, request, lambda out: out["total"] == 1)
    assert out["total"] == 1, out
    assert out["rendered"].splitlines()[1] == (
        f"- {key} [decision]: Spill the final distinct? "
        f"Decided spill it because memory is bounded. — source: {SOURCE}"
    )


def test_a_long_recorded_decision_prints_as_text(
    control_plane_url: str, mint_test_token: Any
) -> None:
    # A statement past 600 stored bytes is cut by memory.for_file's select and still printed
    # decoded: no quotes, no escapes.
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"])
    _post(
        control_plane_url,
        "decision.record",
        token,
        {
            "org": ORG,
            "namespace": "recorded-long",
            "agent": "claude-code",
            "category": "design",
            "scenario": "Réécrire la jointure ?",
            "outcome": "réécrire la jointure " * 10,
            "reasoning": "la mémoire est bornée " * 15,
            "alternatives": ["garder la jointure actuelle " * 4, "attendre la version " * 4],
            "files": ["src/c.cpp"],
        },
    )
    request = {"org": ORG, "namespace": "recorded-long", "path": "src/c.cpp"}
    out = _poll(control_plane_url, token, request, lambda out: out["total"] == 1)
    assert out["total"] == 1, out
    assert out["hits"][0]["value_truncated"] is True
    line = out["rendered"].splitlines()[1]
    assert line.endswith("…") and ": Réécrire la jointure ? Decided" in line
    assert "\\u" not in line and '"' not in line
