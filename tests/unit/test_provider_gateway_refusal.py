"""A dashboard configuration the gateway check refuses applies nothing, and
what replaces it takes over the hold it keeps, through the real save, reset
and refresh paths over one store of configuration rows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from server.core.model_work_gate import ModelWorkGate
from server.core.provider_keys import install_provider_keys
from server.insights.api.models_provider_setup import (
    ProviderResetInput,
    ProviderSetupInput,
    ProviderValidation,
)
from server.insights.app import provider_activation as activation
from server.insights.app import provider_coordinator
from server.insights.app import provider_reset as reset
from server.insights.app import provider_runtime as runtime
from server.observability.problems import HealthState, collect_problems

_ENV = ("openai", "text-embedding-3-large", 3072, "", False)


def _setup(provider: str = "compatible", base_url: str = "https://gateway.example/v1") -> Any:
    model = "gemini-embedding-001" if provider == "gemini" else "text-embedding-3-small"
    embedding: dict[str, Any] = {"provider": provider, "model": model, "dimensions": 3072}
    if provider == "compatible":
        embedding["base_url"] = base_url
    return ProviderSetupInput.model_validate(
        {
            "llm": {"provider": "openai", "model": "gpt-6-luna"},
            "embedding": embedding,
            "keys": {"openai": "k"},
        }
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The configuration rows, newest first, shared by save, reset and the
    runtime, with the runtime's settings behind a gateway at gateway.example
    and the environment embedding with OpenAI at 3072."""
    s = runtime.settings.model_copy(deep=True)
    s.inference_gateway_required = True
    s.inference_gateway_hosts = "gateway.example"
    s.openai_base_url = "https://gateway.example/v1"
    s.anthropic_base_url = "https://gateway.example"
    s.embedding_provider, s.embedding_model, s.embedding_dim = (
        "openai",
        "text-embedding-3-large",
        3072,
    )
    s.embedding_base_url, s.embedding_send_dimensions = "", False
    for module in (runtime, activation):
        monkeypatch.setattr(module, "settings", s)
    rows: list[dict[str, Any]] = []
    envelopes: dict[str, ProviderSetupInput] = {}

    async def configurations() -> list[dict[str, Any]]:
        return list(rows)

    async def write_configuration(row: dict[str, Any]) -> None:
        rows.insert(0, dict(row))

    def seal(setup: ProviderSetupInput, keys: Any) -> str:
        envelope = f"sealed-{len(envelopes)}"
        envelopes[envelope] = setup
        return envelope

    @asynccontextmanager
    async def lock() -> Any:
        yield None

    gate = ModelWorkGate()
    for module in (runtime, activation, reset):
        monkeypatch.setattr(module, "configurations", configurations)
        monkeypatch.setattr(module, "get_model_work_gate", lambda: gate, raising=False)
    for module in (activation, reset):
        monkeypatch.setattr(module, "write_configuration", write_configuration)
        monkeypatch.setattr(module, "configuration_lock", lock)
        monkeypatch.setattr(module, "rebuild_required", AsyncMock(return_value=True))
    open_envelope = Mock(side_effect=lambda envelope: envelopes[envelope])
    for module in (runtime, activation):
        monkeypatch.setattr(module, "open_configuration", open_envelope)
    monkeypatch.setattr(activation, "_open_active", open_envelope)
    monkeypatch.setattr(reset, "_open_active", open_envelope)
    monkeypatch.setattr(activation, "seal_configuration", seal)
    monkeypatch.setattr(
        activation,
        "validate_providers",
        AsyncMock(return_value=ProviderValidation(ready=True, checks=[])),
    )
    monkeypatch.setattr(activation, "provider_for", lambda _: "openai")
    monkeypatch.setattr(runtime, "_applied_version", "")
    monkeypatch.setattr(runtime, "_environment_settings", None)
    monkeypatch.setattr(runtime, "_maintenance_config_id", None)
    monkeypatch.setattr(runtime, "_refused", "")
    monkeypatch.setattr(runtime, "_refresh_calibration", AsyncMock())
    monkeypatch.setattr(runtime, "environment_embedding", lambda: _ENV)
    monkeypatch.setattr(reset, "environment_embedding", lambda: _ENV)
    install_provider_keys({})
    yield {"rows": rows, "envelopes": envelopes, "gate": gate, "settings": s}
    install_provider_keys({})
    runtime.llm_client.reset()
    runtime.embedder.reset()


