"""The timeouts and retry policies a call site used to hard-code come from their settings
in server/settings/catalog.py: each test changes the setting and sees the call use it."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.settings import Settings, settings
from server.settings._live import coerce


class _Stop(Exception):
    """Ends a stubbed call once it has recorded what it was given."""


def test_the_llm_and_embedding_retry_policies_follow_their_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.embedding import _provider
    from server.llm import _retry

    monkeypatch.setattr(settings, "llm_retry_attempts", 2)
    monkeypatch.setattr(settings, "llm_retry_max_wait_s", 1.5)
    monkeypatch.setattr(settings, "embedding_retry_attempts", 5)
    monkeypatch.setattr(settings, "embedding_retry_max_wait_s", 3.0)
    llm, embed = _retry.retrying(), _provider.retrying()
    assert (llm.stop.max_attempt_number, llm.wait.max) == (2, 1.5)  # type: ignore[attr-defined]
    assert (embed.stop.max_attempt_number, embed.wait.max) == (5, 3.0)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_identity_provider_call_waits_its_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import oidc

    seen: list[Any] = []

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            seen.append(timeout)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *a: Any, **k: Any) -> Any:
            raise _Stop

    async def _discover() -> dict[str, str]:
        return {"token_endpoint": "https://idp.example/token"}

    monkeypatch.setattr(settings, "identity_provider_timeout_s", 2.5)
    monkeypatch.setattr(oidc, "_discover", _discover)
    monkeypatch.setattr(oidc.httpx, "AsyncClient", _Client)
    with pytest.raises(_Stop):
        await oidc._token_request({"grant_type": "authorization_code"})

    class _Jwk:
        def __init__(self, uri: str, *, timeout: float) -> None:
            seen.append(timeout)

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return SimpleNamespace(key="k")

    monkeypatch.setattr(oidc, "PyJWKClient", _Jwk)
    monkeypatch.setattr(oidc, "_jwk_clients", {})
    assert oidc.signing_key("https://idp.example/jwks", "token") == "k"
    assert seen == [2.5, 2.5]


@pytest.mark.asyncio
async def test_a_machine_issuers_discovery_waits_the_identity_provider_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.clients._http as http_client
    from server.auth import machines

    seen: list[float] = []

    class _Http:
        async def get(self, url: str, **kw: Any) -> Any:
            seen.append(kw["timeout"])
            raise _Stop

    monkeypatch.setattr(settings, "identity_provider_timeout_s", 4.0)
    monkeypatch.setattr(http_client, "get_client", lambda: _Http())
    monkeypatch.setattr(machines, "_discovered", {})
    with pytest.raises(_Stop):
        await machines._jwks_uri("https://issuer.example")
    assert seen == [4.0]


@pytest.mark.asyncio
async def test_the_gateway_token_exchange_waits_its_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.clients._http as http_client
    from server.llm import gateway

    seen: list[float] = []

    class _Http:
        async def post(self, url: str, **kw: Any) -> Any:
            seen.append(kw["timeout"])
            raise _Stop

    async def _identity_token() -> str:
        return "jwt"

    monkeypatch.setattr(settings, "inference_token_exchange_timeout_s", 3.0)
    monkeypatch.setattr(settings, "inference_token_url", "https://gateway.example/token")
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(http_client, "get_model_client", lambda: _Http())
    monkeypatch.setattr(gateway, "_identity_token", _identity_token)
    with pytest.raises(_Stop):
        await gateway._renew()
    assert seen == [3.0]


def test_a_langfuse_score_post_waits_its_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import langfuse_scores

    seen: list[float] = []

    def _urlopen(req: Any, *, timeout: float) -> Any:
        seen.append(timeout)
        raise _Stop

    monkeypatch.setattr(settings, "langfuse_timeout_s", 7.0)
    monkeypatch.setattr(settings, "langfuse_url", "https://langfuse.example")
    monkeypatch.setattr(langfuse_scores, "_auth_header", lambda: "Basic x")
    monkeypatch.setattr(langfuse_scores.urllib.request, "urlopen", _urlopen)
    with pytest.raises(_Stop):
        langfuse_scores._post_score({"name": "s", "value": 1})
    assert seen == [7.0]


@pytest.mark.asyncio
async def test_a_hyde_call_slower_than_its_setting_falls_back_to_the_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    import server.llm.client as llm_client
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search import embed

    class _Slow:
        async def complete(self, **k: Any) -> Any:
            await asyncio.sleep(5)

    class _Embedder:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            return [0.1, 0.2]

    monkeypatch.setattr(settings, "memory_search_hyde_timeout_s", 0.05)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Slow())
    work: dict[str, Any] = {"embedding_calls": 0}
    clock = SimpleNamespace(mark=lambda stage: None)
    budget = SimpleNamespace(note_llm_call=lambda n: None)
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="where", hyde=True)
    with caplog.at_level(logging.WARNING):
        vec = await embed._embed_search_query(inp, _Embedder(), "where", "", budget, work, clock)  # type: ignore[arg-type]
    assert vec and "HyDE generation failed" in caplog.text


@pytest.mark.asyncio
async def test_the_shared_http_clients_take_their_timeout_and_pool_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.clients import _http

    monkeypatch.setattr(settings, "http_client_timeout_s", 7.0)
    monkeypatch.setattr(settings, "http_client_connect_timeout_s", 1.5)
    monkeypatch.setattr(settings, "http_client_max_connections", 9)
    monkeypatch.setattr(settings, "http_client_max_keepalive_connections", 3)
    client = _http._build()
    try:
        assert (client.timeout.read, client.timeout.connect) == (7.0, 1.5)
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert (pool._max_connections, pool._max_keepalive_connections) == (9, 3)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("module", "setting"),
    [
        ("server.memory.app.rerank", "memory_search_rerank_timeout_s"),
        ("server.memory.app.classify", "memory_classify_timeout_s"),
    ],
)
def test_a_rerank_or_classify_endpoint_waits_its_setting(
    monkeypatch: pytest.MonkeyPatch, module: str, setting: str
) -> None:
    import importlib

    monkeypatch.setattr(settings, setting, 3.5)
    assert importlib.import_module(module)._timeout().read == 3.5


@pytest.mark.asyncio
async def test_an_end_user_issuers_jwks_fetch_waits_the_identity_provider_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import end_user_keys

    seen: list[Any] = []

    class _Client:
        def __init__(self, **kw: Any) -> None:
            seen.append(kw["timeout"])

        async def __aenter__(self) -> _Client:
            raise _Stop

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(settings, "identity_provider_timeout_s", 4.0)
    monkeypatch.setattr(end_user_keys.httpx, "AsyncClient", _Client)
    await end_user_keys._fetch("issuer", "https://issuer.example/jwks")  # a failed fetch is logged
    assert seen == [4.0]


_NEVER_ZERO = [
    # a work cap of 0 would starve or break its path
    "memory_durability_verifier_max_facts_per_call",
    "memory_profile_cas_max_retries",
    "memory_profile_search_embed_cap",
    "memory_proposals_scan_cap",
    "memory_conflict_sweep_max_rows_per_pass",
    "memory_entity_extract_parse_fail_max",
    "memory_observe_extract_session_context_limit",
    "memory_usage_fold_acted_max_rows_per_pass",
    "memory_usage_fold_max_pending_mutations",
    "code_citations_git_call_cap",
    "code_merge_shas_max_log_commits",
    "code_blame_introduced_max_diff_calls",
    "code_blame_max_graph_commits",
    "namespace_audit_index_row_scan_cap",
    "audit_verify_max_rows_per_pass",
    "delete_version_check_batch_size",
    "model_calls_off_drop_per_pass",
    "http_client_max_connections",
    # an interval or backoff of 0 would spin
    "delete_version_check_interval_seconds",
    "delete_version_check_idle_interval_seconds",
    "memory_code_links_sweep_interval_s",
    "code_bootstrap_backoff_max_s",
    "code_bootstrap_embed_breaker_hold_s",
    "code_bootstrap_mirror_refresh_s",
    "code_bootstrap_mirror_retry_s",
    "code_mirror_warmup_retry_s",
    "code_scip_full_retry_s",
    "storage_frontend_assets_retry_s",
    "storage_frontend_assets_retry_max_s",
    "worker_backoff_base_s",
    "worker_backoff_cap_s",
    "worker_partial_retry_s",
]


@pytest.mark.parametrize("name", _NEVER_ZERO)
def test_a_cap_or_interval_of_zero_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        coerce(Settings, name, 0)


def test_zero_retries_of_an_empty_block_read_still_means_none() -> None:
    assert coerce(Settings, "memory_retrieval_empty_block_retries", 0) == 0
    assert coerce(Settings, "memory_retrieval_empty_block_retry_sleep_s", 0) == 0
