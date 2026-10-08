"""A read-replica: its settings rules, the ops it sends to the main Loom, and
the settings it takes from the main Loom's published snapshot."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from server.core import deployment_config as dc
from server.core import read_replica
from server.operations.registry import OPERATIONS_BY_NAME as OPS
from server.settings import Settings, settings


def _settings(**env: object) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


@pytest.fixture
def replica(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "read_replica", True)


# ---- settings ------------------------------------------------------------


def test_a_read_replica_pins_what_it_implies() -> None:
    s = _settings(read_replica=True)
    assert s.boot_migrate is False
    assert s.background_workers == "off"


def test_the_main_loom_is_untouched() -> None:
    s = _settings()
    assert s.read_replica is False
    assert s.boot_migrate is True
    assert s.background_workers == "auto"


@pytest.mark.parametrize(
    "env", [{"boot_migrate": True}, {"background_workers": "on"}, {"background_workers": "auto"}]
)
def test_a_read_replica_refuses_settings_it_cannot_honour(env: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="LOOM_READ_REPLICA"):
        _settings(read_replica=True, **env)


def test_a_read_replica_keeps_sign_in_and_signup_settings() -> None:
    s = _settings(read_replica=True, self_signup="open", oauth_enabled=True)
    assert s.self_signup == "open"
    assert s.oauth_enabled is True


def test_the_refresh_interval_has_a_floor() -> None:
    with pytest.raises(ValidationError):
        _settings(read_replica_config_refresh_seconds=1)
    s = _settings(read_replica_config_refresh_seconds=30)
    assert s.read_replica_config_refresh_seconds == 30


# ---- ops -----------------------------------------------------------------


def test_every_host_work_op_exists() -> None:
    assert read_replica.HOST_WORK_OPS <= set(OPS)


def test_the_main_loom_refuses_nothing() -> None:
    assert all(read_replica.op_refusal(name) is None for name in OPS)


@pytest.mark.usefixtures("replica")
def test_a_read_replica_serves_every_op_but_host_work() -> None:
    refused = {name for name in OPS if read_replica.op_refusal(name) is not None}
    assert refused == read_replica.HOST_WORK_OPS
    # Writes are served: they land in the shared service for the main Loom's workers.
    for name in ("memory.set", "memory.delete", "run.complete", "learnings.review"):
        assert not OPS[name].read_only, name
        assert read_replica.op_refusal(name) is None, name
    assert "main Loom" in (read_replica.op_refusal("code.refresh") or "")


@pytest.mark.usefixtures("replica")
def test_the_gate_refuses_host_work_before_auth() -> None:
    from server.transport.gates import Target, authorize

    with pytest.raises(read_replica.ReplicaRefusal, match="read-replica"):
        asyncio.run(
            authorize(
                OPS["code.refresh"],
                None,
                Target("acme", "code-widgets", "code-widgets", None),
                transport="rest",
            )
        )
    asyncio.run(
        authorize(OPS["memory.set"], None, Target("acme", "ci", "ci", None), transport="rest")
    )


# ---- deployment config ---------------------------------------------------


def test_process_and_secret_settings_are_not_published() -> None:
    for name in ("ch_password", "ch_host", "jwt_secret", "background_workers", "blob_dir"):
        assert not dc.publishable(name), name
    for name in ("read_replica", "read_replica_config_refresh_seconds", "code_github_token"):
        assert not dc.publishable(name), name
    for name in ("code_repos_json", "embedding_model", "tracker_code_namespace_bindings_json"):
        assert dc.publishable(name), name


def test_a_snapshot_strips_credentials() -> None:
    repos = [
        {
            "org": "acme",
            "namespace": "code-widgets",
            "repo": "acme/widgets",
            "github_token": "ghp_secret",
            "git_remote": "https://x-access-token:ghp_secret@github.com/acme/widgets.git",
        }
    ]
    s = _settings(code_repos_json=json.dumps(repos), embedding_model="text-embedding-3-large")
    snap = dc.snapshot(s)
    assert "ghp_secret" not in json.dumps(snap)
    entry = json.loads(snap["code_repos_json"])[0]
    assert entry["git_remote"] == "https://github.com/acme/widgets.git"
    assert snap["embedding_model"] == "text-embedding-3-large"
    assert set(snap[dc.KEYS_ENTRY]) == {"signing", "active_kid", "sealing"}
    assert "ch_host" not in snap


def test_apply_fills_only_what_the_replicas_env_leaves_unset() -> None:
    rep = _settings(read_replica=True, code_org="mine")
    published = {"code_org": "acme", "code_namespace": "code-widgets", "ch_host": "evil"}
    changed = dc.apply(rep, published, frozenset(rep.model_fields_set))
    assert changed == ["code_namespace"]
    assert rep.code_org == "mine"
    assert rep.code_namespace == "code-widgets"
    assert rep.ch_host != "evil"


def test_apply_refuses_a_different_embedding_space() -> None:
    rep = _settings(read_replica=True, embedding_model="fake-ngram-3072")
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        dc.apply(
            rep, {"embedding_model": "text-embedding-3-large"}, frozenset(rep.model_fields_set)
        )


def test_token_keys_must_match_the_main_looms() -> None:
    main = _settings(jwt_secret="s" * 40)
    same = _settings(read_replica=True, jwt_secret="s" * 40)
    other = _settings(read_replica=True, jwt_secret="t" * 40)
    published = dc.snapshot(main)
    assert "s" * 40 not in json.dumps(published)
    dc.check_keys(same, published)
    with pytest.raises(ValueError, match="token keys do not match the main Loom"):
        dc.check_keys(other, published)
    dc.check_keys(other, {})  # a snapshot without fingerprints is not checked


def test_publish_writes_one_settings_row() -> None:
    class _Ch:
        rows: list[tuple[str, list[dict[str, object]]]] = []

        async def insert(self, table: str, rows: list[dict[str, object]]) -> None:
            self.rows.append((table, rows))

    ch = _Ch()
    asyncio.run(dc.publish(ch, published_by="deploy:workers"))
    [(table, rows)] = ch.rows
    assert table == "loom.deployment_config"
    assert rows[0]["name"] == "settings"
    assert rows[0]["published_by"] == "deploy:workers"
    assert dc.KEYS_ENTRY in json.loads(str(rows[0]["value"]))


@pytest.mark.usefixtures("replica")
def test_a_read_replica_never_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server import boot

    monkeypatch.setattr(settings, "deployment", True)
    spawned: list[str] = []
    monkeypatch.setattr(boot, "_spawn_boot_task", lambda coro, name: spawned.append(name))
    boot.publish_deployment_config(object())
    assert spawned == []


# ---- schema --------------------------------------------------------------


@pytest.mark.usefixtures("replica")
def test_a_read_replica_does_not_migrate() -> None:
    from server.core.schema.migrate import main

    assert main([]) == 2


@pytest.mark.usefixtures("replica")
def test_a_read_replica_ahead_of_the_shared_schema_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import boot

    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ("099_next.sql",))
    with pytest.raises(RuntimeError, match="upgrade the main Loom first"):
        boot.require_replica_schema()
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    boot.require_replica_schema()


def test_retire_asks_for_confirmation_and_a_participant_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from server.cli.read_replica import _cmd_retire

    assert _cmd_retire(argparse.Namespace(participant="not-a-uuid", yes=True)) == 2
    assert (
        _cmd_retire(
            argparse.Namespace(participant="00000000-0000-0000-0000-000000000001", yes=False)
        )
        == 2
    )
    assert "--yes" in capsys.readouterr().err


@pytest.mark.usefixtures("replica")
def test_a_replica_refusal_says_where_to_send_the_op_not_how_to_get_access() -> None:
    from server.transport.mcp_call import _forbidden_result

    with pytest.raises(read_replica.ReplicaRefusal) as caught:
        read_replica.refuse_host_work("code.refresh")
    result = _forbidden_result(OPS["code.refresh"], caught.value, None, "acme", "code-widgets")
    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["reason"] == read_replica.REASON
    assert body["retry"] is False
    assert "main Loom" in body["error"]
    assert "request access" not in body["error"]
    read_replica.refuse_host_work("memory.set")