def _commit(store: dict[str, Any], setup: ProviderSetupInput, version: int = 100) -> str:
    """A configuration committed (by another replica) and stored."""
    envelope = f"committed-{version}"
    store["envelopes"][envelope] = setup
    config_id = str(uuid4())
    store["rows"].insert(
        0, {"config_id": config_id, "state": "active", "version": version, "envelope": envelope}
    )
    return config_id


@pytest.mark.parametrize(
    "refused",
    [
        _setup("compatible", "https://myres.openai.azure.com/openai"),
        _setup("compatible", "https://10.0.0.5/v1"),
        _setup("gemini"),
    ],
)
@pytest.mark.asyncio
async def test_a_configuration_off_the_gateway_applies_nothing_and_holds_model_work(
    store: dict[str, Any], refused: ProviderSetupInput
) -> None:
    s = store["settings"]
    before = s.model_dump(include={"embedding_provider", "embedding_base_url", "llm_model"})
    config_id = _commit(store, refused)
    await runtime.load_active_configuration()
    assert s.model_dump(include={"embedding_provider", "embedding_base_url", "llm_model"}) == before
    assert store["gate"].closed and runtime._maintenance_config_id == config_id
    assert store["gate"].refusal == runtime.refused_configuration()  # answered, not retried
    if refused.embedding.provider == "compatible":
        assert "the saved embedding base URL" in runtime.refused_configuration()
    else:
        assert "the saved Gemini embedder" in runtime.refused_configuration()
    state = HealthState(env={}, refused_configuration=runtime.refused_configuration())
    assert "model_configuration_refused" in {p.code for p in collect_problems(s, state)}


@pytest.mark.asyncio
async def test_a_configuration_at_the_gateway_applies(store: dict[str, Any]) -> None:
    s = store["settings"]
    _commit(store, _setup("compatible", "https://gateway.example/v1"))
    for _ in range(2):  # the second refresh resumes once the first has drained
        await runtime.load_active_configuration()
    assert s.embedding_base_url == "https://gateway.example/v1"
    assert runtime.refused_configuration() == "" and not store["gate"].closed


