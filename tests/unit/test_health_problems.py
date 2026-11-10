"""Health, doctor and the dashboard share one list of
problems, each with a fix.

`/v1/health` `status` reflects more than ClickHouse (no `status: ok` beside
`fake_llm: true`); `providers_degraded` tells a configured fake from a
failing real provider; `adaptive_thresholds.likely_cause` does not blame a
missing OpenAI key when only the LLM is fake; and every problem names its
fix.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import __version__, boot
from server.observability.problems import (
    HealthState,
    Problem,
    collect_problems,
    derive_status,
)
from server.settings import DEV_JWT_SECRET, Settings


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for var in (
        "LOOM_EMBEDDING_MODEL",
        "LOOM_EMBEDDING_DIM",
        "LOOM_LLM_PROVIDER",
        "LOOM_LLM_MODEL",
        "LOOM_REQUIRE_REAL_PROVIDERS",
        "LOOM_AUTH_REQUIRED",
        "LOOM_JWT_SECRET",
        "LOOM_BACKGROUND_WORKERS",
        "LOOM_DEPLOYMENT",
        "LOOM_IDENTITY_PROVIDER",
        "LOOM_BIND_HOST",
        "LOOM_PUBLIC_BASE_URL",
        "LOOM_ALLOW_INSECURE",
        "LOOM_TRUSTED_PROXIES",
        "LOOM_CORS_ORIGINS",
    ):
        monkeypatch.delenv(var, raising=False)
    # Auth off unless a test says otherwise: bare Settings has auth on with
    # the dev secret, which is its own (tested) problem.
    for k, v in {"LOOM_AUTH_REQUIRED": "false", **env}.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _codes(problems: list[Problem]) -> list[str]:
    return [p.code for p in problems]


def _by_code(problems: list[Problem], code: str) -> Problem:
    return next(p for p in problems if p.code == code)


# --- collect_problems: each condition -> code / severity / fix -------------


def test_stock_dev_stack_has_two_warns_and_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stock.env (fake embedder + fake LLM) must stay
    `status: ok`: both fakes are warns, and each fix names the env var."""
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_EMBEDDING_DIM="1024",
        LOOM_LLM_PROVIDER="fake",
    )
    problems = collect_problems(s, HealthState(env={}))
    assert _codes(problems) == ["fake_embedder", "fake_llm"]
    assert {p.severity for p in problems} == {"warn"}
    assert "LOOM_EMBEDDING_MODEL=" in _by_code(problems, "fake_embedder").fix
    assert "LOOM_LLM_PROVIDER=openai" in _by_code(problems, "fake_llm").fix
    assert derive_status(True, problems) == "ok"


def test_require_real_providers_escalates_fakes_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_REQUIRE_REAL_PROVIDERS="true",
    )
    problems = collect_problems(s, HealthState(env={}))
    assert {p.severity for p in problems} == {"error"}
    assert derive_status(True, problems) == "degraded"


def test_keyed_llm_without_key_is_reported_with_the_key_as_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="openai")
    p = _by_code(collect_problems(s, HealthState(env={})), "llm_key_missing")
    assert p.severity == "warn"
    assert "OPENAI_API_KEY is not set" in p.message
    assert "set OPENAI_API_KEY" in p.fix
    # With the key present the problem is gone.
    assert "llm_key_missing" not in _codes(
        collect_problems(s, HealthState(env={"OPENAI_API_KEY": "sk"}))
    )


def test_keyed_embedder_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch, LOOM_EMBEDDING_MODEL="text-embedding-3-small", LOOM_LLM_PROVIDER="fake"
    )
    p = _by_code(collect_problems(s, HealthState(env={})), "embedder_key_missing")
    assert "OPENAI_API_KEY" in p.message and "set OPENAI_API_KEY" in p.fix
    assert "embedder_key_missing" not in _codes(
        collect_problems(s, HealthState(env={"OPENAI_API_KEY": "sk"}))
    )


