"""The MCP `loom_search` answer is pinned: for a fixed fan-out payload the
text summary and the structured result are byte-identical to the recorded
answers in `fixtures/loom_search_mcp_answers.json`, and the fan-out is asked
the same question (namespace, fan-out set, clamped options).

Regenerate the fixture only when the rendering is meant to change:

    python -m tests.unit.test_loom_search_mcp_render --write
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from server.auth.fanout import Fanout
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from tests.unit.test_mcp_lean_results import PAYLOAD as _SEARCH

FIXTURE = Path(__file__).with_name("fixtures") / "loom_search_mcp_answers.json"
ORG = "acme"

_KNOWLEDGE = {
    "results": [
        {
            "source_uri": "loom://runbooks/freeze",
            "chunk_idx": 0,
            "content": "The freeze starts two days\nbefore the cut.",
            "namespace": "team",
            "score": 0.03,
            "similarity": 0.52,
            "tags": ["kind:runbook"],
            "section": "Release > Freeze",
        }
    ],
}
_PROFILE = {
    "results": [
        {"label": "home_city", "value": "Lives in\nBerlin", "namespace": "alice", "score": 0.7}
    ]
}
_CONVERSATIONS = {
    "results": [
        {
            "thread_id": "4ab6",
            "msg_id": "8a46",
            "role": "user",
            "content": "which location\nare you on?",
            "ts": datetime(2026, 7, 14, 11, 7, 12),
            "namespace": "alice",
        },
        {
            "thread_id": "9c01",
            "content_excerpt": "the other thread",
            "created_at": datetime(2026, 7, 15, 8, 0, 0),
            "namespace": "team",
        },
    ]
}
_TRACKER = {
    "verdict": "duplicate_of",
    "namespaces": ["qa-acme"],
    "results": [{"number": 101, "title": "Crash in merge", "url": "https://x/101", "score": 0.9}],
}


def _payload(kind: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory": copy.deepcopy(_SEARCH["memory"]),
        "knowledge": copy.deepcopy(_KNOWLEDGE),
        "profile": copy.deepcopy(_PROFILE),
        "conversations": copy.deepcopy(_CONVERSATIONS),
    }
    if kind == "empty":
        return {
            "memory": {"results": []},
            "knowledge": {"results": []},
            "profile": {"results": []},
            "conversations": {"results": []},
        }
    if kind == "failed_legs":
        base["knowledge"] = {"error": "knowledge leg timed out", "results": []}
        base["conversations"] = {"error": "boom", "results": []}
        return base
    if kind == "tracker":
        return {
            "tracker": copy.deepcopy(_TRACKER),
            **base,
            "withheld": {"memory": 2, "conversations": 1, "reason": "tracker_verdict_leads"},
        }
    if kind == "no_block":
        base["memory"].pop("reader_block")
        base["memory"].pop("reader_instructions")
        return base
    return base


# (name, [(session id, arguments, payload kind)])
SCENARIOS: list[tuple[str, list[tuple[str, dict[str, Any], str]]]] = [
    (
        "first_then_second_in_one_session",
        [("s1", {"query": "where do I go"}, "full"), ("s1", {"query": "where do I go"}, "full")],
    ),
    ("no_session_id", [("", {"query": "where do I go"}, "full")]),
    ("explain", [("s2", {"query": "where do I go", "explain": True}, "full")]),
    (
        "this_namespace_no_bodies_top_2",
        [("s3", {"query": "q", "namespace_scope": "this", "bodies": "none", "top_k": 2}, "full")],
    ),
    (
        "out_of_range_arguments_are_clamped",
        [
            (
                "s4",
                {
                    "query": "q",
                    "top_k": 500,
                    "recipe": "bogus",
                    "bodies": "weird",
                    "namespace_scope": "everything",
                    "tags": "not-a-list",
                    "page": "p" * 200,
                },
                "full",
            )
        ],
    ),
    (
        "tags_recipe_page",
        [("s5", {"query": "q", "tags": ["a", ""], "recipe": "fast", "page": " h "}, "full")],
    ),
    ("tracker_verdict_and_withheld", [("s6", {"query": "crash in merge"}, "tracker")]),
    ("nothing_found", [("s7", {"query": "q"}, "empty")]),
    ("failed_legs", [("s8", {"query": "q"}, "failed_legs")]),
    ("plain_hit_lines_without_a_block", [("s9", {"query": "q"}, "no_block")]),
]


def _principal() -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=("team", "alice"),
        roles=(Role.WRITER,),
        is_service_account=False,
    )


def _make_harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every I/O edge of one `loom_search` call stubbed: the caller's default
    is the teammate namespace `team`, the fan-out set is [team, alice], so
    the unaddressed search is re-seeded onto `alice`."""
    import server.core.ratelimit as ratelimit_mod
    import server.transport.mcp_call as mcp_call
    from server.memory.app.loom_search import handler as handler_mod
    from server.transport import mcp_outcome, mcp_runs, mcp_tools

    state: dict[str, Any] = {"payload": "full", "asked": []}

    async def _sections(**kw: Any) -> dict[str, Any]:
        state["asked"].append({k: v for k, v in kw.items()})
        return _payload(state["payload"])

    async def _fanout(org: str, *_: object) -> Fanout:
        return Fanout(org=org, namespaces=("team", "alice"), cap=32)

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": ORG, "namespace": "team", "agent": "claude-code"}

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    async def _level(*_a: Any, **_k: Any) -> str:
        return "off"

    class _Run:
        run_id = "run-1"

    async def _ensure(*_a: Any, **_k: Any) -> Any:
        return _Run()

    class _Limiter:
        def check(self, *_a: Any, **_k: Any) -> None:
            return None

    for mod in (handler_mod, mcp_tools):
        monkeypatch.setattr(mod, "search_sections", _sections, raising=False)
    for mod in (mcp_call, mcp_tools):
        monkeypatch.setattr(mod, "_caller_defaults", _defaults, raising=False)
    for mod in (mcp_call, mcp_outcome, mcp_tools):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _fanout)
    monkeypatch.setattr("server.auth.authz.check_authorized", _allowed)
    monkeypatch.setattr("server.code.app.member_writes.authorize_op", _allowed)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _allowed)
    monkeypatch.setattr("server.identity.app.restricted_access.note_restricted_access", _allowed)
    monkeypatch.setattr("server.transport.post_rows.post_handler_rows", _allowed)
    monkeypatch.setattr("server.observability.capture_consent.resolve", _level)
    monkeypatch.setattr("server.observability.capture_consent.resolve_most_restrictive", _level)
    monkeypatch.setattr(ratelimit_mod, "get_limiter", lambda: _Limiter())
    monkeypatch.setattr(mcp_runs, "ensure", _ensure)
    mcp_tools._INSTRUCTED.clear()

    def _call(session: str, arguments: dict[str, Any], kind: str) -> dict[str, Any]:
        state["payload"] = kind
        params = {"name": "loom_search", "arguments": copy.deepcopy(arguments)}
        out = asyncio.run(
            mcp_call._call_tool(
                1, params, auth=_principal(), client_name="claude-code", mcp_session_id=session
            )
        )
        return out["result"]

    return _call, state


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    from server.transport import mcp_tools

    yield _make_harness(monkeypatch)
    mcp_tools._INSTRUCTED.clear()