@pytest.mark.asyncio
async def test_a_reset_needing_a_rebuild_takes_over_a_refusals_hold(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    assert store["gate"].closed
    result = await reset.reset_configuration(ProviderResetInput(expected_version="100"))
    assert result.state == "queued"
    await runtime.load_active_configuration()  # no "does not own admission"
    queued = store["rows"][0]
    assert queued["state"] == "queued" and queued["parent_id"].hex == refused.replace("-", "")
    assert runtime._maintenance_config_id == str(queued["config_id"])
    assert store["gate"].closed
    assert store["gate"].refusal == ""  # a rebuild now holds, retried as one

    reached: list[bool] = []

    async def coordinate_once() -> None:
        reached.append(True)
        raise asyncio.CancelledError  # one refresh is enough

    monkeypatch.setattr(provider_coordinator, "coordinate_once", coordinate_once)
    with pytest.raises(asyncio.CancelledError):
        await runtime.refresh_loop()
    assert reached == [True]  # the rebuild can start


@pytest.mark.asyncio
async def test_a_reset_with_nothing_to_rebuild_reopens_model_work(store: dict[str, Any]) -> None:
    _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    assert store["gate"].closed and runtime.refused_configuration()
    reset.rebuild_required.return_value = False
    result = await reset.reset_configuration(ProviderResetInput(expected_version="100"))
    assert result.state == "active" and store["rows"][0]["state"] == "environment"
    await runtime.load_active_configuration()
    assert not store["gate"].closed
    assert runtime.refused_configuration() == ""
    assert store["settings"].embedding_model == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_a_save_needing_a_rebuild_takes_over_a_refusals_hold(store: dict[str, Any]) -> None:
    _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    accepted = _setup("compatible", "https://gateway.example/v1").model_copy(
        update={"expected_version": "100"}
    )
    result = await activation.save_configuration(accepted)
    assert result.state == "queued"
    await runtime.load_active_configuration()
    assert runtime._maintenance_config_id == result.config_id and store["gate"].closed
    assert store["gate"].refusal == ""


@pytest.mark.asyncio
async def test_a_save_off_the_gateway_is_refused_before_it_is_stored(store: dict[str, Any]) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await activation.save_configuration(_setup("compatible", "https://10.0.0.5/v1"))
    assert refused.value.status_code == 422 and store["rows"] == []


async def _reset_without_rebuild(store: dict[str, Any]) -> None:
    reset.rebuild_required.return_value = False
    version = str(store["rows"][0]["version"])
    result = await reset.reset_configuration(ProviderResetInput(expected_version=version))
    assert result.state == "active"
    await runtime.load_active_configuration()
    assert not store["gate"].closed and runtime._maintenance_config_id is None


@pytest.mark.asyncio
async def test_a_save_after_a_reset_that_ended_a_refusals_hold_queues_cleanly(
    store: dict[str, Any],
) -> None:
    _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    await _reset_without_rebuild(store)
    activation.rebuild_required.return_value = True
    version = str(store["rows"][0]["version"])
    accepted = _setup("compatible", "https://gateway.example/v1").model_copy(
        update={"expected_version": version}
    )
    result = await activation.save_configuration(accepted)
    assert result.state == "queued"
    await runtime.load_active_configuration()  # nothing stale to hand over
    assert runtime._maintenance_config_id == result.config_id and store["gate"].closed


@pytest.mark.asyncio
async def test_a_refusal_committed_while_another_transition_holds_then_replaced(
    store: dict[str, Any],
) -> None:
    runtime._hold_admission("m")  # a transition this process is draining for
    refused = _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    assert store["gate"].closed and runtime.refused_configuration()
    # Refused, not asked to retry a rebuild that never ends.
    assert store["gate"].refusal == runtime.refused_configuration()
    assert runtime._maintenance_config_id == refused
    accepted = _setup("compatible", "https://gateway.example/v1").model_copy(
        update={"expected_version": "100"}
    )
    result = await activation.save_configuration(accepted)
    await runtime.load_active_configuration()  # the queued save takes the hold from m
    assert result.state == "queued" and runtime._maintenance_config_id == result.config_id
    assert store["gate"].refusal == ""


@pytest.mark.asyncio
async def test_a_second_refusal_after_the_first_hold_ended_holds_again(
    store: dict[str, Any],
) -> None:
    _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    await _reset_without_rebuild(store)
    second = _commit(store, _setup("compatible", "https://10.0.0.5/v1"), version=10**20)
    await runtime.load_active_configuration()
    assert store["gate"].closed and runtime._maintenance_config_id == second


@pytest.mark.asyncio
async def test_a_restart_with_a_refusal_committed_and_its_replacement_queued(
    store: dict[str, Any],
) -> None:
    refused = _commit(store, _setup("gemini"))
    queued = str(uuid4())
    store["rows"].insert(
        0, {"config_id": queued, "parent_id": refused, "state": "queued", "version": 101}
    )
    await runtime.load_active_configuration()  # a fresh process: nothing held yet
    assert runtime._maintenance_config_id == queued and store["gate"].closed
    assert store["gate"].refusal == ""  # a rebuild in progress, retried as one


@pytest.mark.asyncio
async def test_a_drain_for_a_row_the_load_missed_raises_once_then_the_next_refresh_hands_over(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from server.insights.app import provider_barrier

    monkeypatch.setattr(provider_barrier, "get_model_work_gate", lambda: store["gate"])
    for name in ("flush_registered_buffers", "_server_side_settled"):
        monkeypatch.setattr(provider_barrier, name, AsyncMock(return_value=True))
    monkeypatch.setattr(provider_barrier, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(provider_barrier, "record_participant", AsyncMock())
    _commit(store, _setup("gemini"))
    await runtime.load_active_configuration()
    result = await reset.reset_configuration(ProviderResetInput(expected_version="100"))
    queued = UUID(result.config_id)
    # The same tick's coordinate_once sees the queued row before any load has.
    with pytest.raises(ValueError, match="does not own admission"):
        await provider_barrier.acknowledge_drain(queued)
    await runtime.load_active_configuration()  # the next tick hands the gate over
    assert runtime._maintenance_config_id == str(queued)
    assert await provider_barrier.acknowledge_drain(queued)