def test_gemini_embedder_accepts_either_key(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch, LOOM_EMBEDDING_MODEL="gemini-embedding-001", LOOM_LLM_PROVIDER="fake"
    )
    assert "embedder_key_missing" in _codes(collect_problems(s, HealthState(env={})))
    assert "embedder_key_missing" not in _codes(
        collect_problems(s, HealthState(env={"GOOGLE_API_KEY": "g"}))
    )


def test_a_width_the_coded_tables_do_not_take_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_EMBEDDING_DIM="1024",
        LOOM_LLM_PROVIDER="fake",
    )
    tables = ["categories", "chunks", "entities", "memory_embeddings", "shared"]
    stale = HealthState(
        probed_dim=1024,
        width_mismatches={f"loom.{t}.embedding": (3072, 1024) for t in tables}
        | {"loom.code_chunks_small.embedding": (16, 8)},
    )
    p = _by_code(collect_problems(s, stale), "coded_width_mismatch")
    assert p.severity == "error"
    assert (
        "3072-dim columns written at 1024: loom.categories.embedding, loom.chunks.embedding, "
        "loom.entities.embedding and 2 more; "
        "16-dim columns written at 8: loom.code_chunks_small.embedding" in p.message
    )
    assert "coded_width_mismatch" not in _codes(collect_problems(s, HealthState(probed_dim=1024)))


def test_embedding_dim_mismatch_is_an_error_naming_the_probed_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="text-embedding-3-small",
        LOOM_EMBEDDING_DIM="1024",
        LOOM_LLM_PROVIDER="fake",
    )
    env = {"OPENAI_API_KEY": "sk"}
    p = _by_code(
        collect_problems(s, HealthState(probed_dim=1536, env=env)), "embedding_dim_mismatch"
    )
    assert p.severity == "error" and p.source == "runtime"
    assert "LOOM_EMBEDDING_DIM=1536" in p.fix
    assert "embedding_dim_mismatch" not in _codes(
        collect_problems(s, HealthState(probed_dim=1024, env=env))
    )
    # Unknown (pre-boot) never guesses.
    assert "embedding_dim_mismatch" not in _codes(
        collect_problems(s, HealthState(probed_dim=None, env=env))
    )


def test_provider_runtime_failure_from_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="fake")
    status = {
        "degraded": True,
        "providers": {
            "openai:embeddings": {
                "failing": True,
                "last_error": "429 insufficient_quota",
                "last_error_age_s": 3.0,
            },
            "openai:chat": {"failing": False},
        },
    }
    problems = collect_problems(s, HealthState(provider_status=status, env={}))
    p = _by_code(problems, "provider_failing:openai:embeddings")
    assert p.severity == "error" and p.source == "runtime"
    assert "429 insufficient_quota" in p.message and "3.0s ago" in p.message
    assert "provider_failing:openai:chat" not in _codes(problems)
    assert derive_status(True, problems) == "degraded"


