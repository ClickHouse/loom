"""When a session of tool calls is distilled, its transition
events and its two notes (procedure, hint) are written next to the facts —
bounded, fail-open, from the observations' own content."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import trajectory_notes as tn


def _row(key: str, kind: str, tool: str, narrative: str, *, error: str = "", paths=(), t=0):
    return SimpleNamespace(
        memory_key=key,
        value={
            "kind": kind,
            "tool_name": tool,
            "narrative": narrative,
            "file_paths": list(paths),
            **({"error": error} if error else {}),
        },
        created_at=datetime(2026, 9, 12, 10, 0, t),
        valid_at=datetime(2026, 9, 12),
    )


ROWS = [
    _row(
        "o1",
        "file_read",
        "Read",
        "[file_read] tool=Read\ninput: server/main.py\noutput: def app(): ...",
        paths=("server/main.py",),
        t=0,
    ),
    _row(
        "o2",
        "file_edit",
        "Edit",
        "[file_edit] tool=Edit\ninput: server/main.py old=a new=b\noutput: ok",
        paths=("server/main.py",),
        t=1,
    ),
    _row(
        "o3",
        "error",
        "Bash",
        "[error] tool=Bash\ninput: pytest -q\nerror: 2 failed",
        error="2 failed",
        t=2,
    ),
    _row(
        "o4",
        "command_run",
        "Bash",
        "[command_run] tool=Bash\ninput: pytest -q\noutput: 12 passed",
        t=3,
    ),
    _row("o5", "file_read", "Read", "[file_read] tool=Read", t=4),  # tool name only: trivial
]


def test_observation_from_row_reads_the_structured_value_and_json_strings() -> None:
    o = tn.observation_from_row(ROWS[2])
    assert (o.kind, o.tool_name, o.error) == ("error", "Bash", "2 failed")
    assert not o.trivial
    assert tn.observation_from_row(ROWS[4]).trivial
    s = tn.observation_from_row(
        SimpleNamespace(
            memory_key="k",
            value='{"kind":"tool_use","tool_name":"X","narrative":"[tool_use] tool=X\\noutput: y"}',
            created_at=None,
            valid_at=None,
        )
    )
    assert s.tool_name == "X" and not s.trivial


def test_transition_pairs_skip_trivial_after_rows_and_keep_errors_under_the_cap() -> None:
    obs = [tn.observation_from_row(r) for r in ROWS]
    pairs = tn.transition_pairs(obs, max_pairs=12)
    # o5 is trivial as an "after" state, so (o4, o5) is dropped; four rows → three pairs
    assert [(a.memory_key, b.memory_key) for a, b in pairs] == [
        ("o1", "o2"),
        ("o2", "o3"),
        ("o3", "o4"),
    ]
    capped = tn.transition_pairs(obs, max_pairs=1)
    assert [(a.memory_key, b.memory_key) for a, b in capped] == [
        ("o2", "o3")
    ]  # the error pair survives
    assert tn.transition_pairs(obs, max_pairs=0) == []


def test_event_prompt_and_render_carry_the_action_and_the_error() -> None:
    obs = [tn.observation_from_row(r) for r in ROWS]
    p = tn.event_prompt(obs[1], obs[2])
    assert "ACTION: tool=Bash kind=error" in p and "ERROR: 2 failed" in p and "BEFORE" in p
    text = tn.render_event("pytest run fails", "Two tests fail after the edit.", obs[2])
    assert text.startswith("After Bash `pytest -q`: pytest run fails. Two tests fail")
    stepped = tn.render_event("pytest run fails", "Two tests fail.", obs[2], step=3, total=7)
    assert stepped.startswith("Step 3/7 · After Bash `pytest -q`: pytest run fails.")
    # the action is the observation's own input line, bounded; absent when the narrative has none
    assert tn.action_summary(obs[1]) == "server/main.py old=a new=b"
    # a JSON input renders as key=value words, an edit as old → new
    edit = tn.Observation(
        "e",
        "file_edit",
        "Edit",
        'input: {"file_path": "server/x.py", "old_string": "LIMIT 1", "new_string": "LIMIT 1 SETTINGS max_threads = 1"}',
        "",
        ("server/x.py",),
        None,
        None,
    )
    assert (
        tn.action_summary(edit)
        == "file_path=server/x.py 'LIMIT 1' → 'LIMIT 1 SETTINGS max_threads = 1'"
    )
    run = tn.Observation(
        "r",
        "command_run",
        "Bash",
        'input: {"command": "pytest -q tests", "timeout": 120000}',
        "",
        (),
        None,
        None,
    )
    assert tn.action_summary(run) == "command=pytest -q tests"
    assert tn.action_summary(tn.observation_from_row(ROWS[4])) == ""
    long = tn.Observation("k", "command_run", "Bash", "input: " + "x" * 300, "", (), None, None)
    assert len(tn.action_summary(long)) == 120 and tn.action_summary(long).endswith("…")


def test_parse_event_and_parse_notes_are_strict_about_shape() -> None:
    assert tn.parse_event('{"title": "Edit applied", "transition": "old replaced by new"}') == (
        "Edit applied",
        "old replaced by new",
    )
    assert tn.parse_event('{"title": "", "transition": "x"}') is None
    assert tn.parse_event("not json") is None
    n = tn.parse_notes(
        'prefix {"procedure": {"name": "Run the unit tests", "situation": "after editing server code",'
        ' "steps": ["edit", "pytest -q", "read failures"]}, "hint": {"title": "pytest here", '
        '"content": "- run from repo root"}} suffix'
    )
    assert n.procedure["name"] == "Run the unit tests" and len(n.procedure["steps"]) == 3
    assert n.hint["title"] == "pytest here"
    weak = tn.parse_notes(
        '{"procedure": {"name": "x", "situation": "y", "steps": ["one"]}, "hint": null}'
    )
    assert weak.procedure is None and weak.hint is None


def test_keys_are_deterministic_per_session() -> None:
    assert tn.event_memory_key("s1", "o2") == tn.event_memory_key("s1", "o2")
    assert tn.event_memory_key("s1", "o2") != tn.event_memory_key("s2", "o2")
    assert tn.hint_memory_key("s1").startswith("hint:")
    row = tn.chain_edge_row(org="o", namespace="n", from_key="event:a", to_key="event:b")
    assert row["relation"] == "precedes" and row["run_id"] == "trajectory_events"


class _LLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls.append((system, user))
        return SimpleNamespace(text=self.replies.pop(0) if self.replies else "")


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


SESSION = {"org": "o", "namespace": "n", "agent": "claude-code", "user_id": "-", "session_id": "s1"}


@pytest.mark.asyncio
async def test_distill_events_writes_one_memory_per_pair_and_chains_them(monkeypatch) -> None:
    from server.memory.app import write as write_mod

    written: list[Any] = []

    async def fake_set(inp):
        written.append(inp)
        return SimpleNamespace(memory_key=inp.memory_key)

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    obs = [tn.observation_from_row(r) for r in ROWS]
    llm = _LLM(
        [
            '{"title": "Edit applied", "transition": "main.py now defines app() with b"}',
            "garbage",
            '{"title": "Tests pass", "transition": "12 passed after the fix"}',
        ]
    )
    ch = _CH()
    res = await tn.distill_events(SESSION, obs, llm=llm, max_events=12, ch=ch)
    assert res.llm_calls == 3 and res.events_written == 2 and res.failures == []
    assert all(w.memory_type == "semantic" and "event" in w.tags for w in written)
    # step index in session order, the edit's own input named, then the model's title
    assert written[0].value.startswith(
        "Step 2/5 · After Edit `server/main.py old=a new=b`: Edit applied."
    )
    assert written[1].value.startswith("Step 4/5 · After Bash `pytest -q`: Tests pass.")
    assert "server/main.py" in written[0].entities
    assert written[0].source_trace == "observe.extract:event:o1>o2"
    assert written[0].valid_at == datetime(2026, 9, 12)
    # the two events are chained in order, once
    assert len(ch.inserts) == 1 and ch.inserts[0][0] == "loom.operator_edges"
    edge = ch.inserts[0][1][0]
    assert (edge["from_key"], edge["to_key"], edge["relation"]) == (
        written[0].memory_key,
        written[1].memory_key,
        "precedes",
    )


@pytest.mark.asyncio
async def test_distill_events_fails_open_per_pair(monkeypatch) -> None:
    from server.memory.app import write as write_mod

    async def boom(inp):
        raise RuntimeError("ch down")

    monkeypatch.setattr(write_mod, "handle_memory_set", boom)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    obs = [tn.observation_from_row(r) for r in ROWS[:3]]
    llm = _LLM(['{"title": "t", "transition": "x"}'] * 2)
    res = await tn.distill_events(SESSION, obs, llm=llm, max_events=12, ch=_CH())
    assert res.events_written == 0 and len(res.failures) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["switched_off", "stale"])
async def test_a_call_the_switch_holds_propagates_from_events_and_notes(
    monkeypatch, held: str
) -> None:
    """Not a failure to count: the caller leaves the session to distil again."""
    from server.llm import customer
    from server.settings import settings

    monkeypatch.setattr(settings, "observe_extract_notes_min_observations", 0)
    error = customer.ModelCallsDisabled if held == "switched_off" else customer.SwitchUnread

    class _Held:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise error("held")

    obs = [tn.observation_from_row(r) for r in ROWS]
    with pytest.raises(error):
        await tn.distill_events(SESSION, obs, llm=_Held(), max_events=12, ch=_CH())
    with pytest.raises(error):
        await tn.distill_notes(SESSION, obs, llm=_Held(), ch=_CH())


@pytest.mark.asyncio
async def test_distill_notes_writes_a_procedure_and_a_hint(monkeypatch) -> None:
    from server.settings import settings

    # The notes are floored at 15 useful observations; these
    # fixtures are a few rows, and the floor has its own test.
    monkeypatch.setattr(settings, "observe_extract_notes_min_observations", 0)
    import server.memory.app.procedure as proc
    from server.memory.app import write as write_mod

    procs: list[Any] = []
    hints: list[Any] = []

    async def fake_add(inp):
        procs.append(inp)
        return SimpleNamespace(memory_key="procedure:x")

    async def fake_set(inp):
        hints.append(inp)
        return SimpleNamespace(memory_key=inp.memory_key)

    monkeypatch.setattr(proc, "handle_procedure_add", fake_add)
    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    obs = [tn.observation_from_row(r) for r in ROWS]
    llm = _LLM(
        [
            '{"procedure": {"name": "Fix and re-test", "situation": "a unit test fails after an edit",'
            ' "steps": ["read the failing test", "edit", "pytest -q"]},'
            ' "hint": {"title": "pytest in this repo", "content": "- run pytest -q from the repo root"}}'
        ]
    )
    res = await tn.distill_notes(SESSION, obs, llm=llm)
    assert res.procedure_written and res.hint_written and res.llm_calls == 1
    assert procs[0].name == "Fix and re-test" and "from_observation" in procs[0].tags
    assert hints[0].tags == ["hint", "from_observation"] and hints[0].memory_type == "semantic"
    # the hint carries the session's paths as entities, so the entity-gated
    # fact leg can find it when a question names one of them
    assert hints[0].entities == ["server/main.py"]
    assert hints[0].value.startswith("pytest in this repo\n- run pytest")
    # the session prompt lists the calls in order with their errors
    assert "ERROR: 2 failed" in llm.calls[0][1] and "1. [file_read] tool=Read" in llm.calls[0][1]


@pytest.mark.asyncio
async def test_distill_notes_skips_a_session_of_bare_tool_names() -> None:
    obs = [tn.observation_from_row(ROWS[4])]
    llm = _LLM([])
    res = await tn.distill_notes(SESSION, obs, llm=llm)
    assert res.llm_calls == 0 and not res.procedure_written


@pytest.mark.asyncio
async def test_worker_extras_skip_trivial_sessions() -> None:
    from server.memory.workers import observe_extract as w

    # all trivial → no model is even constructed
    res = await w._distill_extras(SESSION, [ROWS[4]])
    assert res.llm_calls == 0 and res.failures == []


def test_settings_defaults() -> None:
    from server.settings import Settings

    f = Settings.model_fields
    assert f["observe_extract_max_events_per_session"].default == 12
    assert f["observe_extract_enabled"].default is False  # the worker itself stays opt-in


def test_hint_entities_are_the_sessions_distinct_paths_bounded() -> None:
    obs = [tn.observation_from_row(r) for r in ROWS]
    assert tn.hint_entities_for(obs) == ["server/main.py"]
    many = [
        tn.Observation(f"o{i}", "file_read", "Read", "x\ny", "", (f"f{i}.py",), None, None)
        for i in range(20)
    ]
    assert len(tn.hint_entities_for(many)) == 12


# --- a session's notes cover the session ----------------------


def test_transition_pairs_only_after_keeps_the_boundary_pair() -> None:
    obs = [tn.observation_from_row(r) for r in ROWS]
    keys = [o.memory_key for o in sorted(obs, key=lambda o: o.created_at)]
    # this pass distils the last two observations; the pair into the first
    # of them crosses the crystallize boundary and is kept
    pairs = tn.transition_pairs(obs, max_pairs=12, only_after=set(keys[-2:]))
    afters = [b.memory_key for _, b in pairs]
    assert set(afters) <= set(keys[-2:]) and pairs[0][0].memory_key == keys[-3]
    assert tn.transition_pairs(obs, max_pairs=12, only_after=set()) == []
    assert tn.session_tag("s1") == tn.session_tag("s1") != tn.session_tag("s2")
    assert tn.session_tag("s1").startswith("session:")


@pytest.mark.asyncio
async def test_events_of_a_later_pass_chain_onto_the_earlier_passs_last_event(
    monkeypatch,
) -> None:
    from server.memory.app import write as write_mod

    written: list[Any] = []

    async def fake_set(inp):
        written.append(inp)
        return SimpleNamespace(memory_key=inp.memory_key)

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    obs = [tn.observation_from_row(r) for r in ROWS]
    ordered = sorted(obs, key=lambda o: o.created_at)
    new = {ordered[3].memory_key}  # pass two distils the 4th observation only
    llm = _LLM(['{"title": "Tests pass", "transition": "12 passed after the fix"}'])
    ch = _CH()
    res = await tn.distill_events(SESSION, obs, llm=llm, max_events=12, ch=ch, only_after=new)
    assert res.events_written == 1 and len(written) == 1
    # numbered over the whole session, not the fragment
    assert written[0].value.startswith("Step 4/5 ·")
    # chained onto the event the earlier pass wrote for the 3rd observation
    edge = ch.inserts[0][1][0]
    assert edge["from_key"] == tn.event_memory_key("s1", ordered[2].memory_key)
    assert edge["to_key"] == written[0].memory_key


@pytest.mark.asyncio
async def test_a_later_pass_supersedes_the_sessions_earlier_procedure(monkeypatch) -> None:
    from server.settings import settings

    # The notes are floored at 15 useful observations; these
    # fixtures are a few rows, and the floor has its own test.
    monkeypatch.setattr(settings, "observe_extract_notes_min_observations", 0)
    import server.memory.app.handlers as hm
    import server.memory.app.procedure as proc
    from server.memory.app import write as write_mod

    deleted: list[str] = []

    async def fake_add(inp):
        return SimpleNamespace(memory_key="procedure_read_and_fix")

    async def fake_set(inp):
        return SimpleNamespace(memory_key=inp.memory_key)

    async def fake_delete(inp):
        deleted.append(inp.memory_key)
        return SimpleNamespace(tombstoned=True, version=1)

    monkeypatch.setattr(proc, "handle_procedure_add", fake_add)
    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    monkeypatch.setattr(hm, "handle_memory_delete", fake_delete)

    class _CHQ(_CH):
        async def query_all(self, sql, params=None):
            assert params["tag"] == tn.session_tag("s1")
            # the tag hashes the session id alone: the read is the session's
            # own identity, never another principal sharing the id
            assert (params["agent"], params["uid"], params["sid"]) == (
                SESSION.get("agent") or "-",
                SESSION.get("user_id") or "-",
                "s1",
            )
            assert "user_id = {uid:String}" in sql and "agent = {agent:String}" in sql
            return [
                {
                    "memory_key": "procedure_check_auth",
                    "agent": "claude-code",
                    "user_id": "-",
                    "session_id": "s1",
                },
                {
                    "memory_key": "procedure_read_and_fix",
                    "agent": "claude-code",
                    "user_id": "-",
                    "session_id": "s1",
                },
            ]

    obs = [tn.observation_from_row(r) for r in ROWS]
    llm = _LLM(
        [
            '{"procedure": {"name": "Read and fix", "situation": "a test fails", "steps": ["read", "fix"]},'
            ' "hint": {"title": "t", "content": "c"}}'
        ]
    )
    res = await tn.distill_notes(SESSION, obs, llm=llm, ch=_CHQ())
    assert res.procedure_written and res.procedures_superseded == 1
    assert deleted == ["procedure_check_auth"]  # the kept key is not deleted


@pytest.mark.asyncio
async def test_worker_extras_merge_the_session_context(monkeypatch) -> None:
    from server.memory.workers import observe_extract as w

    seen: dict[str, Any] = {}

    async def fake_events(session, obs, *, llm, max_events, res, only_after=None, ch=None):
        seen["n"] = len(obs)
        seen["only_after"] = only_after
        return res

    async def fake_notes(session, obs, *, llm, res, ch=None):
        seen["notes_n"] = len(obs)
        return res

    monkeypatch.setattr(tn, "distill_events", fake_events)
    monkeypatch.setattr(tn, "distill_notes", fake_notes)
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: object())
    context = [tn.observation_from_row(r) for r in ROWS[:3]]
    # this pass carries the last two rows; one of them is already in the context read
    res = await w._distill_extras(SESSION, ROWS[2:], context=context)
    assert seen["n"] == 5 and seen["notes_n"] == 5
    assert seen["only_after"] == {tn.observation_from_row(r).memory_key for r in ROWS[2:]}
    # no context → today's per-fragment behaviour
    await w._distill_extras(SESSION, ROWS[2:], context=None)
    assert seen["n"] == 3 and seen["only_after"] is None
    assert res is not None
