"""The session brief plane: condensation, the revision
merge, grounding, rendering, the repository's column contract, and the four
handlers over an in-memory ClickHouse stand-in. CH-free, LLM-free."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_session_brief import (
    SessionBriefDeleteInput,
    SessionBriefGetInput,
    SessionBriefListInput,
    SessionBriefSaveInput,
)
from server.memory.api.ops_session_brief import OPERATIONS
from server.memory.app import session_brief as h
from server.memory.domain import session_brief as sb
from server.memory.domain import session_brief_render as sb_render
from server.memory.domain.session_brief import BriefEvent as E
from server.memory.store import session_briefs as repo
from tests.unit.schema_ddl import column_names, ddl

IDENT = {"org": "o", "namespace": "ns", "agent": "claude-code", "session_id": "s1"}


def _session() -> list[E]:
    return [
        E(kind="user", text="Add a session brief plane to loom. Never merge during a deploy hold."),
        E(kind="read", path="server/memory/api/context.py"),
        E(kind="read", path="server/memory/app/working_memory.py"),
        E(kind="search", text="grep session_summaries"),
        E(
            kind="assistant",
            text="The conversations table holds no coding turns, so the brief "
            "must come from client events.",
        ),
        E(kind="edit", path="server/memory/store/session_briefs.py"),
        E(kind="command", text="Run the unit tests"),
        E(kind="command", text="Run the unit tests"),
        E(kind="error", text="AssertionError: revision 2 != 1", ref="u-9"),
        E(kind="edit", path="server/memory/app/session_brief.py"),
        E(kind="assistant", text="Fixed the revision count; PR #2120 is open."),
    ]


# -- condensation ------------------------------------------------------------


def test_condense_keeps_every_ask_and_error_and_folds_reads() -> None:
    text, stats = sb.condense_events(_session(), 10_000)
    assert stats.events == 11 and stats.collapsed == 3  # 3 reads → 1 line, 1 repeated command
    assert text.count("[") == 8
    assert "[0 user] Add a session brief plane" in text
    assert "[8 error] AssertionError: revision 2 != 1 (ref u-9)" in text
    assert "3 reads/searches: server/memory/api/context.py" in text
    # deterministic
    assert sb.condense_events(_session(), 10_000)[0] == text


def test_condense_under_pressure_spends_on_asks_and_errors_first() -> None:
    events = [E(kind="user", text="the goal: ship it")]
    events += [E(kind="command", text=f"command number {i} " + "x" * 150) for i in range(200)]
    events += [E(kind="error", text="boom at the end")]
    text, stats = sb.condense_events(events, 3000)
    assert len(text) <= 3100
    assert "the goal: ship it" in text and "boom at the end" in text
    assert stats.sampled_out > 150
    # head and tail of the sampled tier both survive
    assert "command number 0 " in text and "command number 199 " in text
    assert "lower-priority lines of this window omitted" in text


def test_condense_when_even_the_asks_overflow_trims_them_to_share() -> None:
    events = [E(kind="user", text="q" * 600) for _ in range(40)]
    text, _ = sb.condense_events(events, 2000)
    assert len(text) <= 2100 and text.count("[") >= 10


def test_split_windows_caps_the_window_count() -> None:
    events = [E(kind="command", text="y" * 1000) for _ in range(100)]
    ws = sb.split_windows(events, 10_000, 4)
    assert len(ws) == 4 and sum(len(w) for w in ws) == 100
    assert sb.split_windows([], 10, 3) == []


# -- the model's answer ------------------------------------------------------


def test_parse_revision_tolerates_fences_strings_and_missing_keys() -> None:
    raw = (
        "```json\n"
        + json.dumps(
            {
                "goal": "ship briefs",
                "status": "tests green",
                "new_decisions": [
                    {"what": "store events client-side", "why": "no turns on server"},
                    "a bare decision",
                ],
                "new_dead_ends": [{"tried": "read loom.conversations", "why_not": "empty in prod"}],
                "open_questions": "one question",
                "next_steps": ["open PR"],
                "files": [{"path": "server/memory/app/session_brief.py", "role": "handler"}],
                "pointers": ["PR #2120"],
            }
        )
        + "\n```"
    )
    parsed = sb.parse_revision(raw)
    assert parsed is not None
    state = parsed.state
    assert state.goal == "ship briefs" and state.open_questions == ["one question"]
    assert [d.what for d in parsed.decisions] == ["store events client-side", "a bare decision"]
    assert parsed.dead_ends[0].why_not == "empty in prod"
    assert parsed.done == []
    with_done = sb.parse_revision(
        json.dumps(
            {
                "new_done": [
                    {"what": "migration 0100 harm usage-event", "ref": "PR #458"},
                    "bare item",
                ]
            }
        )
    )
    assert with_done is not None
    assert [(d.what, d.ref) for d in with_done.done] == [
        ("migration 0100 harm usage-event", "PR #458"),
        ("bare item", ""),
    ]
    assert sb.parse_revision("no json here") is None
    assert sb.parse_revision("{broken") is None
    assert sb.parse_revision("") is None


def test_grounding_drops_invented_files_and_pointers() -> None:
    state = sb.BriefSections(
        files=[
            sb.BriefFile(path="server/memory/app/session_brief.py"),
            sb.BriefFile(path="/abs/prefix/server/memory/store/session_briefs.py"),
            sb.BriefFile(path="server/invented_module.py"),
        ],
        pointers=["PR #2120", "PR #9999", "migration 1470 record", "see the docs"],
    )
    evidence = (
        "edit server/memory/app/session_brief.py\nserver/memory/store/session_briefs.py"
        "\nPR #2120 is open; Migration 1470"
    )
    grounded, dropped = sb.ground_sections(state, evidence)
    assert [f.path for f in grounded.files] == [
        "server/memory/app/session_brief.py",
        "/abs/prefix/server/memory/store/session_briefs.py",
    ]
    assert grounded.pointers == ["PR #2120", "migration 1470 record"]
    assert dropped == 3
    # a scheme prefix the events leave off still grounds; prose is not a path
    more, n = sb.ground_sections(
        sb.BriefSections(
            pointers=["doi:10.1000/182 (the spec)", "PR #7 (A/B bench)", "doi:10.9999/00000"]
        ),
        "we read 10.1000/182 today; PR #7 is open",
    )
    assert more.pointers == ["doi:10.1000/182 (the spec)", "PR #7 (A/B bench)"] and n == 1


def test_merge_replaces_state_and_only_appends_history() -> None:
    prev = sb.BriefSections(
        goal="g1",
        status="s1",
        decisions=[sb.BriefDecision(what="use RMT", why="point reads")],
        dead_ends=[sb.BriefDeadEnd(tried="TTL", why_not="owner rule")],
        next_steps=["old step"],
        files=[sb.BriefFile(path="a.py")],
        pointers=["#1"],
    )
    state = sb.BriefSections(
        status="s2",
        next_steps=["new step"],
        files=[sb.BriefFile(path="b.py"), sb.BriefFile(path="a.py")],
        pointers=["#2"],
    )
    merged = sb.merge_sections(
        prev,
        sb.WindowRevision(
            state=state,
            decisions=[sb.BriefDecision(what="Use RMT")],
            dead_ends=[sb.BriefDeadEnd(tried="mutations", why_not="slow on Cloud")],
            done=[sb.BriefDone(what="the briefs table", ref="PR #1")],
        ),
    )
    assert merged.goal == "g1" and merged.status == "s2"
    assert [d.what for d in merged.decisions] == ["use RMT"]  # dedup, case-folded
    assert [d.tried for d in merged.dead_ends] == ["TTL", "mutations"]
    assert merged.next_steps == ["new step"]
    assert [f.path for f in merged.files] == ["b.py", "a.py"]
    assert merged.pointers == ["#2", "#1"]
    assert [d.ref for d in merged.done] == ["PR #1"]


def test_history_never_collapses_across_many_revisions() -> None:
    cur: sb.BriefSections | None = None
    for i in range(30):
        cur = sb.merge_sections(
            cur,
            sb.WindowRevision(
                state=sb.BriefSections(status=f"s{i}"),
                decisions=[sb.BriefDecision(what=f"decision {i}")],
                done=[sb.BriefDone(what=f"done {i}")],
            ),
        )
    assert cur is not None
    assert len(cur.decisions) == sb.LIST_CAPS["decisions"]
    first_kept = 30 - sb.LIST_CAPS["decisions"]
    assert cur.decisions[-1].what == "decision 29"
    assert cur.decisions[0].what == f"decision {first_kept}"
    assert len(cur.done) == sb.LIST_CAPS["done"] and cur.done[-1].what == "done 29"


def test_overlay_keeps_what_the_agent_left_empty() -> None:
    prev = sb.BriefSections(goal="g", status="s", next_steps=["x"], constraints=["never y"])
    given = sb.BriefSections(status="done", decisions=[sb.BriefDecision(what="d", why="w")])
    out = sb.overlay_sections(prev, given)
    assert (out.goal, out.status, out.next_steps) == ("g", "done", ["x"])
    assert out.constraints == ["never y"] and out.decisions[0].why == "w"


def test_deterministic_revision_reads_goal_status_files_rules_and_pointers() -> None:
    rev = sb.deterministic_revision(_session())
    state, dec, dead = rev.state, rev.decisions, rev.dead_ends
    assert state.goal.startswith("Add a session brief plane")
    assert state.status == "Fixed the revision count; PR #2120 is open."
    assert [f.path for f in state.files] == [
        "server/memory/app/session_brief.py",
        "server/memory/store/session_briefs.py",
    ]
    assert any("Never merge during a deploy hold" in c for c in state.constraints)
    assert "PR #2120" in state.pointers
    assert dec == [] and dead == []
    # the error was fixed by a later edit → not an open question
    assert state.open_questions == []
    late = [*_session(), E(kind="error", text="ImportError: no module x")]
    s2 = sb.deterministic_revision(late).state
    assert s2.open_questions == ["unresolved error: ImportError: no module x"]
    printed = [
        E(kind="command", text="Open the PR — $ gh pr create"),
        E(kind="note", text="output: https://github.com/ClickHouse/loom/pull/2120"),
    ]
    done = sb.deterministic_revision(printed).done
    assert [(d.what, d.ref) for d in done] == [
        ("Open the PR", "https://github.com/ClickHouse/loom/pull/2120")
    ]


# -- render ------------------------------------------------------------------


def test_render_frames_the_brief_and_trims_low_sections_first() -> None:
    s = sb.BriefSections(
        goal="ship",
        status="half done",
        next_steps=["a", "b"],
        pointers=[f"pointer {i} " + "p" * 150 for i in range(10)],
        files=[sb.BriefFile(path=f"f{i}.py", role="r" * 150) for i in range(15)],
    )
    meta = sb_render.RenderMeta(
        brief_key="auto/loom/main",
        revision=3,
        updated_at="2026-09-26 08:00Z",
        trigger="precompact",
        project="loom",
        branch="main",
        head_sha="abc123",
        sessions=2,
        events_total=1200,
        source_chars=400_000,
        latest_session="s1",
    )
    full = sb_render.render_brief(s, meta, char_cap=100_000)
    assert full.startswith('# Session brief "auto/loom/main" — revision 3')
    assert sb_render.BRIEF_FRAME in full and "not a source of facts" in full
    assert "condensed from 1,200 events (391 KB) of 2 sessions" in full
    assert "Next steps:\n1. a\n2. b" in full
    short = sb_render.render_brief(s, meta, char_cap=2500)
    assert len(short) <= 2500 and "Goal:\nship" in short and "Next steps:" in short
    assert short.count("pointer ") < full.count("pointer ")


def test_brief_keys() -> None:
    assert sb.auto_brief_key("loom", "feat/session-briefs") == "auto/loom/feat-session-briefs"
    assert sb.auto_brief_key("", "") == "auto/project/detached"
    # the plugin's autoBriefKey (lib/session-brief.mjs) is held to these too
    assert sb.auto_brief_key("my repo", "user/a b") == "auto/my-repo/user-a-b"
    assert sb.brief_key_ok("loom@feat/x") and not sb.brief_key_ok("has space")
    assert not sb.brief_key_ok("")


# -- the repository's column contract ---------------------------------------


def _ddl_columns(table: str) -> list[str]:
    return column_names(table)


def test_builder_columns_match_the_table() -> None:
    assert [*repo.COLUMNS, "version"] == _ddl_columns("session_briefs")


def test_the_revisions_view_selects_every_revisions_column() -> None:
    view = ddl("session_brief_revisions_mv").split("AS SELECT", 1)[1]
    view = view.split("FROM loom.session_briefs", 1)[0]
    selected = [c.strip() for c in view.split(",")]
    assert selected == _ddl_columns("session_brief_revisions")
    assert set(selected) <= {*repo.COLUMNS, "version"}


def test_builder_carries_every_column_forward_and_refuses_unknown_ones() -> None:
    prev = repo.build_brief_row(
        None, org="o", namespace="ns", brief_key="k", goal="g", head_sha="abc", revision=4
    )
    nxt = repo.build_brief_row(prev, tombstoned_at=repo.now())
    assert nxt["goal"] == "g" and nxt["head_sha"] == "abc" and nxt["revision"] == 4
    assert nxt["version"] > prev["version"] and nxt["created_at"] == prev["created_at"]
    with pytest.raises(KeyError):
        repo.build_brief_row(prev, not_a_column=1)
    with pytest.raises(ValueError):
        repo.build_brief_row(None, org="o", namespace="ns", brief_key="")


# -- handlers over an in-memory ClickHouse ------------------------------------


class _MemCH:
    """Just enough of loom.session_briefs + its revisions mirror: the MV is
    emulated on insert, FINAL is the max version per key."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.revisions: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings=None) -> None:
        assert table == "loom.session_briefs"
        cols = set(_ddl_columns("session_brief_revisions"))
        for r in rows:
            assert set(r) == {*repo.COLUMNS, "version"}, "a version row must carry every column"
            self.rows.append(dict(r))
            self.revisions.append({k: v for k, v in r.items() if k in cols})

    def _newest(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.rows:
            k = r["brief_key"]
            if k not in out or r["version"] > out[k]["version"]:
                out[k] = r
        return out

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        p = params or {}
        if "FROM loom.session_briefs FINAL" in sql:
            row = self._newest().get(p["key"])
            if row and "scope_uid" in p and row["user_id"] != p["scope_uid"]:
                return None
            return row
        if "uniqExact(revision)" in sql:
            return {"n": len({r["revision"] for r in self.revisions if r["brief_key"] == p["key"]})}
        if "FROM loom.session_brief_revisions" in sql:
            hits = [
                r
                for r in self.revisions
                if r["brief_key"] == p["key"] and r["revision"] == p["rev"]
            ]
            return max(hits, key=lambda r: r["version"]) if hits else None
        raise AssertionError(sql)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        p = params or {}
        assert "LIMIT 1 BY brief_key" in sql
        rows = [r for r in self._newest().values() if r["tombstoned_at"] is None]
        if "project" in p:
            rows = [r for r in rows if r["project"] == p["project"]]
        if "agent" in p:
            rows = [r for r in rows if r["agent"] == p["agent"]]
        if "sid" in p:
            rows = [r for r in rows if p["sid"] in r["source_sessions"]]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return rows[: p.get("lim", 1)]


class _LLM:
    provider = "openai"
    model = "gpt-test"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 1024, response_format=None):
        from server.llm.client import LLMResult

        self.calls.append((system, user))
        text = self.replies.pop(0) if self.replies else "{}"
        if text == "RAISE":
            raise RuntimeError("provider down")
        return LLMResult(
            text=text,
            tokens_in=len(user) // 4,
            tokens_out=50,
            provider=self.provider,
            model=self.model,
        )