def test_the_key_a_problem_names_is_the_one_the_provider_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key tables are the provider packages' own, so a key a provider
    starts accepting is accepted here without a second edit."""
    from server.core import llm_provider
    from server.embedding import embedder

    monkeypatch.setitem(embedder.KEY_ENVS, "gemini", ("GEMINI_KEY_NEXT", "GEMINI_API_KEY"))
    monkeypatch.setitem(llm_provider.KEY_ENV_BY_PROVIDER, "anthropic", "ANTHROPIC_KEY_NEXT")
    s = _settings(
        monkeypatch, LOOM_EMBEDDING_MODEL="gemini-embedding-001", LOOM_LLM_PROVIDER="anthropic"
    )
    env = {"GEMINI_KEY_NEXT": "g", "ANTHROPIC_KEY_NEXT": "a"}
    codes = _codes(collect_problems(s, HealthState(env=env)))
    assert "embedder_key_missing" not in codes and "llm_key_missing" not in codes
    status = {
        "providers": {
            "gemini:embeddings": {"failing": True, "last_error": "429"},
            "anthropic:messages": {"failing": True, "last_error": "529"},
        }
    }
    problems = collect_problems(s, HealthState(provider_status=status, env=env))
    assert "GEMINI_KEY_NEXT's quota" in _by_code(problems, "provider_failing:gemini:embeddings").fix
    assert (
        "ANTHROPIC_KEY_NEXT's quota"
        in _by_code(problems, "provider_failing:anthropic:messages").fix
    )


def test_dev_jwt_secret_with_auth_on_is_a_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_AUTH_REQUIRED="true",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
    )
    p = _by_code(collect_problems(s, HealthState(env={})), "dev_jwt_secret")
    assert p.severity == "warn"
    assert "LOOM_JWT_SECRET" in p.fix
    # Auth off: the secret is moot, no problem.
    s_off = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_AUTH_REQUIRED="false",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
    )
    assert "dev_jwt_secret" not in _codes(collect_problems(s_off, HealthState(env={})))


def test_a_keyring_key_does_not_hide_the_dev_root_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root secret seals every keyring key, so a dev root secret is the
    one warning even while a keyring key signs."""
    from datetime import datetime

    from server.auth import keyring

    monkeypatch.setattr(
        keyring,
        "_view",
        keyring._View(
            loaded=True, keys={"k1": keyring.SigningKey("k1", "HS256", "s" * 40, "", datetime.min)}
        ),
    )
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_AUTH_REQUIRED="true",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
    )
    codes = _codes(collect_problems(s, HealthState(env={})))
    assert "dev_jwt_secret" in codes and "dev_sealing_secret" not in codes


def test_empty_jwt_secret_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_JWT_SECRET="",
    )
    p = _by_code(collect_problems(s, HealthState(env={})), "jwt_secret_empty")
    assert p.severity == "error" and "Set LOOM_JWT_SECRET" in p.fix


def test_pending_migrations_error_names_loom_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="fake")
    pending = ("1000_a.sql", "1010_b.sql", "1020_c.sql", "1030_d.sql")
    p = _by_code(
        collect_problems(s, HealthState(pending_migrations=pending, env={})), "pending_migrations"
    )
    assert p.severity == "error" and p.source == "schema"
    assert "4 migration(s)" in p.message and "…" in p.message
    assert "loom migrate" in p.fix
    assert "pending_migrations" not in _codes(
        collect_problems(s, HealthState(pending_migrations=(), env={}))
    )


def test_instance_label_missing_only_for_a_server_outside_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="fake")
    p = _by_code(
        collect_problems(s, HealthState(instance_label="", env={})), "instance_label_missing"
    )
    assert p.severity == "warn" and "LOOM_INSTANCE_LABEL=" in p.fix
    assert "instance_label_missing" not in _codes(
        collect_problems(s, HealthState(instance_label="iso:x:7801", env={}))
    )
    # The doctor shell (None) is not a server process.
    assert "instance_label_missing" not in _codes(collect_problems(s, HealthState(env={})))
    dep = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_DEPLOYMENT="1",
    )
    assert "instance_label_missing" not in _codes(
        collect_problems(dep, HealthState(instance_label="", env={}))
    )


def test_worker_lease_absent_is_info_and_explains_off_by_design(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_BACKGROUND_WORKERS="off",
    )
    p = _by_code(
        collect_problems(web, HealthState(worker_lease_held=False, env={})), "workers_lease_absent"
    )
    assert p.severity == "info" and p.source == "workers"
    assert "LOOM_BACKGROUND_WORKERS=off" in p.message
    assert derive_status(True, [p]) == "ok"
    auto = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="fake-ngram-1024",
        LOOM_LLM_PROVIDER="fake",
        LOOM_DEPLOYMENT="1",
    )
    p2 = _by_code(
        collect_problems(auto, HealthState(worker_lease_held=False, env={})), "workers_lease_absent"
    )
    assert "another process" in p2.message
    branch = _settings(
        monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="fake"
    )
    p3 = _by_code(
        collect_problems(branch, HealthState(worker_lease_held=False, env={})),
        "workers_lease_absent",
    )
    assert "outside the deployment" in p3.message
    assert "workers_lease_absent" not in _codes(
        collect_problems(auto, HealthState(worker_lease_held=True, env={}))
    )


