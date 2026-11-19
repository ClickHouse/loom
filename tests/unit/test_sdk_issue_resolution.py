"""The paired SDK protocol retains failed attempts and keeps grading outside the agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import sdk_issue_agent as agent
from bench import sdk_issue_resolution as bench


@pytest.fixture
def config(tmp_path: Path) -> argparse.Namespace:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "code.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "code.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    base = bench.git(repo, "rev-parse", "HEAD").decode().strip()
    task = tmp_path / "task.json"
    bench.write_json(
        task,
        {
            "instance_id": "example__repo-1",
            "repo": "example/repo",
            "base_commit": base,
            "problem_statement": "Correct the value.",
        },
    )
    grader = tmp_path / "grader.json"
    bench.write_json(
        grader,
        {
            "command": ["evaluator", "{patch}", "{output}"],
            "selected_tests": ["test_value"],
            "timeout_s": 30,
        },
    )
    return argparse.Namespace(
        checkout=repo,
        task=task,
        grader_config=grader,
        expected_base=base,
        out=tmp_path / "run",
        model="test-model",
        region="test-region",
        max_turns=3,
        max_tokens=1000,
        timeout_s=20,
        arm="baseline",
        org="example",
        namespace="code-example",
        base_url="https://example.invalid",
        token_env="BENCH_TOKEN",
    )


def response(*requests: dict, reason: str = "tool_use") -> dict:
    content = [{"toolUse": {"toolUseId": str(i), **r}} for i, r in enumerate(requests)]
    if not requests:
        content = [{"text": "Done."}]
    return {
        "stopReason": reason,
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {
            "inputTokens": 10,
            "outputTokens": 3,
            "cacheReadInputTokens": 4,
            "cacheWriteInputTokens": 2,
        },
    }


def execute_fake(monkeypatch: pytest.MonkeyPatch, responses: list, close_error=None) -> None:
    class Client:
        def converse(self, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def close(self):
            if close_error is not None:
                raise close_error

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *a, **k: Client()))

    def process(argv, **kwargs):
        out = Path(argv[-1])
        assert (out / "MANIFEST.json").exists()
        asyncio.run(bench.worker(out))
        return 0, False

    monkeypatch.setattr(bench, "run_process_group", process)


def edit() -> dict:
    return {"name": "replace_text", "input": {"path": "code.py", "old": "1", "new": "2"}}


def test_pair_has_identical_policy_local_tools_and_external_grade(config) -> None:
    baseline = bench.manifest_for(config)
    config.arm = "loom"
    treatment = bench.manifest_for(config)
    assert baseline["protocol"] == treatment["protocol"]
    assert baseline["tools"] == treatment["tools"][1:]
    assert "use it first" in baseline["protocol"]["system"]
    assert "Never make an unrelated call" in baseline["protocol"]["system"]
    assert all(t["toolSpec"]["name"] != "run_tests" for t in treatment["tools"])
    config.grader_config.write_text(config.grader_config.read_text().replace("30", "31"))
    assert bench.manifest_for(config)["protocol"] != baseline["protocol"]
    task = json.loads(config.task.read_text())
    task["test_patch"] = "held out"
    bench.write_json(config.task, task)
    with pytest.raises(ValueError, match="sanitized"):
        bench.manifest_for(config)


@pytest.mark.parametrize(
    "failure,status",
    [
        (response({"name": "read_file", "input": {"path": "../outside"}}), "guard_refused"),
        (RuntimeError("provider transport failed"), "provider_error"),
    ],
)
def test_partial_patch_and_returned_usage_survive_failure(config, monkeypatch, failure, status):
    execute_fake(monkeypatch, [response(edit()), failure])
    result = bench.run_trial(config)
    assert result["status"] == status and result["resolved"] is None
    assert result["usage"]["inputTokens"] >= 10
    assert result["usage"]["cacheReadInputTokens"] >= 4
    assert result["patch_bytes"] and b"+value = 2" in (config.out / "agent.diff").read_bytes()
    assert result["usage_complete"] is False
    assert (config.out / "RESULT.json").exists()
    if status == "guard_refused":
        assert result["failed_tool_calls"] == 1
    with pytest.raises(FileExistsError):
        bench.run_trial(config)


def test_turn_limit_is_not_completed_and_empty_patch_is_explicit(config, monkeypatch):
    config.max_turns = 1
    execute_fake(monkeypatch, [response(edit())])
    result = bench.run_trial(config)
    assert result["status"] == "max_turns" and result["patch_bytes"] > 0
    (config.checkout / "code.py").write_text("value = 1\n")
    config.out = config.out.parent / "empty"
    execute_fake(monkeypatch, [response(reason="end_turn")])
    result = bench.run_trial(config)
    assert result["status"] == "completed_empty_patch" and result["patch_bytes"] == 0
    assert result["model_selected_loom_calls"] == 0 and result["resolved"] is None


def test_loom_lookup_is_actual_optional_binding_and_errors_are_recorded(config, monkeypatch):
    import loom

    calls = []

    class SDK:
        def __init__(self, **kwargs):
            assert kwargs["retry"] == {"max_attempts": 1}
            assert kwargs["auto_run"] is False and kwargs["mentor_brief"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_with_meta(self, op, body):
            calls.append((op, body))
            if op == "code.index_status":
                return SimpleNamespace(
                    request_id="preflight", data={"git_head": config.expected_base}
                )
            raise RuntimeError("secret transport details")

    config.arm = "loom"
    monkeypatch.setenv("BENCH_TOKEN", "fixture-credential")
    monkeypatch.setattr(loom, "Agent", SDK)
    execute_fake(
        monkeypatch,
        [
            response(
                {
                    "name": "loom_code_lookup",
                    "input": {"kind": "search", "target": "value implementation"},
                }
            ),
            response(reason="end_turn"),
        ],
    )
    result = bench.run_trial(config)
    assert result["model_selected_loom_calls"] == result["sdk_failed_lookups"] == 1
    assert result["loom_results"] == [] and result["sdk_lookup_wall_s"] >= 0
    assert calls[-1][1]["token_budget"] == 1200 and "max_tokens" not in calls[-1][1]
    assert "fixture-credential" not in (config.out / "events.jsonl").read_text()
    assert "secret transport details" not in (config.out / "events.jsonl").read_text()
    config.out = config.out.parent / "zero-adoption"
    execute_fake(monkeypatch, [response(reason="end_turn")])
    unused = bench.run_trial(config)
    assert unused["model_selected_loom_calls"] == 0
    assert unused["status"] == "completed_empty_patch" and unused["resolved"] is None


@pytest.mark.parametrize(
    "http_status,code,expected_status,cleanup_fails",
    [
        (401, "AUTH_INVALID", "guard_refused", False),
        (403, "AUTH_INSUFFICIENT", "guard_refused", False),
        (409, "SESSION_PROTECTION_REQUIRED", "guard_refused", False),
        (403, "AUTH_INSUFFICIENT", "guard_refused", True),
        (400, "VALIDATION_FAILED", "provider_error", False),
        (503, "BACKEND_UNAVAILABLE", "provider_error", False),
    ],
)
def test_typed_sdk_preflight_failures_stop_before_agent_inference(
    config, monkeypatch, http_status, code, expected_status, cleanup_fails
):
    import loom
    from loom.errors import from_response

    failure = from_response(
        {"detail": {"code": code, "message": "private transport details"}},
        http_status,
        request_id="preflight-refusal",
    )
    calls = []

    class SDK:
        def __init__(self, **kwargs):
            assert kwargs["retry"] == {"max_attempts": 1}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("close")
            if cleanup_fails:
                raise RuntimeError("cleanup failed")

        async def call_with_meta(self, op, body):
            calls.append(op)
            raise failure

    def provider_must_not_start(*args, **kwargs):
        pytest.fail("Preflight failure must precede agent provider construction")

    config.arm = "loom"
    monkeypatch.setenv("BENCH_TOKEN", "fixture-credential")
    monkeypatch.setattr(loom, "Agent", SDK)
    execute_fake(monkeypatch, [])
    monkeypatch.setattr(sys.modules["boto3"], "client", provider_must_not_start)
    result = bench.run_trial(config)
    assert result["status"] == expected_status
    assert result["stage"] == "loom_preflight"
    assert result["preflight_status"] == "failed" and result["preflight_failed"] is True
    assert result["agent_inference_started"] is False
    assert result["usage"] == {} and result["tool_calls"] == 0
    assert result["error"]["http_status"] == http_status
    assert result["error"]["request_id"] == "preflight-refusal"
    assert calls == ["code.index_status", "close"]
    events = (config.out / "events.jsonl").read_text()
    assert "private transport details" not in events and "fixture-credential" not in events
    assert not any(e["type"] == "model_response" for e in bench.events_in(config.out))


def test_provider_failure_after_successful_preflight_retains_its_stage(config, monkeypatch):
    import loom

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_with_meta(self, op, body):
            return SimpleNamespace(
                request_id="preflight-ok", data={"git_head": config.expected_base}
            )

    config.arm = "loom"
    monkeypatch.setenv("BENCH_TOKEN", "fixture-credential")
    monkeypatch.setattr(loom, "Agent", SDK)
    execute_fake(monkeypatch, [RuntimeError("provider failed")])
    result = bench.run_trial(config)
    assert result["status"] == "provider_error"
    assert result["stage"] == "agent_inference"
    assert result["preflight_status"] == "passed" and result["preflight_failed"] is False
    assert result["agent_inference_started"] is True and result["usage_complete"] is False


def test_provider_cleanup_does_not_hide_a_guard_refusal(config, monkeypatch):
    execute_fake(
        monkeypatch,
        [response({"name": "read_file", "input": {"path": "../outside"}})],
        close_error=RuntimeError("close failed"),
    )
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused" and result["stage"] == "agent_inference"
    assert result["error"]["type"] == "PermissionError"
    assert result["usage"]["inputTokens"] == 10
    assert any(e["type"] == "provider_cleanup_error" for e in bench.events_in(config.out))


@pytest.mark.parametrize(
    "status,code",
    [(401, "AUTH_INVALID"), (403, "AUTH_INSUFFICIENT"), (409, "SESSION_PROTECTION_REQUIRED")],
)
def test_tool_time_sdk_refusal_retains_its_metadata_and_partial_patch(
    config, monkeypatch, status, code
):
    import loom
    from loom.errors import from_response

    failure = from_response(
        {"detail": {"code": code, "message": "denied"}}, status, request_id="tool-refusal"
    )

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_with_meta(self, op, body):
            if op == "code.index_status":
                return SimpleNamespace(request_id="ok", data={"git_head": config.expected_base})
            raise failure

    config.arm = "loom"
    monkeypatch.setenv("BENCH_TOKEN", "fixture-credential")
    monkeypatch.setattr(loom, "Agent", SDK)
    execute_fake(
        monkeypatch,
        [
            response(edit()),
            response(
                {"name": "loom_code_lookup", "input": {"kind": "symbol", "target": "pkg.value"}}
            ),
        ],
    )
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused" and result["stage"] == "agent_inference"
    assert result["error"]["http_status"] == status and result["error"]["code"] == code
    assert result["error"]["request_id"] == "tool-refusal"
    assert result["usage"]["inputTokens"] == 20
    assert b"+value = 2" in (config.out / "agent.diff").read_bytes()


@pytest.mark.parametrize("refusal", [True, False])
def test_patch_failure_keeps_the_primary_refusal_and_other_error_metadata(
    config, monkeypatch, refusal
):
    failure = (
        response({"name": "read_file", "input": {"path": "../outside"}})
        if refusal
        else RuntimeError("provider failed")
    )
    execute_fake(monkeypatch, [response(edit()), failure])
    original_git = bench.git

    def patch_read_fails(root, *args):
        if args[0] == "diff":
            raise OSError("patch read failed")
        return original_git(root, *args)

    monkeypatch.setattr(bench, "git", patch_read_fails)
    result = bench.run_trial(config)
    assert result["status"] == ("guard_refused" if refusal else "artifact_error")
    assert result["error"]["type"] == ("PermissionError" if refusal else "RuntimeError")
    assert result["patch_errors"] == ["OSError"]
    assert result["usage"]["inputTokens"] >= 10
    assert (config.checkout / "code.py").read_text() == "value = 2\n"


def test_cleanup_origin_permission_refusal_is_primary(config, monkeypatch):
    execute_fake(monkeypatch, [response(reason="end_turn")], close_error=PermissionError("denied"))
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused"
    assert result["stage"] == "provider_cleanup"
    assert result["error"]["type"] == "PermissionError"
    assert result["usage"]["inputTokens"] == 10


@pytest.mark.parametrize("provider_refuses", [True, False])
def test_cleanup_retains_the_first_refusal_across_both_resources(
    config, monkeypatch, provider_refuses
):
    import loom
    from loom.errors import from_response

    sdk_failure = from_response(
        {"detail": {"code": "AUTH_INSUFFICIENT", "message": "denied"}},
        403,
        request_id="cleanup-refusal",
    )

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            raise RuntimeError("secondary cleanup") if provider_refuses else sdk_failure

        async def call_with_meta(self, op, body):
            return SimpleNamespace(request_id="ok", data={"git_head": config.expected_base})

    config.arm = "loom"
    monkeypatch.setenv("BENCH_TOKEN", "fixture-credential")
    monkeypatch.setattr(loom, "Agent", SDK)
    execute_fake(
        monkeypatch,
        [response(reason="end_turn")],
        close_error=PermissionError("first refusal") if provider_refuses else None,
    )
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused"
    assert result["stage"] == ("provider_cleanup" if provider_refuses else "sdk_cleanup")
    if provider_refuses:
        assert result["error"]["type"] == "PermissionError"
        assert any(e["type"] == "sdk_cleanup_error" for e in bench.events_in(config.out))
    else:
        assert result["error"]["http_status"] == 403
        assert result["error"]["request_id"] == "cleanup-refusal"


@pytest.mark.parametrize("signal", ["sdk_terminal", "local_terminal", "sdk_tool", "local_tool"])
@pytest.mark.parametrize("supervisor", ["timeout", "nonzero", "interrupted"])
def test_persisted_refusal_survives_supervision(config, monkeypatch, signal, supervisor):
    from loom.errors import from_response

    sdk = signal.startswith("sdk")
    config.arm = "loom" if sdk else "baseline"
    failure = (
        from_response(
            {"detail": {"code": "AUTH_INSUFFICIENT", "message": "denied"}},
            403,
            request_id="persisted-refusal",
        )
        if sdk
        else PermissionError("denied")
    )
    detail = agent.error_detail(failure)

    def process(argv, **kwargs):
        path = Path(argv[-1]) / "events.jsonl"
        progress = {
            "stage": "agent_inference",
            "preflight_status": "passed" if sdk else "not_requested",
            "agent_inference_started": True,
        }
        agent.append_event(path, {"type": "worker_stage", **progress})
        agent.append_event(path, {"type": "model_response", "response": response(edit())})
        (config.checkout / "code.py").write_text("value = 2\n")
        if signal.endswith("terminal"):
            agent.append_event(
                path, {"type": "terminal", "status": "guard_refused", "error": detail, **progress}
            )
        else:
            name = "loom_code_lookup" if sdk else "read_file"
            agent.append_event(path, {"type": "tool_attempt", "name": name, "tool_use_id": "1"})
            agent.append_event(
                path,
                {
                    "type": "tool_result",
                    "name": name,
                    "status": "error",
                    "error": detail,
                    "duration_s": 0.125,
                },
            )
        if supervisor == "interrupted":
            raise KeyboardInterrupt("offline fixture")
        return (None, True) if supervisor == "timeout" else (17, False)

    monkeypatch.setattr(bench, "run_process_group", process)
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused" and result["error"] == detail
    assert result["stage"] == "agent_inference" and result["agent_inference_started"] is True
    assert result["usage"]["inputTokens"] == 10 and result["usage_complete"] is False
    assert result["patch_bytes"] > 0 and result["patch_errors"] == []
    assert b"+value = 2" in (config.out / "agent.diff").read_bytes()
    expected = {"timeout": "timeout", "nonzero": "worker_exited", "interrupted": "interrupted"}
    assert result["supervision"]["status"] == expected[supervisor]
    if supervisor == "nonzero":
        assert result["supervision"]["returncode"] == 17
    if supervisor == "interrupted":
        assert result["supervision"]["error"]["type"] == "KeyboardInterrupt"


@pytest.mark.parametrize(
    "supervisor,expected",
    [("timeout", "timeout"), ("nonzero", "worker_error"), ("zero", "completed_empty_patch")],
)
def test_successful_terminal_does_not_override_failed_supervision(
    config, monkeypatch, supervisor, expected
):
    def process(argv, **kwargs):
        agent.append_event(
            Path(argv[-1]) / "events.jsonl",
            {"type": "terminal", "status": "completed", "answer": "done"},
        )
        return {"timeout": (None, True), "nonzero": (17, False), "zero": (0, False)}[supervisor]

    monkeypatch.setattr(bench, "run_process_group", process)
    assert bench.run_trial(config)["status"] == expected


def test_path_boundary_includes_symlinks_and_git_metadata(config, tmp_path):
    (config.checkout / "link").symlink_to(tmp_path, target_is_directory=True)
    for name in ("../outside", str(tmp_path / "outside"), ".git/config", "link/task.json"):
        with pytest.raises(PermissionError):
            agent.local_path(config.checkout, name)


def test_local_search_context_is_bounded_and_identical_in_both_arms(config, monkeypatch):
    calls = []

    def search(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "rg" and argv[argv.index("-C") + 1] == "1"
        assert argv[argv.index("--max-count") + 1] == "12"
        assert argv[argv.index("--max-columns") + 1] == "300"
        assert argv[-3:] == ["--", "value", "context.py"]
        assert kwargs == {
            "cwd": config.checkout,
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout="context.py-1-before\ncontext.py:2:value = 1\ncontext.py-3-after\n"
        )

    monkeypatch.setattr(agent.subprocess, "run", search)
    result = agent.local_tool(
        config.checkout,
        "search_code",
        {"path": "context.py", "pattern": "value", "context_lines": 1},
    )
    assert "before" in result["matches"] and "after" in result["matches"]
    assert "context.py" in result["matches"] and result["truncated"] is False
    with pytest.raises(agent.ToolError, match="between 0 and 20"):
        agent.local_tool(config.checkout, "search_code", {"pattern": "value", "context_lines": 21})
    assert len(calls) == 1
    assert agent.tools("baseline")[0] == agent.tools("loom")[1]


def test_zero_exit_grader_is_not_resolution_and_missing_outcomes_are_invalid():
    failed = {
        "valid": True,
        "resolved": False,
        "selected_test_statuses": {"test_value": "FAILED"},
        "missing_tests": [],
    }
    assert bench.grade_outcome(failed, ["test_value"], 0, False) == {
        "status": "graded",
        "valid": True,
        "resolved": False,
    }
    assert bench.grade_outcome(None, ["test_value"], 0, False)["valid"] is False
    missing = {**failed, "selected_test_statuses": {}}
    assert bench.grade_outcome(missing, ["test_value"], 0, False)["status"] == "invalid_setup"
    assert (
        bench.grade_outcome({**failed, "resolved": True}, ["test_value"], 0, False)["valid"]
        is False
    )
    inconsistent = {**failed, "selected_test_statuses": {"test_value": "PASSED"}}
    assert bench.grade_outcome(inconsistent, ["test_value"], 0, False)["status"] == "invalid_setup"


def test_supervisor_refusal_and_timeout_still_finalize(config, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("admission refused")

    monkeypatch.setattr(bench, "run_process_group", denied)
    refused = bench.run_trial(config)
    assert refused["status"] == "guard_refused" and refused["patch_bytes"] == 0
    assert refused["resolved"] is None and refused["usage"] == {}
    config.out = config.out.parent / "timed-out"

    def timed_out(argv, **kwargs):
        out = Path(argv[-1])
        agent.append_event(
            out / "events.jsonl",
            {"type": "model_response", "response": response(reason="end_turn")},
        )
        (config.checkout / "code.py").write_text("value = 3\n")
        return None, True

    monkeypatch.setattr(bench, "run_process_group", timed_out)
    result = bench.run_trial(config)
    assert result["status"] == "timeout" and result["usage"]["inputTokens"] == 10
    assert b"+value = 3" in (config.out / "agent.diff").read_bytes()


def test_dirty_setup_refusal_preserves_checkout_without_claiming_patch(config, monkeypatch):
    tracked = config.checkout / "code.py"
    untracked = config.checkout / "existing.py"
    tracked.write_text("value = 9\n")
    untracked.write_text("existing = True\n")

    def unexpected_worker(*args, **kwargs):
        pytest.fail("A dirty checkout must be refused before starting the worker")

    monkeypatch.setattr(bench, "run_process_group", unexpected_worker)
    result = bench.run_trial(config)
    assert result["status"] == "guard_refused"
    assert result["tool_calls"] == 0 and result["patch_bytes"] == 0
    assert (config.out / "agent.diff").read_bytes() == b""
    assert tracked.read_text() == "value = 9\n"
    assert untracked.read_text() == "existing = True\n"


@pytest.mark.parametrize("completed_duration", [None, 0.125])
def test_unfinished_sdk_lookup_has_incomplete_timing(config, monkeypatch, completed_duration):
    config.arm = "loom"

    def timed_out(argv, **kwargs):
        events = Path(argv[-1]) / "events.jsonl"
        if completed_duration is not None:
            agent.append_event(
                events, {"type": "tool_attempt", "name": "loom_code_lookup", "tool_use_id": "1"}
            )
            agent.append_event(
                events,
                {
                    "type": "tool_result",
                    "name": "loom_code_lookup",
                    "tool_use_id": "1",
                    "status": "success",
                    "duration_s": completed_duration,
                },
            )
        agent.append_event(
            events, {"type": "tool_attempt", "name": "loom_code_lookup", "tool_use_id": "2"}
        )
        return None, True

    monkeypatch.setattr(bench, "run_process_group", timed_out)
    result = bench.run_trial(config)
    assert result["status"] == "timeout"
    assert result["sdk_lookup_wall_s"] is None
    assert result["sdk_lookup_timing_complete"] is False
    assert result["sdk_completed_lookup_wall_s"] == (completed_duration or 0)
    assert result["sdk_unfinished_lookups"] == 1


def test_external_grade_requires_structured_results_and_never_retries(config, monkeypatch):
    execute_fake(monkeypatch, [response(reason="end_turn")])
    bench.run_trial(config)

    def grader(*args, **kwargs):
        bench.write_json(
            kwargs["cwd"] / "result.json",
            {
                "valid": True,
                "resolved": False,
                "selected_test_statuses": {"test_value": "FAILED"},
                "missing_tests": [],
            },
        )
        assert kwargs["timeout_s"] == 30
        return 0, False

    monkeypatch.setattr(bench, "run_process_group", grader)
    result = bench.grade_trial(config.out)
    assert result["valid"] is True and result["resolved"] is False
    with pytest.raises(FileExistsError):
        bench.grade_trial(config.out)