@pytest.fixture()
def mem(monkeypatch) -> _MemCH:
    ch = _MemCH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    from server.knowledge.store import conversations as conversation_repo

    ch.recording = True

    async def _recording(org, namespace, ch_=None):
        return ch.recording

    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", _recording)
    monkeypatch.setattr(h.settings, "session_brief_llm", True)
    monkeypatch.setattr(h.settings, "session_brief_window_chars", 24_000)
    monkeypatch.setattr(h.settings, "session_brief_max_windows", 4)
    return ch


def _llm(monkeypatch, replies: list[str]) -> _LLM:
    llm = _LLM(replies)
    import server.llm.client as client_mod

    monkeypatch.setattr(client_mod, "get_llm_client_for_model", lambda model: llm)
    return llm


def _save(**kw: Any):
    return asyncio.run(h.handle_session_brief_save(SessionBriefSaveInput(**{**IDENT, **kw})))


def test_save_with_the_model_revises_grounds_and_counts(mem, monkeypatch) -> None:
    reply = json.dumps(
        {
            "goal": "Add a session brief plane",
            "status": "handler written, tests failing",
            "new_decisions": [
                {"what": "events come from the client", "why": "no turns server-side"}
            ],
            "new_dead_ends": [],
            "open_questions": ["why revision 2 != 1"],
            "next_steps": ["fix the count"],
            "files": [
                {"path": "server/memory/app/session_brief.py", "role": "handler"},
                {"path": "server/made_up.py", "role": "?"},
            ],
            "constraints": ["Never merge during a deploy hold"],
            "pointers": ["PR #2120", "PR #1"],
        }
    )
    llm = _llm(monkeypatch, [reply])
    out = _save(
        project="loom",
        branch="feat/x",
        head_sha="19d01dc28",
        trigger="precompact",
        events=_session(),
        event_cursor="120",
    )
    assert out.changed and out.revision == 1 and out.author == "llm"
    assert out.brief_key == "auto/loom/feat-x"
    assert out.stats.dropped_ungrounded == 2 and out.stats.events_in == 11
    assert out.stats.llm_model == "gpt-test" and out.stats.tokens_out == 50
    assert "server/made_up.py" not in out.rendered and "PR #2120" in out.rendered
    system, user = llm.calls[0]
    assert system == sb.SYSTEM_PROMPT and "PREVIOUS BRIEF:\n{}" in user
    assert "Session context: project loom, branch feat/x, head 19d01dc28." in user
    row = mem.rows[-1]
    assert (
        row["source_class"] == "derived"
        and row["actor"]
        and row["prompt_version"] == sb.PROMPT_VERSION
    )
    assert row["source_sessions"] == ["s1"] and row["event_cursor"] == "120"

    # a retry of the same session + cursor writes nothing
    again = _save(project="loom", branch="feat/x", events=_session(), event_cursor="120")
    assert not again.changed and again.reason == "already-saved" and len(mem.rows) == 1

    # the next window sees the previous brief and appends history
    reply2 = json.dumps(
        {
            "status": "tests green",
            "new_decisions": [
                {"what": "count revisions from the stored row", "why": "the MV mirrors it"}
            ],
            "next_steps": ["open the PR"],
        }
    )
    llm.replies.append(reply2)
    out2 = _save(
        session_id="s2",
        project="loom",
        branch="feat/x",
        events=[E(kind="assistant", text="tests green")],
        event_cursor="200",
    )
    assert out2.revision == 2
    assert '"events come from the client"' in llm.calls[1][1]
    stored = sb_render.sections_from_json(mem.rows[-1]["sections"])
    assert [d.what for d in stored.decisions] == [
        "events come from the client",
        "count revisions from the stored row",
    ]
    assert stored.goal == "Add a session brief plane" and stored.next_steps == ["open the PR"]
    assert mem.rows[-1]["source_sessions"] == ["s1", "s2"]
    assert mem.rows[-1]["events_total"] == 12


