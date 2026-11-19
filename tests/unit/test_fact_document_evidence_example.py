"""Offline HTTP doubles check separate fact and section reads, not live evidence."""

from __future__ import annotations

import functools
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "fact_document_evidence", ROOT / "sdk/python/examples/fact_document_evidence.py"
)
assert SPEC is not None and SPEC.loader is not None
example = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(example)

FACT = {
    "found": True,
    "agent": "fixture-writer",
    "user_id": "fixture-user",
    "session_id": "fixture-session",
    "value": "Synthetic claim for offline contract checks",
    "version": 4,
    "status": "active",
    "actor": "process:fixture-writer",
    "source_class": "asserted",
    "source_trace": "fixture-origin",
    "source_resource_id": "a-different-document",
    "unexpected_private_field": "do not forward",
}
SECTION = {
    "found": True,
    "source_uri": "fixture:runbook",
    "section_content": "Synthetic passage for offline contract checks",
    "section_chunk_idxs": [2, 3],
    "sections": [{"title": "do not forward other sections"}],
}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> tuple[list[httpx.Request], dict[str, Any]]:
    requests: list[httpx.Request] = []
    replies = {"memory.get": FACT.copy(), "knowledge.outline": SECTION.copy()}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=replies[request.url.path.removeprefix("/v1/")])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setenv("LOOM_BASE_URL", "http://loom.test")
    monkeypatch.setenv("LOOM_API_KEY", "fixture-credential")
    monkeypatch.setenv("LOOM_AUTO_RUN", "on")
    monkeypatch.setenv("LOOM_MENTOR_BRIEF", "on")
    return requests, replies


def args() -> Any:
    return example.argparse.Namespace(
        org="fixture-org",
        namespace="fixture-ns",
        agent="fixture-writer",
        user_id="fixture-user",
        session_id="fixture-session",
        memory_key="fixture-claim",
        source_uri="fixture:runbook",
        section=["Deployments", "Rollback"],
    )


async def test_exact_read_contracts_and_unverified_pair(server: Any) -> None:
    requests, _ = server
    result = await example.run(args())
    assert [r.url.path for r in requests] == ["/v1/memory.get", "/v1/knowledge.outline"]
    assert all(r.method == "POST" for r in requests)
    fact_body, section_body = [json.loads(r.content) for r in requests]
    assert fact_body == {
        "org": "fixture-org",
        "namespace": "fixture-ns",
        "agent": "fixture-writer",
        "user_id": "fixture-user",
        "session_id": "fixture-session",
        "run_id": "-",
        "memory_key": "fixture-claim",
        "fields": list(example.FACT_FIELDS),
    }
    assert section_body == {
        "org": "fixture-org",
        "namespace": "fixture-ns",
        "source_uri": "fixture:runbook",
        "section_path": ["Deployments", "Rollback"],
        "include_children": False,
    }
    assert result["fact"] == {k: v for k, v in FACT.items() if k != "unexpected_private_field"}
    assert result["document_section"] == {k: v for k, v in SECTION.items() if k != "sections"}
    assert result["selection"]["relationship"] == "caller_selected"
    assert result["support"] == "not_evaluated"
    public_summary = json.dumps(example.summary(result))
    for private in (*FACT.values(), SECTION["section_content"], SECTION["source_uri"]):
        if isinstance(private, str):
            assert private not in public_summary


@pytest.mark.parametrize("missing", ["fact", "document", "section", "optional_fields"])
async def test_missing_data_does_not_acquire_support(server: Any, missing: str) -> None:
    _, replies = server
    if missing == "fact":
        replies["memory.get"] = {"found": False}
    elif missing == "document":
        replies["knowledge.outline"] = {"found": False, "source_uri": "fixture:runbook"}
    elif missing == "section":
        replies["knowledge.outline"]["section_content"] = None
        replies["knowledge.outline"]["section_chunk_idxs"] = []
    else:
        replies["memory.get"] = {
            "found": True,
            "value": "claim",
            **{field: FACT[field] for field in ("agent", "user_id", "session_id")},
        }
    result = await example.run(args())
    assert result["support"] == "not_evaluated"
    report = example.summary(result)
    assert report["fact_found"] is (missing != "fact")
    assert report["section_found"] is (missing not in ("document", "section"))
    if missing == "optional_fields":
        assert "source_resource_id" not in result["fact"]


@pytest.mark.parametrize("field", ["agent", "user_id", "session_id"])
@pytest.mark.parametrize("missing", [False, True])
async def test_unverifiable_identity_stops_before_document_read(
    server: Any, field: str, missing: bool
) -> None:
    requests, replies = server
    if missing:
        del replies["memory.get"][field]
    else:
        replies["memory.get"][field] = "different-owner"
    with pytest.raises(ValueError, match="selected identity"):
        await example.run(args())
    assert [request.url.path for request in requests] == ["/v1/memory.get"]


@pytest.mark.parametrize("agent", ["", "-", "*"])
async def test_anonymous_agent_selection_fails_before_requests(server: Any, agent: str) -> None:
    requests, _ = server
    selection = args()
    selection.agent = agent
    with pytest.raises(ValueError, match="explicit owning agent"):
        await example.run(selection)
    assert requests == []


async def test_empty_section_fails_before_requests(server: Any) -> None:
    requests, _ = server
    selection = args()
    selection.section = []
    with pytest.raises(ValueError, match="nonempty"):
        await example.run(selection)
    assert requests == []


def test_cli_does_not_reflect_error_body(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    async def failed(_: Any) -> None:
        raise example.LoomError("private response body and credential")

    monkeypatch.setattr(example, "run", failed)
    monkeypatch.setattr(example.argparse.ArgumentParser, "parse_args", lambda _: args())
    assert example.main() == 1
    output = capsys.readouterr()
    assert "Read failed" in output.out
    assert "private response body" not in output.out + output.err
