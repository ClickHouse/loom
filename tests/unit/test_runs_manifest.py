"""Context.pack's `runs` section — one line per recent session
of the identity, joined from the episodic plane, the run ledger and the
digest table; opt-in, identity-scoped, fail-open."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.app import runs_manifest as rm

OBS = [
    {
        "session_id": "s-new",
        "first_at": datetime(2026, 9, 12, 10, 0),
        "last_at": datetime(2026, 9, 12, 10, 40),
        "n": 14,
        "edits": 3,
        "commands": 5,
        "errors": 1,
        "files": ["server/main.py", "tests/test_x.py"],
    },
    {
        "session_id": "s-old",
        "first_at": datetime(2026, 9, 10, 9, 0),
        "last_at": datetime(2026, 9, 10, 9, 20),
        "n": 4,
        "edits": 0,
        "commands": 2,
        "errors": 0,
        "files": [],
    },
    {"session_id": "-", "n": 3},  # no session: dropped
]
RUNS = [
    {
        "run_id": "cc-s-new",
        "session_id": "s-new",
        "goal": "fix the failing parser test  and rerun",
        "status": "completed",
        "started_at": datetime(2026, 9, 12, 10, 0),
        "duration_ms": 2400000,
    },
    {
        "run_id": "cc-lonely",
        "session_id": "-",
        "goal": "a run with no observations",
        "status": "failed",
        "started_at": datetime(2026, 9, 11, 8, 0),
        "duration_ms": 5000,
    },
]
DIGESTS = [
    {"session_id": "s-new", "summary_text": "On 2026-09-12 the user fixed the parser test by ..."}
]


def test_merge_joins_the_three_planes_newest_first_and_bounds() -> None:
    rows = rm.merge_rows(OBS, RUNS, DIGESTS, limit=10)
    assert [r.session_id for r in rows] == ["s-new", "cc-lonely", "s-old"]
    new = rows[0]
    assert (new.calls, new.edits, new.commands, new.errors) == (14, 3, 5, 1)
    assert new.status == "completed" and new.goal.startswith(
        "fix the failing parser test and rerun"
    )
    assert new.digest.startswith("On 2026-09-12") and new.files == [
        "server/main.py",
        "tests/test_x.py",
    ]
    lonely = rows[1]
    assert lonely.run_id == "cc-lonely" and lonely.status == "failed" and lonely.calls == 0
    assert rm.merge_rows(OBS, RUNS, DIGESTS, limit=1)[0].session_id == "s-new"
    assert rm.merge_rows([], [], [], limit=5) == []


def test_render_is_one_terse_line_per_session() -> None:
    text = rm.render_manifest(rm.merge_rows(OBS, RUNS, DIGESTS, limit=10))
    lines = text.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith(
        "- 2026-09-12 10:00 session=s-new · run=completed 2400s · goal: fix the failing"
    )
    assert "14 calls (3 edits, 5 cmds, 1 errors)" in lines[0]
    assert (
        "files: server/main.py, tests/test_x.py" in lines[0] and "digest: On 2026-09-12" in lines[0]
    )
    assert "run=failed 5s · goal: a run with no observations" in lines[1]
    assert lines[2].endswith("4 calls (2 cmds)")


class _CH:
    def __init__(self, answers: list[list[dict[str, Any]]], fail: bool = False) -> None:
        self.answers = list(answers)
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def query_all(self, sql, params=None):
        if self.fail:
            raise RuntimeError("boom")
        self.queries.append((sql, dict(params or {})))
        return self.answers.pop(0) if self.answers else []


@pytest.mark.asyncio
async def test_fetch_scopes_to_the_identity_and_reads_digests_for_seen_sessions() -> None:
    ch = _CH([OBS, RUNS, DIGESTS])
    rows = await rm.fetch_manifest_rows(
        ch, org="o", namespace="n", agent="claude-code", user_id="u", limit=12
    )
    assert [r.session_id for r in rows] == ["s-new", "cc-lonely", "s-old"]
    obs_sql, obs_params = ch.queries[0]
    assert (
        "memory_type = 'episodic'" in obs_sql
        and obs_params["agent"] == "claude-code"
        and obs_params["uid"] == "u"
    )
    runs_sql, _ = ch.queries[1]
    assert "loom.agent_runs FINAL" in runs_sql
    dig_sql, dig_params = ch.queries[2]
    assert "loom.session_summaries FINAL" in dig_sql and set(dig_params["sids"]) == {
        "s-new",
        "s-old",
    }


@pytest.mark.asyncio
async def test_wildcard_agent_and_dash_user_drop_their_predicates() -> None:
    ch = _CH([[], [], []])
    await rm.fetch_manifest_rows(ch, org="o", namespace="n", agent="*", user_id="-", limit=5)
    sql, params = ch.queries[0]
    assert "agent =" not in sql and "user_id =" not in sql and "agent" not in params
    assert len(ch.queries) == 2  # no sessions seen → no digest read


@pytest.mark.asyncio
async def test_runs_manifest_fails_open() -> None:
    body, n = await rm.runs_manifest(
        org="o", namespace="n", agent="*", user_id="-", ch=_CH([], fail=True)
    )
    assert body == "" and n == 0


@pytest.mark.asyncio
async def test_context_pack_serves_the_runs_section_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace as NS

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _empty(*a: Any, **k: Any) -> Any:
        return NS(
            rendered="",
            blocks=[],
            block_count=0,
            entries=[],
            tokens_used=0,
            tokens_budget=0,
            truncated=False,
            labels=[],
            section_count=0,
            results=[],
            hits=[],
            matches=[],
        )

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.search.handle_memory_search",
        "server.memory.app.procedure.handle_procedure_match",
    ):
        monkeypatch.setattr(target, _empty, raising=False)

    calls: list[dict[str, Any]] = []

    async def fake_manifest(**kw: Any) -> tuple[str, int]:
        calls.append(kw)
        return "- 2026-09-12 10:00 session=s-new · run=completed", 1

    monkeypatch.setattr("server.memory.app.runs_manifest.runs_manifest", fake_manifest)
    base = {"org": "o", "namespace": "n", "agent": "claude-code", "user_id": "u", "session_id": "s"}
    out = await handle_context_pack(ContextPackInput(**base, include_runs=True, runs_limit=7))
    assert "runs" in out.sections and out.sections["runs"].startswith(
        "- 2026-09-12 10:00 session=s-new"
    )
    assert calls and calls[0]["limit"] == 7 and calls[0]["agent"] == "claude-code"
    assert any(b.section == "runs" and b.label == "episodic.runs" for b in out.blocks)
    assert out.section_meta["runs"]["sessions"] == 1
    # opt-in: without the flag the section is not built and the manifest is not read
    calls.clear()
    out2 = await handle_context_pack(ContextPackInput(**base))
    assert "runs" not in out2.sections and calls == []
    # only_sections accepts the name
    ContextPackInput(**base, only_sections=["runs"])