def test_save_falls_back_to_the_deterministic_builder(mem, monkeypatch) -> None:
    _llm(monkeypatch, ["RAISE"])
    out = _save(brief_key="mine", events=_session())
    assert out.changed and out.author == "deterministic"
    assert "Goal:\nAdd a session brief plane" in out.rendered
    _llm(monkeypatch, ["not json at all"])
    out2 = _save(brief_key="mine", events=[E(kind="assistant", text="later")])
    assert out2.author == "deterministic" and out2.revision == 2


def test_the_fake_provider_never_writes_a_brief(mem, monkeypatch) -> None:
    fake = _LLM([])
    fake.provider = "fake"
    import server.llm.client as client_mod

    monkeypatch.setattr(client_mod, "get_llm_client_for_model", lambda model: fake)
    out = _save(brief_key="k", events=_session())
    assert out.author == "deterministic" and fake.calls == []


def test_a_long_session_is_revised_window_by_window(mem, monkeypatch) -> None:
    monkeypatch.setattr(h.settings, "session_brief_window_chars", 2000)
    monkeypatch.setattr(h.settings, "session_brief_max_windows", 3)
    llm = _llm(monkeypatch, [json.dumps({"status": f"w{i}"}) for i in range(1, 4)])
    events = [E(kind="command", text=f"step {i} " + "z" * 300) for i in range(60)]
    out = _save(brief_key="long", events=events)
    assert out.stats.windows == 3 and len(llm.calls) == 3
    assert all(len(u) < 2000 * 2 for _, u in llm.calls)
    assert '"status":"w1"' in llm.calls[1][1]  # window 2 sees window 1's revision
    assert "Status:\nw3" in out.rendered


