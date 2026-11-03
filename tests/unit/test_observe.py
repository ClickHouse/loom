"""Unit tests for the ambient-capture (observe) plane."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_observe import ObserveCrystallizeInput, ObserveRecordInput
from server.memory.app import observe as observe_handlers
from server.memory.app.observe import handle_observe_crystallize, handle_observe_record
from server.memory.domain.observation_tags import (
    CRYSTALLIZE_DONE_TAG,
    CRYSTALLIZE_READY_TAG,
    OBSERVATION_TAG,
)


class StubCH:
    """Minimal CH stub. Records inserts; stages query_all returns."""

    def __init__(self) -> None:
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None


def _explode_embedder(*_a, **_k):  # pragma: no cover
    raise AssertionError("observe.record must NOT call the embedder on the hot path")


@pytest.fixture
def install_ch(monkeypatch: pytest.MonkeyPatch):
    def _install(ch: StubCH) -> StubCH:
        monkeypatch.setattr(observe_handlers, "get_client", lambda: ch)

        async def _no_pattern(**_: Any) -> None:
            return None

        # No blacklist pattern matches unless a test installs one.
        monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _no_pattern)
        return ch

    return _install


@pytest.mark.asyncio
async def test_record_single_insert_no_llm_no_embedding(install_ch, monkeypatch):
    ch = install_ch(StubCH())
    monkeypatch.setattr("server.embedding.get_embedder", _explode_embedder)

    out = await handle_observe_record(
        ObserveRecordInput(
            org="clickhouse",
            namespace="ns",
            agent="claude-code",
            session_id="sess-1",
            tool_name="Edit",
            tool_input={"file_path": "server/main.py", "old": "a", "new": "b"},
            tool_output="ok",
        )
    )

    assert len(ch.insert_calls) == 1
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.memory"
    assert len(rows) == 1
    row = rows[0]
    assert row["memory_type"] == "episodic"
    assert row["source_trace"] == "observe.record"
    assert settings.get("async_insert") == 0
    assert out.kind == "file_edit"
    assert OBSERVATION_TAG in row["tags"]
    assert "file_edit" in row["tags"]


@pytest.mark.asyncio
async def test_record_extracts_file_paths_into_tags_and_entities(install_ch):
    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            tool_name="str_replace_editor",
            tool_input={"path": "src/loom/memory.py"},
            file_paths=["docs/architecture.md"],
        )
    )
    row = ch.insert_calls[0][1][0]
    assert "src/loom/memory.py" in out.file_paths
    assert "docs/architecture.md" in out.file_paths
    assert "src/loom/memory.py" in row["entities"]
    assert "memory.py" in row["tags"]
    assert "architecture.md" in row["tags"]


@pytest.mark.asyncio
async def test_record_error_reclassifies_kind(install_ch):
    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            tool_name="Bash",
            tool_input={"command": "pytest"},
            error="AssertionError: 1 != 2",
        )
    )
    row = ch.insert_calls[0][1][0]
    assert out.kind == "error"
    assert "error" in row["tags"]
    assert "command_run" in row["tags"]
    value = json.loads(row["value"])
    assert value["kind"] == "error"
    assert "AssertionError" in value["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,expected_kind",
    [
        ("WebFetch", "web_fetch"),
        ("Write", "file_edit"),
        ("Read", "file_read"),
        ("bash", "command_run"),
        ("some_custom_mcp_tool", "tool_use"),
    ],
)
async def test_record_heuristic_kinds(install_ch, tool_name, expected_kind):
    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(org="o", namespace="n", agent="a", session_id="s", tool_name=tool_name)
    )
    assert out.kind == expected_kind
    assert ch.insert_calls[0][1][0]["memory_type"] == "episodic"


@pytest.mark.asyncio
async def test_record_idempotent_key_on_same_content(install_ch):
    install_ch(StubCH())
    payload = dict(
        org="o",
        namespace="n",
        agent="a",
        session_id="sess-X",
        tool_name="Edit",
        tool_input={"file_path": "x.py"},
    )
    out1 = await handle_observe_record(ObserveRecordInput(**payload))
    out2 = await handle_observe_record(ObserveRecordInput(**payload))
    assert out1.memory_key == out2.memory_key
    assert out1.memory_key.startswith("obs:sess-X:")
    assert out1.content_sha256 == out2.content_sha256


@pytest.mark.asyncio
async def test_crystallize_marks_pending_observations_ready(install_ch, monkeypatch):
    ch = install_ch(StubCH())
    ch.query_all_returns.append([{"memory_key": "obs:sess-1:abcd1234"}])

    from server.memory import store as memory_repo

    fake_row = memory_repo.MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="sess-1",
        memory_key="obs:sess-1:abcd1234",
        memory_type="episodic",
        value={"kind": "file_edit", "narrative": "[file_edit] tool=Edit"},
        confidence=1.0,
        version=123,
        pinned=False,
        created_at=__import__("datetime").datetime(2026, 6, 12),
        expires_at=None,
        tags=[OBSERVATION_TAG, "file_edit"],
        entities=["x.py"],
    )

    async def _fake_get_by_key(identity, key, **kw):
        assert key == "obs:sess-1:abcd1234"
        return fake_row

    monkeypatch.setattr(memory_repo, "get_by_key", _fake_get_by_key)

    out = await handle_observe_crystallize(
        ObserveCrystallizeInput(org="o", namespace="n", agent="a", session_id="sess-1")
    )
    assert out.enqueued == 1
    assert len(ch.insert_calls) == 1
    written = ch.insert_calls[0][1][0]
    assert CRYSTALLIZE_READY_TAG in written["tags"]
    assert OBSERVATION_TAG in written["tags"]
    assert "file_edit" in written["tags"]
    scan_sql = ch.query_all_calls[0][0]
    assert "memory_type = 'episodic'" in scan_sql
    assert "has(tags, {obs_tag:String})" in scan_sql
    assert "NOT has(tags, {ready_tag:String})" in scan_sql


@pytest.mark.asyncio
async def test_crystallize_no_pending_is_noop(install_ch):
    ch = install_ch(StubCH())
    ch.query_all_returns.append([])
    out = await handle_observe_crystallize(
        ObserveCrystallizeInput(org="o", namespace="n", agent="a", session_id="empty")
    )
    assert out.enqueued == 0
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_worker_promotes_ready_observation_via_existing_extraction(monkeypatch):
    import datetime as _dt

    from server.memory import store as memory_repo
    from server.memory.workers import observe_extract

    ch = StubCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    # find_ready_sessions scan -> one session
    ch.query_all_returns.append(
        [
            {
                "org": "o",
                "namespace": "n",
                "agent": "a",
                "user_id": "-",
                "session_id": "sess-1",
                "n": 1,
                "last_at": _dt.datetime(2026, 6, 12),
            }
        ]
    )

    ready_row = memory_repo.MemoryRow(
        memory_id="22222222-2222-2222-2222-222222222222",
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="sess-1",
        memory_key="obs:sess-1:abcd1234",
        memory_type="episodic",
        value={
            "kind": "error",
            "tool_name": "Bash",
            "narrative": "[error] tool=Bash\nerror: ImportError on server/main.py",
            "error": "ImportError on server/main.py",
        },
        confidence=1.0,
        version=123,
        pinned=False,
        created_at=_dt.datetime(2026, 6, 12),
        expires_at=None,
        valid_at=_dt.datetime(2026, 6, 12),
        tags=[OBSERVATION_TAG, "error", CRYSTALLIZE_READY_TAG],
        entities=["server/main.py"],
    )

    # _load_session_observations: one full-projection read of the session
    stored = memory_repo.build_memory_version_row(ready_row)
    ch.query_all_returns.append([{**stored, "value_text": stored["value"]}])

    # The re-read right before the DONE insert: the head is unchanged.
    async def _heads(keys, **_k):
        return {k: ready_row for k in keys}

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _heads)

    calls: dict[str, Any] = {}

    class _FakeWritten:
        def __init__(self, memory_key: str, version: int) -> None:
            self.memory_key = memory_key
            self.version = version

    class _FakeOut:
        written = [_FakeWritten("ci_import_error", 1)]

    async def _fake_extract(extract_in):
        calls["extract_in"] = extract_in
        return _FakeOut()

    monkeypatch.setattr(
        "server.memory.app.extract.handler.handle_memory_set_from_messages", _fake_extract
    )

    result = await observe_extract.run_pass()

    assert result.distilled_count == 1
    assert result.written_memories == 1
    assert result.observations_consumed == 1
    assert not result.failures

    extract_in = calls["extract_in"]
    assert extract_in.org == "o"
    assert extract_in.session_id == "sess-1"
    assert len(extract_in.messages) == 1
    assert "ImportError" in extract_in.messages[0].content
    assert "crystallized" in extract_in.extra_tags

    done_inserts = [
        rows[0]
        for table, rows, _ in ch.insert_calls
        if table == "loom.memory" and CRYSTALLIZE_DONE_TAG in rows[0]["tags"]
    ]
    assert len(done_inserts) == 1
    done = done_inserts[0]
    assert CRYSTALLIZE_READY_TAG not in done["tags"]
    assert OBSERVATION_TAG in done["tags"]
    assert "error" in done["tags"]
    # Every column is the head's; only tags and version move, and the version is a fresh
    # stamp, so the namespace's change watermark moves with it.
    assert done["version"] > ready_row.version
    assert done == memory_repo.build_memory_version_row(
        ready_row, version=done["version"], tags=[OBSERVATION_TAG, "error", CRYSTALLIZE_DONE_TAG]
    )


@pytest.mark.asyncio
async def test_worker_no_ready_sessions_is_noop(monkeypatch):
    from server.memory.workers import observe_extract

    ch = StubCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    ch.query_all_returns.append([])

    result = await observe_extract.run_pass()
    assert result.distilled_count == 0
    assert result.written_memories == 0
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_worker_scan_filters_ready_not_done(monkeypatch):
    from server.memory.workers import observe_extract

    ch = StubCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    ch.query_all_returns.append([])
    await observe_extract.find_ready_sessions(observe_extract.ObserveExtractConfig(), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "memory_type = 'episodic'" in sql
    assert "has(tags, {ready_tag:String})" in sql
    assert "NOT has(tags, {done_tag:String})" in sql
    assert "GROUP BY org, namespace, agent, user_id, session_id" in sql


# ---------------------------------------------------------------------------
# The request fields reach the row, and the row's shape reaches the ledger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_fields_fill_the_row(install_ch):
    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="claude-code",
            session_id="s",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_output="2 failed",
            error="2 failed",
            file_paths=["server/main.py"],
            action_type="command_run",
        )
    )
    row = ch.insert_calls[0][1][0]
    value = json.loads(row["value"])
    assert "pytest -q" in value["narrative"] and "2 failed" in value["narrative"]
    assert value.get("error") == "2 failed"
    assert "server/main.py" in value["file_paths"] and "server/main.py" in out.file_paths
    assert out.kind == "error" and out.has_error is True
    assert out.tool_name == "Bash" and out.narrative_chars == len(value["narrative"])
    assert "action:command_run" in row["tags"]


def test_trajectory_outcomes_ride_the_ledger():
    """out_tool_name / out_has_error / out_narrative_chars on observe.record,
    out_matched / out_procedure_key on procedure.match, out_premise_conflict
    on memory.ask."""
    from server.memory.api.models_observe import ObserveRecordOutput
    from server.memory.api.models_procedure import ProcedureMatchOutput
    from server.observability.event_args import project_result

    obs = project_result(
        ObserveRecordOutput(
            memory_key="k", kind="error", tool_name="Bash", has_error=True, narrative_chars=212
        )
    )
    assert obs["out_tool_name"] == "Bash" and obs["out_has_error"] == "true"
    assert obs["out_narrative_chars"] == "212" and obs["out_kind"] == "error"
    pm = project_result(ProcedureMatchOutput(matches=[], matched=0, procedure_key="", score=0.0))
    assert pm["out_matched"] == "0"
    pm2 = project_result(
        ProcedureMatchOutput(matches=[], matched=2, procedure_key="procedure:deploy", score=0.81)
    )
    assert pm2["out_matched"] == "2" and pm2["out_procedure_key"] == "procedure:deploy"
    from server.observability.event_args import IDENTIFIER_KEYS

    assert "premise_conflict" in IDENTIFIER_KEYS


# --- an observation of the memory plane is not a memory ------


def test_self_referential_tools_match_the_default_globs():
    from server.memory.domain.observation_tags import is_self_referential

    for t in (
        "mcp__loom__loom_memory_set",
        "mcp__loom__loom_memory_delete",
        "mcp__loom__loom_search",
        "loom_knowledge_add",
        "ToolSearch",
        "AskUserQuestion",
        "todowrite",
    ):
        assert is_self_referential(t), t
    for t in ("Bash", "Edit", "Read", "mcp__github__create_pull_request", "WebFetch", ""):
        assert not is_self_referential(t), t
    assert is_self_referential("Bash", ["bash"]) and not is_self_referential("Bash", [])


@pytest.mark.asyncio
async def test_record_of_a_loom_tool_keeps_the_step_not_the_content(install_ch):
    from server.memory.domain.observation_tags import CRYSTALLIZE_DONE_TAG, SELF_REFERENTIAL_TAG

    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            tool_name="mcp__loom__loom_memory_set",
            tool_input={
                "memory_key": "user.schedule.autumn",
                "value": "physio appointment Tuesday; skipping the company run",
            },
            tool_output={"ok": True},
            file_paths=["/tmp/x.py"],
        )
    )
    row = ch.insert_calls[0][1][0]
    value = json.loads(row["value"])
    assert value["narrative"] == "[tool_use] tool=mcp__loom__loom_memory_set"
    assert "physio" not in row["value"] and "user.schedule" not in row["value"]
    assert value["file_paths"] == [] and row["entities"] == []
    assert SELF_REFERENTIAL_TAG in row["tags"] and CRYSTALLIZE_DONE_TAG in row["tags"]
    assert out.skipped_reason == "self_referential"
    assert out.narrative_chars == len(value["narrative"]) and out.file_paths == []


@pytest.mark.asyncio
async def test_record_of_an_ordinary_tool_is_not_skipped(install_ch):
    ch = install_ch(StubCH())
    out = await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_output="3 passed",
        )
    )
    row = ch.insert_calls[0][1][0]
    assert out.skipped_reason == "" and "3 passed" in row["value"]
    assert "skipped:self_referential" not in row["tags"]


def test_path_sweep_drops_url_tails_and_punctuation():
    from server.memory.app.observe import _extract_paths, _looks_like_path

    narrative = (
        'input: {"command":"cd /tmp/blog && T=$(gcloud auth print-access-token) && curl -s '
        'https://docs.googleapis.com/v1/documents/1ClXq?fields=body //  ./gdoc_suggest.py"}'
    )
    paths = _extract_paths({"path": "//"}, ["/", "https://x.y/z.json", "server/main.py"], narrative)
    assert "server/main.py" in paths and "/tmp/blog" in paths and "./gdoc_suggest.py" in paths
    assert not any(p.startswith("//") or "://" in p or p == "/" for p in paths)
    assert not _looks_like_path("//docs.googleapis.com/v1/documents/1ClXq")
    assert not _looks_like_path("/") and not _looks_like_path("ab")
    assert _looks_like_path("a.py") and _looks_like_path("/token") and _looks_like_path("src/x")


def test_skipped_reason_rides_the_ledger():
    from server.memory.api.models_observe import ObserveRecordOutput
    from server.observability.event_args import IDENTIFIER_KEYS
    from server.settings import Settings

    assert "skipped_reason" in IDENTIFIER_KEYS
    assert ObserveRecordOutput.model_fields["skipped_reason"].default == ""
    assert "mcp__loom__*" in Settings.model_fields["observe_skip_tools"].default


# --- a replayed observation is the agent's action -----------


def _obs_row(narrative: str, when):
    from server.memory import store as memory_repo

    return memory_repo.MemoryRow(
        memory_id="33333333-3333-3333-3333-333333333333",
        org="o",
        namespace="n",
        agent="claude-code",
        user_id="-",
        session_id="s",
        memory_key="obs:s:1",
        memory_type="episodic",
        value={"kind": "command_run", "tool_name": "Bash", "narrative": narrative},
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=when,
        expires_at=None,
        valid_at=when,
        tags=["observation"],
        entities=[],
    )


def test_replay_turns_are_spoken_by_the_agent_at_the_observation_time(monkeypatch):
    import datetime as _dt

    from server.memory.app import observe_replay as replay
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS, actor_name

    when = _dt.datetime(2026, 9, 13, 7, 53, 56)
    rows = [_obs_row("[command_run] tool=Bash\ninput: pytest -q\noutput: 1 failed", when)]
    msgs = replay.replay_messages(rows, agent="claude-code")
    assert msgs[0].role == "user" and msgs[0].name == "claude-code"
    assert msgs[0].timestamp == when and "1 failed" in msgs[0].content
    assert replay.replay_instructions() == TRAJECTORY_EXTRACTION_INSTRUCTIONS
    assert 'never to "User"' in TRAJECTORY_EXTRACTION_INSTRUCTIONS
    assert len(TRAJECTORY_EXTRACTION_INSTRUCTIONS) <= 4096  # the field's cap
    # the wildcard agent has no name to speak under
    assert actor_name("*") == "" and actor_name("-") == "" and actor_name(" a ") == "a"
    assert replay.replay_messages(rows, agent="*")[0].name == ""


async def test_the_hooks_observed_at_dates_a_late_flushed_capture(install_ch):
    """The plugin dates each capture as `observed_at`; a
    capture the flush job posts hours later keeps the moment the tool ran.
    `observation_date` still wins when both are given."""
    ch = install_ch(StubCH())
    await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="claude-code",
            session_id="s",
            tool_name="Bash",
            tool_input={"command": "ls"},
            observed_at="2026-09-24T09:15:00.000Z",
        )
    )
    row = ch.insert_calls[0][1][0]
    assert str(row["valid_at"]).startswith("2026-09-24 09:15:00"), row["valid_at"]
    ch2 = install_ch(StubCH())
    await handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="n",
            agent="claude-code",
            session_id="s",
            tool_name="Bash",
            tool_input={"command": "ls"},
            observation_date="2026-09-20T08:00:00Z",
            observed_at="2026-09-24T09:15:00Z",
        )
    )
    assert str(ch2.insert_calls[0][1][0]["valid_at"]).startswith("2026-09-20 08:00:00")


@pytest.mark.asyncio
async def test_record_stores_no_content_that_a_blacklist_pattern_matches(install_ch, monkeypatch):
    from server.memory.app import blacklist_guard

    ch = install_ch(StubCH())
    seen: dict[str, Any] = {}
    hits: list[str] = []

    async def _match(*, org: str, namespace: str, value: str) -> Any:
        seen["value"] = value
        return SimpleNamespace(pattern_id="p-1") if "sk-live-" in value else None

    async def _note(pattern_id: str) -> None:
        hits.append(pattern_id)

    monkeypatch.setattr(blacklist_guard, "find_match", _match)
    monkeypatch.setattr(blacklist_guard, "note_hit", _note)
    out = await observe_handlers.handle_observe_record(
        ObserveRecordInput(
            org="o",
            namespace="ns",
            agent="a",
            session_id="s1",
            tool_name="Bash",
            tool_input={"command": "export KEY=sk-live-abc123 && deploy.sh /srv/app.py"},
            tool_output="ok",
        )
    )
    assert "sk-live-abc123" in seen["value"]  # the check ran on the full narrative
    assert out.skipped_reason == "blacklisted" and hits == ["p-1"]
    ((table, rows, _),) = ch.insert_calls
    row = rows[0]
    assert "sk-live" not in row["value"] and "deploy.sh" not in row["value"]
    assert "skipped:blacklisted" in row["tags"] and CRYSTALLIZE_DONE_TAG in row["tags"]
    assert row["entities"] == []