def test_problems_are_sorted_worst_first_and_serialise(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, LOOM_EMBEDDING_MODEL="fake-ngram-1024", LOOM_LLM_PROVIDER="fake")
    problems = collect_problems(
        s, HealthState(pending_migrations=("1000_a.sql",), worker_lease_held=False, env={})
    )
    assert [p.severity for p in problems] == ["error", "warn", "warn", "info"]
    d = problems[0].as_dict()
    assert set(d) == {"code", "severity", "message", "fix", "source"}
    json.dumps(d)


def test_every_fix_is_one_imperative_sentence_naming_a_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(
        monkeypatch,
        LOOM_EMBEDDING_MODEL="text-embedding-3-small",
        LOOM_EMBEDDING_DIM="1024",
        LOOM_LLM_PROVIDER="openai",
        LOOM_AUTH_REQUIRED="true",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
        LOOM_BACKGROUND_WORKERS="off",
    )
    state = HealthState(
        probed_dim=1536,
        provider_status={
            "degraded": True,
            "providers": {"openai:chat": {"failing": True, "last_error": "500"}},
        },
        pending_migrations=("1000_a.sql",),
        worker_lease_held=False,
        instance_label="",
        env={},
    )
    problems = collect_problems(s, state)
    assert len(problems) >= 8
    for p in problems:
        assert p.fix[0].isupper() and p.fix.endswith("."), p.fix
        assert re.search(
            r"LOOM_[A-Z_]+|[A-Z]+_API_KEY|`loom [a-z-]+`|loom-<colour>-workers|openssl", p.fix
        ), p.fix


def test_derive_status_ch_down_wins() -> None:
    assert derive_status(False, []) == "degraded"
    assert derive_status(True, []) == "ok"
    assert derive_status(True, [Problem("x", "warn", "m", "Set X.")]) == "ok"
    assert derive_status(True, [Problem("x", "error", "m", "Set X.")]) == "degraded"


# --- /v1/health -------------------------------------------------------------


@pytest.fixture
def health_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import server.main as main_module
    from server.settings import settings

    class _StubCH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"one": 1}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _StubCH())
    # `status` reads the breaker and the boot snapshot;
    # start from a clean process, whatever earlier tests recorded.
    from server.observability import provider_health

    provider_health.reset()
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-1024")
    monkeypatch.setattr(settings, "embedding_dim", 1024)
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "require_real_providers", False)
    monkeypatch.setattr("server.embedding.embedder._probed_dim", None)
    return TestClient(main_module.app)


def test_health_carries_problems_and_stays_ok_on_the_stock_stack(
    health_client: TestClient,
) -> None:
    body = health_client.get("/v1/health").json()
    codes = {p["code"] for p in body["problems"]}
    assert {"fake_embedder", "fake_llm"} <= codes
    errors = [p for p in body["problems"] if p["severity"] == "error"]
    assert body["status"] == "ok", errors
    assert not errors, errors
    for p in body["problems"]:
        assert set(p) == {"code", "severity", "message", "fix", "source"}
    # Every problem field is there.
    for key in (
        "fake_embedding",
        "fake_llm",
        "providers_degraded",
        "providers",
        "embedding_dim_mismatch",
        "llm_provider",
        "llm_model",
    ):
        assert key in body, key
    assert body["fake_llm"] is True and body["fake_embedding"] is True
    assert body["version"] == __version__


def test_health_degraded_under_require_real_providers(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "require_real_providers", True)
    r = health_client.get("/v1/health")
    assert r.status_code == 200  # the HTTP code stays ClickHouse-only
    body = r.json()
    assert body["status"] == "degraded"
    assert {
        p["severity"] for p in body["problems"] if p["code"] in ("fake_embedder", "fake_llm")
    } == {"error"}