def test_model_written_sections_overlay_without_a_model_call(mem, monkeypatch) -> None:
    llm = _llm(monkeypatch, [])
    _save(brief_key="named", use_llm=False, events=_session())
    out = _save(
        brief_key="named",
        trigger="model",
        sections=sb.BriefSections(
            status="saved by the agent", dead_ends=[sb.BriefDeadEnd(tried="x", why_not="y")]
        ),
    )
    assert out.author == "model" and out.revision == 2 and llm.calls == []
    stored = sb_render.sections_from_json(mem.rows[-1]["sections"])
    assert stored.goal.startswith("Add a session brief plane")
    assert stored.status == "saved by the agent" and stored.dead_ends[0].tried == "x"


def test_compare_and_set_and_the_nothing_to_save_case(mem, monkeypatch) -> None:
    _llm(monkeypatch, [])
    assert _save(brief_key="k", events=[]).reason == "nothing-to-save"
    _save(brief_key="k", use_llm=False, events=_session())
    with pytest.raises(HTTPException) as e:
        _save(brief_key="k", use_llm=False, events=_session(), if_revision=0)
    assert e.value.status_code == 409
    assert _save(brief_key="k", use_llm=False, events=_session(), if_revision=1).revision == 2
    with pytest.raises(HTTPException) as e2:
        asyncio.run(
            h.handle_session_brief_save(
                SessionBriefSaveInput(org="o", namespace="ns", agent="a", events=_session())
            )
        )
    assert e2.value.status_code == 422


