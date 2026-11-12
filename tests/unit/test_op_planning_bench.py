"""bench/op_planning.py scoring + fixture validity, offline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bench import op_planning as B
from bench.text2mem_convert import convert, convert_step

FIXTURE = Path("bench/fixtures/op_planning_tasks.jsonl")


def _tools(settings_override) -> dict[str, dict]:
    from server.transport.mcp_tools import _tools_descriptor

    settings_override(mcp_toolset="all")
    d = asyncio.run(_tools_descriptor(schema_profile="compact"))
    return {x["name"]: x["inputSchema"] for x in d}


def test_every_gold_tool_and_required_arg_exists_on_the_surface(settings_override) -> None:
    tools = _tools(settings_override)
    tasks = B.load_tasks(FIXTURE)
    assert len(tasks) >= 30
    for t in tasks:
        for g in t["gold"]:
            assert g["tool"] in tools, (t["id"], g["tool"])
            props = set((tools[g["tool"]].get("properties") or {}).keys())
            for a in g.get("required", []):
                assert a in props, (t["id"], g["tool"], a)
            for a in g.get("args") or {}:
                assert a in props, (t["id"], g["tool"], a)
    assert B.gaps(tasks, set(tools)) == []
    classes = {(t["class"]["instruction_type"], t["class"]["structure"]) for t in tasks}
    assert classes == {
        ("direct", "single"),
        ("indirect", "single"),
        ("direct", "workflow"),
        ("indirect", "workflow"),
        ("direct", "noop"),
        ("indirect", "noop"),
    }
    assert sum(1 for t in tasks if t.get("assertions")) >= 12
    assert len({t["id"] for t in tasks}) == len(tasks)


def test_fixture_exercises_the_revision_path(settings_override) -> None:
    """The revision verbs are in the gold, `memory.apply` is a small share
    of it, and four tasks have no write as their answer."""
    tools = _tools(settings_override)
    tasks = B.load_tasks(FIXTURE)
    steps = [g["tool"] for t in tasks for g in t["gold"]]
    from collections import Counter

    c = Counter(steps)
    assert c["loom_memory_apply"] == 6
    assert c["loom_memory_update"] >= 2 and c["loom_memory_pin"] >= 2
    assert c["loom_blacklist_add"] >= 1 and c["loom_working_memory_append"] >= 1
    dry = [
        t
        for t in tasks
        if any(
            (g.get("args") or {}).get("dry_run") is True and g["tool"] == "loom_memory_set"
            for g in t["gold"]
        )
    ]
    assert len(dry) >= 2
    noop = [t for t in tasks if t["class"]["structure"] == "noop"]
    assert len(noop) == 4 and all(t["gold"] == [] for t in noop)
    revision = [t for t in tasks if t.get("cohort") == "revision"]
    assert len(revision) == 12 and all(
        t.get("cohort", "legacy") in ("legacy", "revision") for t in tasks
    )
    # the seven apply tasks in the retired file stay runnable
    retired = B.load_tasks(Path("bench/fixtures/op_planning_tasks_retired.jsonl"))
    assert len(retired) == 7 and all(
        any(g["tool"] == "loom_memory_apply" for g in t["gold"]) for t in retired
    )
    assert not ({t["id"] for t in retired} & {t["id"] for t in tasks})
    # `dry_run` and the new verbs are on the core surface the bench lists
    assert "dry_run" in (tools["loom_memory_set"].get("properties") or {})
    for t in revision:
        for g in t["gold"]:
            assert g["tool"] in tools, (t["id"], g["tool"])


def test_noop_gold_scores_nothing_called_as_a_pass_and_reads_as_lenient() -> None:
    task = {"id": "n", "class": {"structure": "noop"}, "gold": []}
    schemas = {
        "loom_memory_get": {"properties": {"memory_key": {}}},
        "loom_memory_set": {"properties": {"memory_key": {}, "value": {}}},
    }
    s = B.score_task(task, [], schemas)
    assert s["sma"] is True and s["tool_acc"] == 1.0 and s["args_ok"] is True
    read = {"name": "loom_memory_get", "input": {"memory_key": "k"}}
    write = {"name": "loom_memory_set", "input": {"memory_key": "k", "value": "v"}}
    assert B.score_task(task, [write], schemas)["sma"] is False
    assert B.score_task(task, [read], schemas)["tool_acc"] == 0.0
    # a read before deciding not to write is caution, not a wrong plan
    assert B.lenient_calls([], [read]) == []
    assert B.score_task(task, B.lenient_calls([], [read]), schemas)["sma"] is True
    assert B.lenient_calls([], [read, write]) == [write]
    assert B.gaps([task], set(schemas)) == []


def test_first_turn_defaults_to_auto_and_stubs_carry_the_verdict_fields() -> None:
    assert B.FIRST_TURN_DEFAULT == "auto"
    import json as _json

    dry = _json.loads(B._stub_result({"name": "loom_memory_set", "input": {"dry_run": True}}))
    assert dry["dry_run"] is True and dry["noop"] is True and dry["version"] == 0
    real = _json.loads(B._stub_result({"name": "loom_memory_pin", "input": {}}))
    assert real["conflict_verdict"] == "clear" and real["version"] == 1
    upd = _json.loads(B._stub_result({"name": "loom_memory_update", "input": {}}))
    assert upd["version"] == 2
    got = _json.loads(B._stub_result({"name": "loom_memory_get", "input": {"memory_key": "kx"}}))
    assert got["found"] is True and got["memory_key"] == "kx"
    res = {
        "model": "m",
        "toolset": "core",
        "tools": 1,
        "listing_bytes": 1,
        "profile": "standard",
        "mean_input_tokens": 1.0,
        "gaps": [],
        "first_turn": "auto",
        "scored": [
            {
                "id": "a",
                "class": {},
                "sma": True,
                "lenient_sma": True,
                "tool_acc": 1.0,
                "args_ok": True,
                "hallucinated": False,
                "missing_steps": 0,
                "got": [],
                "cohort": "legacy",
            },
            {
                "id": "b",
                "class": {},
                "sma": False,
                "lenient_sma": False,
                "tool_acc": 0.0,
                "args_ok": False,
                "hallucinated": False,
                "missing_steps": 0,
                "got": ["x"],
                "cohort": "revision",
            },
        ],
    }
    text = B.summarise_plan(res)
    assert "cohort=legacy" in text and "cohort=revision" in text and "first_turn=auto" in text


def test_step_scoring_follows_the_ast_rules() -> None:
    gold = {
        "tool": "loom_memory_apply",
        "required": ["target", "action", "reason", "limit"],
        "args": {"action": "delete", "target": {"filter": {"tags_any": ["q3-migration"]}}},
    }
    schema = {
        "properties": {k: {} for k in ("target", "action", "reason", "limit", "dry_run", "label")}
    }
    ok = {
        "name": "loom_memory_apply",
        "input": {
            "target": {"filter": {"tags_any": ["Q3-Migration"], "memory_type": "semantic"}},
            "action": "delete",
            "reason": "done",
            "limit": 50,
            "dry_run": True,
        },
    }
    s = B.score_step(gold, ok, schema)
    assert s["ok"] and s["tool"] and s["required"] and s["literal"] and s["no_extra"]
    # wrong tool
    assert not B.score_step(gold, {**ok, "name": "loom_memory_delete"}, schema)["tool"]
    # missing required
    bad = {
        "name": "loom_memory_apply",
        "input": {k: v for k, v in ok["input"].items() if k != "reason"},
    }
    assert not B.score_step(gold, bad, schema)["required"]
    # literal mismatch (bool exact; enum exact)
    bad = {"name": "loom_memory_apply", "input": {**ok["input"], "action": "expire"}}
    assert not B.score_step(gold, bad, schema)["literal"]
    # hallucinated parameter
    bad = {"name": "loom_memory_apply", "input": {**ok["input"], "confirm": True}}
    st = B.score_step(gold, bad, schema)
    assert not st["no_extra"] and not st["ok"]
    # nothing emitted
    assert not B.score_step(gold, None, schema)["ok"]


def test_task_scoring_is_all_or_nothing_over_steps_and_extra_calls() -> None:
    task = {
        "id": "w",
        "class": {},
        "gold": [
            {"tool": "loom_memory_set", "required": ["memory_key", "value"]},
            {
                "tool": "loom_memory_lock",
                "required": ["memory_key", "reason"],
                "args": {"mode": "read_only"},
            },
        ],
    }
    schemas = {
        "loom_memory_set": {"properties": {"memory_key": {}, "value": {}}},
        "loom_memory_lock": {"properties": {"memory_key": {}, "reason": {}, "mode": {}}},
    }
    calls = [
        {"name": "loom_memory_set", "input": {"memory_key": "k", "value": "v"}},
        {
            "name": "loom_memory_lock",
            "input": {"memory_key": "k", "reason": "r", "mode": "read_only"},
        },
    ]
    assert B.score_task(task, calls, schemas)["sma"] is True
    assert B.score_task(task, calls[:1], schemas)["sma"] is False  # missing step
    assert B.score_task(task, calls + calls[:1], schemas)["sma"] is False  # extra call
    swapped = [calls[1], calls[0]]
    assert B.score_task(task, swapped, schemas)["sma"] is False  # order matters


def test_execution_helpers() -> None:
    assert B._tool_to_op("loom_memory_apply") == "memory.apply"
    assert B._tool_to_op("loom_search") == "memory.search"
    assert B._tool_to_op("loom_working_memory_set") == "working_memory.set"
    assert B._tool_to_op("loom_memory_update_by_id") == "memory.update_by_id"
    assert B._tool_to_op("loom_memory_update") == "memory.update"
    body = {
        "found": True,
        "tags": ["a", "ops"],
        "confidence": 0.4001,
        "lock_mode": "read_only",
        "expires_at": "2026-10-01T00:00:00",
    }
    checks = B._check(
        {
            "found": True,
            "tags": {"op": "contains", "value": "ops"},
            "confidence": {"op": "approx", "value": 0.4},
            "expires_at": {"op": "exists"},
            "lock_mode": "read_only",
        },
        body,
    )
    assert all(ok for _, ok, _ in checks)
    assert not B._check({"found": False}, body)[0][1]
    assert B._fill({"k": "{ns}-{id:a}"}, "ns1", {"a": "mid"}) == {"k": "ns1-mid"}


def test_text2mem_converter_maps_and_gaps() -> None:
    assert convert_step({"op": "Encode"})["tool"] == "loom_memory_set"
    assert (
        convert_step({"op": "Delete", "target": {"filter": {"has_tags": ["x"]}}})["tool"]
        == "loom_memory_apply"
    )
    assert convert_step({"op": "Delete", "target": {"ids": ["1"]}})["tool"] == "loom_memory_delete"
    assert convert_step({"op": "Lock", "target": {"ids": "7"}})["tool"] == "loom_memory_lock"
    assert convert_step({"op": "Lock", "target": {"search": {"intent": {"query": "q"}}}})[
        "args"
    ] == {"action": "lock"}
    assert convert_step({"op": "Promote"}) is None and convert_step({"op": "Merge"}) is None
    samples = [
        {
            "id": "a",
            "class": {"lang": "en", "instruction_type": "direct", "structure": "single"},
            "nl": {"en": "delete old"},
            "schema_list": [{"op": "Delete", "target": {"filter": {}}}],
        },
        {
            "id": "b",
            "class": {"lang": "en", "instruction_type": "direct", "structure": "single"},
            "nl": {"en": "merge"},
            "schema_list": [{"op": "Merge", "target": {"ids": ["1", "2"]}}],
        },
        {
            "id": "c",
            "class": {"lang": "zh", "instruction_type": "direct", "structure": "single"},
            "nl": {"zh": "..."},
            "schema_list": [{"op": "Encode"}],
        },
    ]
    tasks, gaps = convert(samples, "en")
    assert [t["id"] for t in tasks] == ["t2m-a"] and gaps == {"Merge": 1}
    assert json.dumps(tasks[0])  # serialisable


def test_lenient_view_collapses_preview_and_drops_reads() -> None:
    gold = [
        {
            "tool": "loom_memory_apply",
            "required": ["target", "action", "reason", "limit"],
            "args": {"action": "delete", "target": {"filter": {"tags_any": ["q3"]}}},
        }
    ]
    preview = {
        "name": "loom_memory_apply",
        "input": {
            "action": "delete",
            "reason": "r",
            "limit": 10,
            "target": {"filter": {"tags_any": ["q3"]}},
        },
    }
    enforce = {"name": "loom_memory_apply", "input": {**preview["input"], "dry_run": False}}
    read = {"name": "loom_memory_get", "input": {"memory_key": "k"}}
    out = B.lenient_calls(gold, [read, preview, enforce])
    assert out == [enforce]
    # a different scope does not collapse
    other = {"name": "loom_memory_apply", "input": {**enforce["input"], "target": {"keys": ["a"]}}}
    assert B.lenient_calls(gold, [preview, other]) == [preview, other]
    # reads stay when the gold asks for a read
    gold_r = [{"tool": "loom_search", "required": ["query"]}]
    assert (
        B.lenient_calls(gold_r, [read, {"name": "loom_search", "input": {"query": "q"}}])[0] == read
    )
    # a wrong tool is scored on its own schema for the hallucination check
    schemas = {
        "loom_memory_apply": {
            "properties": {"target": {}, "action": {}, "reason": {}, "limit": {}}
        },
        "loom_memory_get": {"properties": {"memory_key": {}}},
    }
    st = B.score_task({"id": "x", "class": {}, "gold": gold}, [read], schemas)
    assert st["steps"][0]["no_extra"] is True and st["sma"] is False


def test_lenient_view_keeps_a_two_step_apply_when_the_gold_asks_for_it() -> None:
    preview = {
        "name": "loom_memory_apply",
        "input": {
            "action": "delete",
            "reason": "r",
            "limit": 10,
            "target": {"filter": {"tags_any": ["tmp"]}},
        },
    }
    enforce = {"name": "loom_memory_apply", "input": {**preview["input"], "dry_run": False}}
    two_step_gold = [
        {"tool": "loom_memory_apply", "required": []},
        {"tool": "loom_memory_apply", "required": []},
    ]
    assert B.lenient_calls(two_step_gold, [preview, enforce]) == [preview, enforce]


def test_archetype_cohort_is_the_tasks_a_toolset_lists_every_gold_tool_of(
    settings_override,
) -> None:
    """The coding cohort = tasks with no GAP on the coding
    listing (a no-op gold always qualifies); the same set scores on every arm."""
    tasks = B.load_tasks(FIXTURE)
    coding = {t["name"] for t in B.load_tools("coding", "standard", "off")}
    core_code = {t["name"] for t in B.load_tools("core", "standard", "on")}
    cohort = B.archetype_cohort(tasks, coding)
    ids = {t["id"] for t in cohort}
    # Memory.pin, blacklist.add and working_memory.append are not on
    # the coding listing, so the four revision tasks whose gold names them
    # are outside the coding cohort (no GAP means the tier lists every gold tool);
    # the other eight revision tasks stay.
    assert len(cohort) == 21
    dropped = {"pin-direct-01", "pin-indirect-01", "blacklist-indirect-01", "wm-append-direct-01"}
    assert not (dropped & ids)
    assert all(
        t["id"] in ids for t in tasks if t.get("cohort") == "revision" and t["id"] not in dropped
    ), "the 8 listed revision tasks"
    assert {"set-direct-01", "get-direct-01", "search-direct-01", "delete-direct-01"} <= ids
    assert not (
        {"lock-direct-01", "apply-label-direct-01", "ask-indirect-01", "history-direct-01"} & ids
    )
    assert B.gaps(cohort, coding) == [] and B.gaps(cohort, core_code) == []
    # the coding listing is 20 tools (including tracker.duplicate_check,
    # bisect candidates, PR check and blast radius) and carries
    # the code tools without the flag
    assert (
        len(coding) == 21
        and "loom_code_search" in coding
        and "loom_tracker_duplicate_check" in coding
    )
    assert "loom_code_bisect_candidates" in coding and "loom_code_pr_check" in coding
    assert len(B.load_tools("coding", "standard", "on")) == 21
    assert len(B.load_tools("core", "standard", "off")) == 13  # + loom_op
    # the summary names the arm's settings
    res = {
        "model": "m",
        "toolset": "coding",
        "tools": 19,
        "listing_bytes": 1,
        "profile": "standard",
        "mean_input_tokens": 1.0,
        "gaps": [],
        "first_turn": "auto",
        "code_tools": "off",
        "cohort_toolset": "coding",
        "task_ids": ["a"],
        "scored": [
            {
                "id": "a",
                "class": {},
                "sma": True,
                "lenient_sma": True,
                "tool_acc": 1.0,
                "args_ok": True,
                "hallucinated": False,
                "missing_steps": 0,
                "got": [],
                "cohort": "revision",
            }
        ],
    }
    text = B.summarise_plan(res)
    assert "code_tools=off" in text and "cohort_toolset=coding" in text and "tasks=1" in text


def test_a_model_that_never_answers_errors_instead_of_scoring_no_calls(monkeypatch) -> None:
    """An empty reply reads as "no tool call", which scores a no-op task as a
    pass, so exhausted retries raise and the task is left out of the scores."""
    import httpx
    import pytest

    def dead(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": "overloaded"})

    monkeypatch.setattr(B.time, "sleep", lambda _s: None)
    with httpx.Client(transport=httpx.MockTransport(dead)) as c:
        with pytest.raises(B.ModelUnavailable):
            B._post_model(c, "k", {"model": "m"})


def test_execute_registers_each_namespace_as_bench_and_erases_them(monkeypatch) -> None:
    import httpx

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path, body))
        return httpx.Response(200, json={"memory_id": "m1", "results": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        B.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    task = {
        "id": "t",
        "gold": [{"tool": "loom_memory_set", "args": {"memory_key": "k", "value": "v"}}],
        "assertions": [{"op": "memory.get", "body": {"memory_key": "k"}, "expect": {}}],
    }
    res = B.run_execute([task], base_url="http://bench.invalid")
    assert res["results"][0]["passed"]
    paths = [p for p, _ in seen]
    assert paths[0] == "/v1/namespace.register" and paths[-1] == "/v1/erasure.request"
    ns = seen[0][1]["namespace"]
    assert seen[0][1]["kind"] == "bench" and ns.startswith("opb-")
    assert json.loads(seen[-1][1]["scope_value"])["namespaces"] == [ns]
