"""Saved credentials are applied before clients and jobs use model settings."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from server.core.background import TaskSet
from server.core.embedding_space import fingerprint as embedding_space_fingerprint
from server.core.model_work_gate import ModelWorkGate
from server.core.provider_keys import install_provider_keys, provider_key, saved_key_presence
from server.insights.api.models_provider_setup import ProviderSetupInput
from server.insights.app import provider_runtime as runtime
from server.insights.domain.provider_rebuild import REBUILD_TABLES
from server.insights.domain.provider_rebuild_manifest import RebuildManifest, RebuildTablePlan
from server.observability.problems import HealthState, collect_problems


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime, "settings", runtime.settings.model_copy(deep=True))
    monkeypatch.setattr(runtime, "_applied_version", "")
    monkeypatch.setattr(runtime, "_environment_settings", None)
    monkeypatch.setattr(runtime, "_maintenance_config_id", None)
    monkeypatch.setattr(runtime, "_refused", "")
    gate = ModelWorkGate()
    monkeypatch.setattr(runtime, "get_model_work_gate", lambda: gate)
    monkeypatch.setattr(runtime, "_refresh_calibration", AsyncMock())
    install_provider_keys({})
    yield
    install_provider_keys({})
    runtime.llm_client.reset()
    runtime.embedder.reset()


def setup_input() -> ProviderSetupInput:
    return ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "anthropic", "model": "claude-test"},
            "embedding": {"provider": "openai", "model": "text-embedding-test"},
            "keys": {"anthropic": "saved-llm-key", "openai": "saved-embedding-key"},
        }
    )


def prepared_serving_row(*, fingerprint=None):
    setup = setup_input()
    space = setup.embedding
    identity = uuid4()
    manifest = RebuildManifest(
        identity,
        fingerprint
        or embedding_space_fingerprint(
            space.provider, space.model, space.dimensions, space.base_url, space.send_dimensions
        ),
        tuple(
            RebuildTablePlan(
                table,
                uuid4(),
                "ReplacingMergeTree",
                ("embedding",),
                "a" * 64,
                uuid4(),
                uuid4(),
                uuid4(),
                0,
            )
            for table in REBUILD_TABLES
        ),
        (512,),
    )
    return {
        "config_id": str(identity),
        "state": "active",
        "version": 100,
        "envelope": "saved",
        "progress": json.dumps({"phase": "activate", "manifest": manifest.to_dict()}),
    }


@pytest.mark.asyncio
async def test_key_rotation_inherits_completed_serving_recipe(monkeypatch):
    completed = prepared_serving_row()
    rotated = {
        "config_id": str(uuid4()),
        "parent_id": completed["config_id"],
        "state": "active",
        "version": 101,
        "envelope": "saved",
        "progress": "{}",
    }
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[rotated, completed]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    runtime.settings.code_search_vector_model_default = "prefix:512:old-model"
    await runtime.load_active_configuration()
    assert runtime.settings.code_search_vector_model_default == "prefix:512:text-embedding-test"
    assert json.loads(runtime.settings.code_vector_tables) == {
        "prefix:512:text-embedding-test": {"table": "loom.code_chunks_prefix", "dim": 512}
    }
    assert not runtime.get_model_work_gate().closed


@pytest.mark.asyncio
async def test_inconsistent_serving_recipe_keeps_old_clients_and_admission_closed(monkeypatch):
    row = prepared_serving_row(fingerprint="f" * 64)
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    original = runtime.settings.embedding_model
    with pytest.raises(ValueError, match="Serving recipe"):
        await runtime.load_active_configuration()
    assert runtime.get_model_work_gate().closed
    assert runtime.settings.embedding_model == original
    assert runtime._applied_version == ""


def test_saved_keys_override_environment_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    before = dict(os.environ)
    install_provider_keys({"openai": "saved-key"})
    assert provider_key("openai") == "saved-key"
    assert saved_key_presence() == {"openai": True}
    assert dict(os.environ) == before
    install_provider_keys({"openai": ""})
    assert provider_key("openai") == ""
    install_provider_keys({})
    assert provider_key("openai") == "environment-key"


def test_gemini_supports_google_environment_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "alias-key")
    assert provider_key("gemini") == "alias-key"


def test_openai_only_setup_clears_mixed_provider_job_defaults() -> None:
    config = ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-test"},
            "embedding": {"provider": "openai", "model": "text-embedding-test"},
            "keys": {"openai": "one-test-key"},
        }
    )
    runtime.settings.memory_extract_verify_models = "claude-test"
    runtime.settings.code_ask_llm_model = "claude-other"
    runtime.settings.retrieval_judge_model = "claude-judge"
    runtime.apply_configuration(config)
    resolved = runtime.settings.model_dump(include=set(runtime.llm_setup_settings("", "", {}, [])))
    assert all(value in ("openai", "gpt-test", "") for value in resolved.values())
    assert provider_key("openai") == "one-test-key"
    assert saved_key_presence() == {"openai": True}


def test_environment_reset_restores_models_and_removes_saved_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")
    before = runtime.environment_settings()
    runtime.apply_configuration(setup_input())
    runtime.apply_environment_configuration()
    after = runtime.settings.model_dump(include=set(before))
    assert after == {name: value for name, value in before.items() if not name.startswith("_")}
    assert provider_key("openai") == "environment-test-key"
    assert saved_key_presence() == {}


@pytest.mark.asyncio
async def test_reset_revision_does_not_resurrect_an_older_dashboard_save(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": "reset", "state": "environment", "version": 124},
                {"config_id": "old", "state": "active", "version": 123, "envelope": "old"},
            ]
        ),
    )
    opened = Mock()
    monkeypatch.setattr(runtime, "open_configuration", opened)
    install_provider_keys({"openai": "old-dashboard-key"})
    await runtime.load_active_configuration()
    assert saved_key_presence() == {}
    assert runtime._applied_version == "124"
    opened.assert_not_called()


@pytest.mark.asyncio
async def test_environment_embedding_drift_fails_closed_before_reset(monkeypatch):
    expected = runtime.environment_settings()
    expected["embedding_provider"] = "openai"
    expected["embedding_model"] = "different-vector-space"
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {
                    "state": "environment",
                    "version": 124,
                    "progress": json.dumps({"environment": json.dumps(expected)}),
                }
            ]
        ),
    )
    applied = Mock()
    monkeypatch.setattr(runtime, "apply_environment_configuration", applied)
    with pytest.raises(RuntimeError, match="Environment embedding settings differ"):
        await runtime.load_active_configuration()
    applied.assert_not_called()
    assert runtime._applied_version == ""


def test_apply_single_provider_settings_and_reset_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_reset, embedding_reset = Mock(), Mock()
    monkeypatch.setattr(runtime.llm_client, "reset", llm_reset)
    monkeypatch.setattr(runtime.embedder, "reset", embedding_reset)
    runtime.apply_configuration(setup_input())
    assert runtime.settings.llm_provider == "anthropic"
    assert runtime.settings.memory_extract_verify_models == "claude-test"
    assert runtime.settings.session_brief_llm_model == ""
    assert runtime.settings.embedding_provider == "openai"
    assert runtime.settings.embedding_dim == 3072
    assert runtime.settings.require_real_providers
    assert provider_key("anthropic") == "saved-llm-key"
    llm_reset.assert_called_once()
    embedding_reset.assert_called_once()


def test_health_recognizes_saved_keys_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.apply_configuration(setup_input())
    monkeypatch.setattr(runtime.embedder, "settings", runtime.settings)
    problems = collect_problems(runtime.settings, HealthState(env={}))
    assert not {"llm_key_missing", "embedder_key_missing", "fake_embedder", "fake_llm"} & {
        p.code for p in problems
    }


def test_health_reports_missing_saved_embedding_key(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.apply_configuration(setup_input())
    monkeypatch.setattr(runtime.embedder, "settings", runtime.settings)
    install_provider_keys({"openai": "", "anthropic": "saved-key"})
    problems = collect_problems(runtime.settings, HealthState(env={"OPENAI_API_KEY": "old-key"}))
    assert "embedder_key_missing" in {problem.code for problem in problems}


@pytest.mark.asyncio
async def test_boot_ignores_pending_and_failed_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {"config_id": "pending", "state": "rebuilding", "envelope": "pending"},
                {"config_id": "failed", "state": "failed", "envelope": "failed"},
                {"config_id": "active", "state": "active", "envelope": "active", "version": 123},
            ]
        ),
    )
    opened = Mock(return_value=setup_input())
    applied = Mock()
    monkeypatch.setattr(runtime, "open_configuration", opened)
    monkeypatch.setattr(runtime, "apply_configuration", applied)
    await runtime.load_active_configuration()
    opened.assert_called_once_with("active")
    applied.assert_called_once()
    assert runtime.get_model_work_gate().closed


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "rebuilding", "validating", "failed"])
async def test_first_run_pending_configuration_blocks_data_plane_even_without_prior_save(
    monkeypatch, state
):
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {
                    "config_id": "pending",
                    "state": state,
                }
            ]
        ),
    )
    await runtime.load_active_configuration()
    assert runtime.get_model_work_gate().closed
    assert runtime._applied_version == ""


@pytest.mark.asyncio
async def test_committed_embedding_change_waits_for_local_requests_before_applying(monkeypatch):
    row = {"config_id": "new", "state": "active", "version": 123, "envelope": "saved"}
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    gate = runtime.get_model_work_gate()
    original_model = runtime.settings.embedding_model
    async with gate.admit():
        await runtime.load_active_configuration()
        assert gate.closed
        assert runtime.settings.embedding_model == original_model
        assert runtime._applied_version == ""
    await runtime.load_active_configuration()
    assert runtime.settings.embedding_model == "text-embedding-test"
    assert runtime._applied_version == "123"
    runtime._refresh_calibration.assert_awaited_once()
    assert not gate.closed


@pytest.mark.asyncio
async def test_committed_embedding_change_waits_for_registered_deferred_write(monkeypatch):
    row = {"config_id": "new", "state": "active", "version": 123, "envelope": "saved"}
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    original_model = runtime.settings.embedding_model
    release = asyncio.Event()
    observed_models = []

    async def deferred_write():
        await release.wait()
        observed_models.append(runtime.settings.embedding_model)

    task = TaskSet().spawn(deferred_write(), name="test-provider-deferred-write")
    try:
        await runtime.load_active_configuration()
        assert runtime.get_model_work_gate().closed
        assert runtime.settings.embedding_model == original_model
        assert runtime._applied_version == ""
        runtime._refresh_calibration.assert_not_awaited()
        assert not task.cancelled()
    finally:
        release.set()
        await task
    assert observed_models == [original_model]
    await runtime.load_active_configuration()
    assert runtime.settings.embedding_model == "text-embedding-test"
    assert runtime._applied_version == "123"
    assert not runtime.get_model_work_gate().closed


@pytest.mark.asyncio
async def test_failed_rebuild_keeps_last_configuration_closed_until_matching_commit(monkeypatch):
    rows = [{"config_id": "pending", "state": "failed"}]
    monkeypatch.setattr(runtime, "configurations", AsyncMock(side_effect=lambda: rows))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    await runtime.load_active_configuration()
    assert runtime.get_model_work_gate().closed
    rows[:] = [{"config_id": "pending", "state": "active", "version": 124, "envelope": "saved"}]
    await runtime.load_active_configuration()
    assert not runtime.get_model_work_gate().closed
    assert runtime._maintenance_config_id is None
    assert runtime._applied_version == "124"


@pytest.mark.asyncio
async def test_calibration_failure_cannot_reopen_admission_on_next_refresh(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {
                    "config_id": "new",
                    "state": "active",
                    "version": 123,
                    "envelope": "saved",
                }
            ]
        ),
    )
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    calibration = AsyncMock(side_effect=[RuntimeError("probe failed"), None])
    monkeypatch.setattr(runtime, "_refresh_calibration", calibration)
    with pytest.raises(RuntimeError, match="probe failed"):
        await runtime.load_active_configuration()
    assert runtime.get_model_work_gate().closed
    assert runtime._applied_version == ""
    await runtime.load_active_configuration()
    assert calibration.await_count == 2
    assert not runtime.get_model_work_gate().closed


@pytest.mark.asyncio
async def test_key_rotation_after_rebuild_commit_does_not_strand_a_lagging_replica(monkeypatch):
    rows = [{"config_id": "rebuild", "state": "rebuilding"}]
    monkeypatch.setattr(runtime, "configurations", AsyncMock(side_effect=lambda: rows))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    await runtime.load_active_configuration()
    rows[:] = [
        {
            "config_id": "rotation",
            "parent_id": "rebuild",
            "state": "active",
            "version": 126,
            "envelope": "latest",
        },
        {"config_id": "rebuild", "state": "active", "version": 125, "envelope": "rebuilt"},
    ]
    await runtime.load_active_configuration()
    assert not runtime.get_model_work_gate().closed
    assert runtime._applied_version == "126"
    runtime.open_configuration.assert_called_once_with("latest")


@pytest.mark.asyncio
async def test_unrelated_commit_cannot_reopen_an_unfinished_transition(monkeypatch):
    runtime._hold_admission("unfinished")
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(
            return_value=[
                {
                    "config_id": "unrelated",
                    "state": "active",
                    "version": 123,
                    "envelope": "saved",
                }
            ]
        ),
    )
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=setup_input()))
    with pytest.raises(RuntimeError, match="no matching committed"):
        await runtime.load_active_configuration()
    assert runtime.get_model_work_gate().closed
    assert runtime._applied_version == ""


@pytest.mark.asyncio
async def test_unreadable_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "configurations",
        AsyncMock(return_value=[{"state": "active", "envelope": "bad", "version": 123}]),
    )
    monkeypatch.setattr(
        runtime, "open_configuration", Mock(side_effect=ValueError("secret detail"))
    )
    with pytest.raises(RuntimeError) as error:
        await runtime.load_active_configuration()
    assert "secret detail" not in str(error.value)


def _behind_the_gateway(settings) -> None:
    settings.inference_gateway_required = True
    settings.inference_gateway_hosts = "gateway.example"
    settings.openai_base_url = "https://gateway.example/v1"
    settings.anthropic_base_url = "https://gateway.example"


def _gemini_setup() -> ProviderSetupInput:
    return ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "anthropic", "model": "claude-test"},
            "embedding": {
                "provider": "gemini",
                "model": "gemini-embedding-001",
                "dimensions": 3072,
            },
            "keys": {"anthropic": "saved-llm-key", "gemini": "saved-embedding-key"},
        }
    )


@pytest.mark.asyncio
async def test_a_refused_rebuilt_configuration_with_another_embedding_keeps_model_work_held(
    monkeypatch,
):
    # A Gemini index rebuilt at 3072 while the env embeds with OpenAI at 3072:
    # the widths match, so only holding model work keeps the env embedder off it.
    s = runtime.settings
    _behind_the_gateway(s)
    s.embedding_provider, s.embedding_model, s.embedding_dim = (
        "openai",
        "text-embedding-3-large",
        3072,
    )
    gemini = _gemini_setup().embedding
    row = prepared_serving_row(
        fingerprint=embedding_space_fingerprint(
            gemini.provider,
            gemini.model,
            gemini.dimensions,
            gemini.base_url,
            gemini.send_dimensions,
        )
    )
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[row]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=_gemini_setup()))
    before = (s.embedding_model, s.code_search_vector_model_default, s.code_vector_tables)
    for _ in range(2):  # every refresh keeps it so
        await runtime.load_active_configuration()
        assert runtime.get_model_work_gate().closed
        assert (
            s.embedding_model,
            s.code_search_vector_model_default,
            s.code_vector_tables,
        ) == before
        assert runtime._applied_version == ""
    assert "Gemini" in runtime.refused_configuration()
    codes = {
        p.code
        for p in collect_problems(
            s, HealthState(refused_configuration=runtime.refused_configuration())
        )
    }
    assert "model_configuration_refused" in codes


@pytest.mark.asyncio
async def test_a_refused_configuration_with_the_same_embedding_leaves_the_settings_serving(
    monkeypatch,
):
    s = runtime.settings
    _behind_the_gateway(s)
    s.embedding_provider, s.embedding_model, s.embedding_dim = "openai", "text-embedding-test", 3072
    s.embedding_base_url, s.embedding_send_dimensions = "", False
    refused = setup_input()
    monkeypatch.setattr(runtime, "configurations", AsyncMock(return_value=[prepared_serving_row()]))
    monkeypatch.setattr(runtime, "open_configuration", Mock(return_value=refused))
    monkeypatch.setattr(runtime, "gateway_refusal", lambda embedding: "refused for the test")
    await runtime.load_active_configuration()
    assert not runtime.get_model_work_gate().closed
    assert runtime.refused_configuration() == "refused for the test"
    assert runtime._applied_version == ""


def test_a_reset_to_the_environment_clears_a_refused_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime, "_refused", "LOOM_INFERENCE_GATEWAY_REQUIRED refuses the Gemini embedder"
    )
    runtime.apply_environment_configuration()
    assert runtime.refused_configuration() == ""