def test_get_list_revision_and_delete(mem, monkeypatch) -> None:
    _llm(monkeypatch, [])
    _save(project="loom", branch="main", use_llm=False, events=_session())
    _save(
        project="loom",
        branch="main",
        use_llm=False,
        events=[E(kind="assistant", text="second window")],
    )
    _save(
        session_id="other",
        agent="codex",
        brief_key="elsewhere",
        project="other",
        use_llm=False,
        events=[E(kind="user", text="a different project")],
    )

    got = asyncio.run(
        h.handle_session_brief_get(
            SessionBriefGetInput(**IDENT, brief_key="auto/loom/main", include_sections=True)
        )
    )
    assert got.found and got.revision == 2 and got.revisions == 2
    assert got.sections is not None and got.sections.status == "second window"
    assert got.rendered.startswith('# Session brief "auto/loom/main" — revision 2')
    first = asyncio.run(
        h.handle_session_brief_get(
            SessionBriefGetInput(**IDENT, brief_key="auto/loom/main", revision=1)
        )
    )
    assert first.found and first.revision == 1 and "revision 1" in first.rendered
    by_project = asyncio.run(
        h.handle_session_brief_get(SessionBriefGetInput(**IDENT, project="loom"))
    )
    assert by_project.brief_key == "auto/loom/main"
    by_session = asyncio.run(
        h.handle_session_brief_get(SessionBriefGetInput(**IDENT, from_session="other"))
    )
    assert by_session.brief_key == "elsewhere"
    with pytest.raises(HTTPException):
        asyncio.run(h.handle_session_brief_get(SessionBriefGetInput(**IDENT)))

    listed = asyncio.run(h.handle_session_brief_list(SessionBriefListInput(**IDENT)))
    assert [b.brief_key for b in listed.briefs] == ["elsewhere", "auto/loom/main"]
    assert "- auto/loom/main (rev 2, just now, claude-code): Add a session brief" in listed.rendered
    mine = asyncio.run(h.handle_session_brief_list(SessionBriefListInput(**IDENT, mine_only=True)))
    assert [b.brief_key for b in mine.briefs] == ["auto/loom/main"]

    gone = asyncio.run(
        h.handle_session_brief_delete(SessionBriefDeleteInput(**IDENT, brief_key="auto/loom/main"))
    )
    assert gone.deleted and gone.revision == 2
    assert mem.rows[-1]["goal"].startswith("Add a session brief")  # carried forward
    after = asyncio.run(
        h.handle_session_brief_get(SessionBriefGetInput(**IDENT, brief_key="auto/loom/main"))
    )
    assert not after.found and after.deleted
    again = asyncio.run(
        h.handle_session_brief_delete(SessionBriefDeleteInput(**IDENT, brief_key="auto/loom/main"))
    )
    assert not again.deleted
    # a save after the delete starts fresh but keeps counting revisions
    fresh = _save(
        project="loom", branch="main", use_llm=False, events=[E(kind="user", text="new start")]
    )
    assert fresh.revision == 3
    assert sb_render.sections_from_json(mem.rows[-1]["sections"]).goal == "new start"
    # ... and the forgotten brief's revisions are not served through it
    old = asyncio.run(
        h.handle_session_brief_get(
            SessionBriefGetInput(**IDENT, brief_key="auto/loom/main", revision=1)
        )
    )
    assert not old.found
    cur = asyncio.run(
        h.handle_session_brief_get(
            SessionBriefGetInput(**IDENT, brief_key="auto/loom/main", revision=3)
        )
    )
    assert cur.found and "new start" in cur.rendered