def _record(harness: Any) -> dict[str, Any]:
    call, state = harness
    out: dict[str, Any] = {}
    for name, steps in SCENARIOS:
        answers = []
        for session, arguments, kind in steps:
            state["asked"].clear()
            result = call(session, arguments, kind)
            asked = state["asked"][0] if state["asked"] else None
            answers.append({"result": result, "asked": asked})
        out[name] = json.loads(json.dumps(answers, default=str))
    return out


def test_every_scenario_answers_as_recorded(harness: Any) -> None:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    got = _record(harness)
    assert sorted(got) == sorted(recorded)
    for name in recorded:
        assert json.dumps(got[name], sort_keys=False) == json.dumps(
            recorded[name], sort_keys=False
        ), name


def _knowledge_text(bodies: str) -> str:
    """The loom_search text for one knowledge hit as `knowledge.search` serves it."""
    from server.knowledge.api.models_knowledge import _SearchHit
    from server.transport.mcp_tools import search_answer

    hit = _SearchHit(
        source_uri="docs/retention.md",
        chunk_idx=0,
        content="Audit rows are kept\nfor ninety days. " + "x" * 300,
        tags=[],
        distance=0.4,
        similarity=0.6,
    )
    payload = {
        "memory": {"results": []},
        "knowledge": {"results": [hit.model_dump()]},
        "profile": {"results": []},
        "conversations": {"results": []},
    }
    out = search_answer(payload, {"query": "q", "bodies": bodies}, "team", "")
    return out["content"][0]["text"]


def test_a_knowledge_hit_line_carries_its_chunk_text() -> None:
    line = _knowledge_text("excerpt").splitlines()[-1]
    assert line.startswith("  - [docs/retention.md] Audit rows are kept for ninety days. xxx")
    # Clipped as the memory and conversation lines are.
    assert len(line) == len("  - [docs/retention.md] ") + 200


def test_bodies_none_keeps_a_knowledge_hit_to_its_source() -> None:
    assert _knowledge_text("none").splitlines()[-1] == "  - [docs/retention.md]"


if __name__ == "__main__" and "--write" in sys.argv:
    mp = pytest.MonkeyPatch()
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(json.dumps(_record(_make_harness(mp)), indent=1) + "\n", encoding="utf-8")
    mp.undo()
    print(f"wrote {FIXTURE}")