def test_health_degraded_on_dim_mismatch(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("server.embedding.embedder._probed_dim", 1536)
    body = health_client.get("/v1/health").json()
    assert body["embedding_dim_mismatch"] is True
    assert body["status"] == "degraded"
    p = next(p for p in body["problems"] if p["code"] == "embedding_dim_mismatch")
    assert "LOOM_EMBEDDING_DIM=1536" in p["fix"]


def test_health_likely_cause_depends_on_the_embedder_only(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake LLM with a real embedder is not blamed as a missing OpenAI key;
    calibration probes the embedder, so only the embedder can be the cause."""
    from server.memory.app import embedder_calibration
    from server.settings import settings

    monkeypatch.setattr(embedder_calibration, "get_calibration", lambda: None)
    body = health_client.get("/v1/health").json()
    assert body["adaptive_thresholds"]["likely_cause"] == "fake embedder"

    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    # The embedder's key is set (without it the fake would be served).
    monkeypatch.setattr("server.embedding.embedder.provider_key", lambda p: "key")
    body = health_client.get("/v1/health").json()
    assert body["fake_embedding"] is False
    assert body["fake_llm"] is True
    assert body["adaptive_thresholds"]["likely_cause"] == "probe error — check boot logs"


def test_a_keyed_embedder_without_its_key_is_a_degraded_fake(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process serves the fake n-gram embedder then, and health says so."""
    from server.settings import settings

    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr("server.embedding.embedder.provider_key", lambda p: "")
    monkeypatch.setattr("server.embedding.embedder.gateway.keyless", lambda: False)
    body = health_client.get("/v1/health").json()
    assert body["fake_embedding"] is True and body["providers_degraded"] is True


# --- loom doctor -----------------------------------------------------------


def test_doctor_prints_the_same_problem_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli.doctor import _cmd_doctor
    from server.settings import settings
    from tests.unit.test_cli_doctor import _passing_httpx_client

    monkeypatch.setattr("httpx.Client", _passing_httpx_client)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-1024")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "require_real_providers", False)
    rc = _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "[WARN]  problem:fake_llm" in out
        and "Fix: Choose an LLM in Settings → Models & keys" in out
    )
    assert "[WARN]  problem:fake_embedder" in out
    # Same list as /v1/health: the doctor's codes are the collector's codes.
    expected = {p.code for p in collect_problems(settings, HealthState())}
    printed = set(re.findall(r"\[(?:PASS|WARN|FAIL)\]  problem:(\S+)", out))
    assert printed == expected


def test_doctor_fails_on_an_error_problem(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli.doctor import _cmd_doctor
    from server.settings import settings
    from tests.unit.test_cli_doctor import _passing_httpx_client

    monkeypatch.setattr("httpx.Client", _passing_httpx_client)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "require_real_providers", True)
    rc = _cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL]  problem:fake_llm" in out


def test_doctor_json_emits_checks_and_problems(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli.doctor import _cmd_doctor
    from server.settings import settings
    from tests.unit.test_cli_doctor import _passing_httpx_client

    monkeypatch.setattr("httpx.Client", _passing_httpx_client)
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "require_real_providers", False)
    rc = _cmd_doctor(argparse.Namespace(json=True))
    body = json.loads(capsys.readouterr().out)
    assert rc == 0 and body["ok"] is True
    assert {c["name"] for c in body["checks"]} >= {
        "python_version",
        "settings_load",
        "problem:fake_llm",
    }
    fake_llm = next(p for p in body["problems"] if p["code"] == "fake_llm")
    assert fake_llm["severity"] == "warn" and "LOOM_LLM_PROVIDER=openai" in fake_llm["fix"]


def test_doctor_cli_accepts_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.cli as cli
    from server.cli import app

    seen: dict[str, bool] = {}

    def fake_doctor(args: argparse.Namespace) -> int:
        seen["json"] = bool(args.json)
        return 0

    monkeypatch.setattr(app, "_cmd_doctor", fake_doctor)
    assert cli.main(["doctor", "--json"]) == 0
    assert seen == {"json": True}


# --- exposure: a non-loopback bind, forwarded headers, CORS ----------------

_EXPOSURE = {
    "public_base_url_missing",
    "insecure_bind_auth_off",
    "insecure_bind_dev_secret",
    "trusted_proxies_any",
    "cors_wildcard",
}