def test_a_deleted_brief_serves_no_revision(mem, monkeypatch) -> None:
    _llm(monkeypatch, [])
    _save(brief_key="gone", use_llm=False, events=_session())
    asyncio.run(h.handle_session_brief_delete(SessionBriefDeleteInput(**IDENT, brief_key="gone")))
    past = asyncio.run(
        h.handle_session_brief_get(SessionBriefGetInput(**IDENT, brief_key="gone", revision=1))
    )
    assert not past.found and past.deleted


def test_a_token_bounded_to_one_user_cannot_read_or_replace_anothers_brief(
    mem, monkeypatch
) -> None:
    from tests.unit.principals import acting_as, principal

    _llm(monkeypatch, [])
    _save(brief_key="auto/loom/main", user_id="u2", use_llm=False, events=_session())
    pinned = principal("writer", org="o", namespaces=("ns",), read_user_id="u1")
    with acting_as(pinned):
        for kw in ({"events": []}, {"events": _session()}, {"if_revision": 0}):
            with pytest.raises(HTTPException) as e:
                _save(brief_key="auto/loom/main", user_id="u1", use_llm=False, **kw)
            assert e.value.status_code == 409
            assert e.value.detail["code"] == "BRIEF_KEY_TAKEN"
            assert "revision" not in e.value.detail
            assert "Never merge" not in str(e.value.detail)
        gone = asyncio.run(
            h.handle_session_brief_delete(
                SessionBriefDeleteInput(**IDENT, user_id="u1", brief_key="auto/loom/main")
            )
        )
        assert not gone.deleted and gone.revision == 0
        mine = _save(brief_key="mine", user_id="u1", use_llm=False, events=_session())
        assert mine.changed and mine.revision == 1
    row = mem._newest()["auto/loom/main"]
    assert (row["user_id"], row["revision"], row["tombstoned_at"]) == ("u2", 1, None)


def test_registration() -> None:
    names = [op.name for op in OPERATIONS]
    assert names == [
        "session.brief_save",
        "session.brief_get",
        "session.brief_list",
        "session.brief_delete",
    ]
    save = OPERATIONS[0]
    assert save.audit and save.required_role.name == "WRITER"
    assert OPERATIONS[1].required_role.name == "VIEWER"
    from server.operations.registry import OPERATIONS_BY_NAME

    assert all(n in OPERATIONS_BY_NAME for n in names)


def test_a_revision_cut_off_by_the_output_cap_keeps_its_finished_keys() -> None:
    full = json.dumps(
        {
            "goal": "ship briefs",
            "status": "handler written",
            "new_decisions": [{"what": "events come from the client", "why": "no turns"}],
            "files": [{"path": "a.py", "role": "x"}, {"path": "b.py", "role": "y"}],
        }
    )
    cut = full[: full.index('{"path": "b.py"') + 12]  # mid-string in the last item
    parsed = sb.parse_revision(cut)
    assert parsed is not None
    assert parsed.state.goal == "ship briefs" and parsed.state.status == "handler written"
    assert [d.what for d in parsed.decisions] == ["events come from the client"]
    assert [f.path for f in parsed.state.files] == ["a.py"]
    assert sb.parse_revision('{"goal": "x", "status": "an unterminated') is not None
    assert sb.parse_revision('{"goal') is None


def test_an_oversized_or_overlong_reply_is_clipped_to_the_model_caps() -> None:
    reply = json.dumps(
        {
            "files": [{"path": "p" * 900, "role": "r" * 900}]
            + [{"path": f"f{i}.py", "role": "x"} for i in range(120)],
            "new_done": [{"what": "w" * 900, "ref": "#" * 900}],
            "pointers": [f"#{i}" for i in range(200)],
        }
    )
    parsed = sb.parse_revision(reply)
    assert parsed is not None
    assert len(parsed.state.files[0].path) <= 500 and len(parsed.state.files[0].role) <= 200
    assert len(parsed.state.files) <= 40 and len(parsed.state.pointers) <= 40
    assert len(parsed.done[0].ref) <= 200


def test_a_reply_the_parser_rejects_degrades_to_the_deterministic_window(mem, monkeypatch) -> None:
    _llm(monkeypatch, ['{"goal": "x"}'])
    monkeypatch.setattr(h.sb, "parse_revision", lambda text: (_ for _ in ()).throw(ValueError("x")))
    out = _save(brief_key="k", events=_session())
    assert out.changed and out.author == "deterministic"