def test_a_loopback_bind_reports_no_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, LOOM_JWT_SECRET=DEV_JWT_SECRET)
    assert not _EXPOSURE & set(_codes(collect_problems(s, HealthState(env={}))))
    # MCP open mode and a short secret are a reachable server's problems only
    s = _settings(monkeypatch, LOOM_MCP_AUTH_MODE="open", LOOM_JWT_SECRET="a" * 16)
    codes = set(_codes(collect_problems(s, HealthState(env={}))))
    assert not codes & {"insecure_bind_mcp_open", "insecure_bind_weak_key"}


def test_a_reachable_bind_without_safeguards_reports_each(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        LOOM_BIND_HOST="0.0.0.0",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
        LOOM_TRUSTED_PROXIES="*",
        LOOM_CORS_ORIGINS="*",
    )
    problems = collect_problems(s, HealthState(env={}))
    assert _EXPOSURE <= set(_codes(problems))
    assert _by_code(problems, "insecure_bind_auth_off").severity == "error"
    assert _by_code(problems, "trusted_proxies_any").severity == "warn"


def test_allow_insecure_downgrades_the_bind_problems_to_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(
        monkeypatch,
        LOOM_BIND_HOST="0.0.0.0",
        LOOM_JWT_SECRET=DEV_JWT_SECRET,
        LOOM_PUBLIC_BASE_URL="https://loom.example",
        LOOM_ALLOW_INSECURE="1",
    )
    problems = collect_problems(s, HealthState(env={}))
    assert "public_base_url_missing" not in _codes(problems)
    assert _by_code(problems, "insecure_bind_auth_off").severity == "warn"
    assert _by_code(problems, "insecure_bind_dev_secret").severity == "warn"


def test_participants_a_reconfiguration_would_wait_on_are_a_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(monkeypatch)
    assert "provider_participants_stale" not in _codes(collect_problems(s, HealthState()))
    assert "provider_participants_stale" not in _codes(
        collect_problems(s, HealthState(stale_participants=()))
    )
    stale = ({"participant_id": "p-1", "reason": "queries_running", "heartbeat_at": "x"},)
    found = _by_code(
        collect_problems(s, HealthState(stale_participants=stale)), "provider_participants_stale"
    )
    assert found.severity == "warn" and "p-1 (queries_running)" in found.message
    assert "loom participant retire" in found.fix


def test_a_full_sign_in_table_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability.problems import HealthState, _oauth_claim_problems

    on = _settings(monkeypatch, LOOM_OAUTH_ENABLED="true")
    assert _oauth_claim_problems(on, HealthState()) == []
    off = _settings(monkeypatch, LOOM_OAUTH_ENABLED="false", LOOM_AUTH_REQUIRED="false")
    assert _oauth_claim_problems(off, HealthState(oauth_signin_state_full=True)) == []
    (full,) = _oauth_claim_problems(on, HealthState(oauth_signin_state_full=True))
    assert full.code == "oauth_signin_state_full"


def test_an_assistant_still_being_set_up_raises_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting, or set up again after LibreChat refused the stored key, ends on its own:
    only a failed setup try is a problem."""
    from server.assistant.app import service

    s = _settings(monkeypatch)
    assert "assistant_service_failing" not in _codes(collect_problems(s, HealthState(env={})))
    # The state a key LibreChat no longer takes leaves behind.
    monkeypatch.setattr(service, "_ready", True)
    monkeypatch.setattr(service, "_problem", "")
    monkeypatch.setattr(service, "_answering", ("agent_1", "sk-old"))
    monkeypatch.setattr(service, "_loop", None)
    monkeypatch.setattr(service, "start_setup_loop", lambda: None)
    service.forget_answering()
    ok, error = service.assistant_state()
    again = HealthState(env={}, assistant_ok=ok, assistant_error=error)
    assert "assistant_service_failing" not in _codes(collect_problems(s, again))
    failed = HealthState(env={}, assistant_ok=False, assistant_error="connection refused")
    assert "assistant_service_failing" in _codes(collect_problems(s, failed))