def test_the_deterministic_builder_never_replaces_a_goal() -> None:
    first = sb.merge_sections(None, sb.deterministic_revision(_session()))
    later = sb.merge_sections(
        first, sb.deterministic_revision([E(kind="user", text="also fix the flaky test")])
    )
    assert later.goal == first.goal and later.goal.startswith("Add a session brief plane")
    # a model revision may still refine it
    refined = sb.merge_sections(
        later, sb.WindowRevision(state=sb.BriefSections(goal="g. Now: ship it."))
    )
    assert refined.goal == "g. Now: ship it."


def test_the_brief_model_falls_back_to_the_deployment_llm_then_to_none(monkeypatch) -> None:
    import server.llm.client as client_mod

    real, fake = _LLM([]), _LLM([])
    fake.provider = "fake"
    monkeypatch.setattr(h.settings, "session_brief_llm", True)
    monkeypatch.setattr(h.settings, "session_brief_llm_model", "claude-haiku-4-5")
    inp = SessionBriefSaveInput(**IDENT, brief_key="k")
    picks = {"claude-haiku-4-5": real, None: fake}
    monkeypatch.setattr(client_mod, "get_llm_client_for_model", lambda m: picks[m])
    assert h._use_llm(inp) == (True, real)
    picks = {"claude-haiku-4-5": fake, None: real}
    assert h._use_llm(inp) == (True, real)
    picks = {"claude-haiku-4-5": fake, None: fake}
    assert h._use_llm(inp) == (False, None)
    assert h._use_llm(inp.model_copy(update={"use_llm": False})) == (False, None)


def test_the_client_compaction_summary_is_kept_first_under_pressure() -> None:
    events = [E(kind="summary", text="S " * 3000)]
    events += [E(kind="command", text=f"cmd {i} " + "x" * 150) for i in range(200)]
    text, _ = sb.condense_events(events, 12_000)
    assert text.startswith("[0 summary] S S") and len(text.split("\n")[0]) > 5000
    assert sb.BriefEvent(kind="summary", text="y" * 15000).text  # fits the wire model


def test_automatic_saves_follow_the_recording_switch(mem, monkeypatch) -> None:
    _llm(monkeypatch, [])
    mem.recording = False
    off = _save(brief_key="rec", use_llm=False, trigger="precompact", events=_session())
    assert not off.changed and off.reason == "recording_off" and mem.rows == []
    # a save the person asked for is theirs to make
    asked = _save(brief_key="rec", use_llm=False, trigger="manual", events=_session())
    assert asked.changed and asked.revision == 1
    mem.recording = True
    on = _save(
        brief_key="rec", use_llm=False, trigger="session_end", events=[E(kind="user", text="later")]
    )
    assert on.changed and on.revision == 2


def test_the_client_summary_is_kept_verbatim_and_served_on_full(mem, monkeypatch) -> None:
    _llm(monkeypatch, [])
    summary = "1. Primary Request: ship briefs. 2. Key decision: events come from the client."
    _save(
        brief_key="cs",
        use_llm=False,
        events=[*_session(), E(kind="summary", text=summary, ts="2026-09-26T09:00:00Z")],
    )
    _save(brief_key="cs", use_llm=False, events=[E(kind="user", text="next window")])
    stored = sb_render.sections_from_json(mem.rows[-1]["sections"])
    assert stored.client_summary == summary and stored.client_summary_at.startswith("2026-09-26")
    plain = asyncio.run(h.handle_session_brief_get(SessionBriefGetInput(**IDENT, brief_key="cs")))
    assert summary not in plain.rendered
    full = asyncio.run(
        h.handle_session_brief_get(SessionBriefGetInput(**IDENT, brief_key="cs", full=True))
    )
    assert full.rendered.startswith(plain.rendered) and full.rendered.endswith(summary)
    assert "verbatim" in full.rendered


def test_a_rule_must_come_from_the_persons_words() -> None:
    sources = sb.rule_sources(
        None,
        [
            E(kind="user", text="Never merge during a deploy hold, and always run the tests."),
            E(kind="assistant", text="Briefs cap strings at 300 characters."),
        ],
    )
    state = sb.BriefSections(
        constraints=[
            "Never merge during a deploy hold",
            "Brief strings are capped at 300 characters",
            "Always run the tests before pushing",
        ]
    )
    grounded, dropped = sb.ground_sections(state, "", sources)
    assert grounded.constraints == [
        "Never merge during a deploy hold",
        "Always run the tests before pushing",
    ]
    assert dropped == 1
