"""The deployment's published configuration: what it carries, how each process
compares itself with it (drift), and when the leader rewrites it."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.core import deployment_config as dc
from server.core import instances
from server.settings import Settings, settings


def _settings(**env: object) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


class _Ch:
    """A ClickHouse double holding the one `settings` row."""

    def __init__(self, stored: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.stored = stored
        self.fail = fail
        self.inserted: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if self.fail:
            raise ConnectionError("ClickHouse down")
        if self.stored is None:
            return None
        return {"value": json.dumps(self.stored), "published_by": "w", "published_at": "t"}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.deployment_config"
        self.inserted.extend(rows)
        self.stored = json.loads(str(rows[0]["value"]))


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment", True)
    monkeypatch.setitem(dc.STATE, "drift", [])
    monkeypatch.setitem(dc.STATE, "checked", False)
    monkeypatch.setitem(dc.STATE, "error", "")


# ---- snapshot -------------------------------------------------------------


def test_a_snapshot_is_the_key_fingerprint_and_what_must_match() -> None:
    s = _settings(
        code_repos_json="[]",
        instance_heartbeat_seconds=7,
        embedding_model="text-embedding-3-large",
    )
    snap = dc.snapshot(s)
    assert set(snap) == {dc.KEYS_ENTRY, *dc.MUST_MATCH}
    assert set(snap[dc.KEYS_ENTRY]) == {"root"}
    assert snap["embedding_model"] == "text-embedding-3-large"


def test_fingerprints_name_the_snapshot_they_were_made_from() -> None:
    s = _settings(embedding_model="text-embedding-3-large", embedding_dim=3072)
    fp = dc.fingerprints(s)
    assert fp["config_fp"] == dc._digest(json.loads(json.dumps(dc.snapshot(s), sort_keys=True)))
    other = dc.fingerprints(_settings(embedding_model="fake-ngram-3072"))
    assert other["settings_fp"] != fp["settings_fp"]
    assert other["keys_fp"] == fp["keys_fp"]
    assert dc.fingerprints(_settings(jwt_secret="t" * 40))["keys_fp"] != fp["keys_fp"]


# ---- drift ----------------------------------------------------------------


def test_compare_names_a_different_secret_and_a_different_embedding_space() -> None:
    published = dc.snapshot(
        _settings(jwt_secret="s" * 40, embedding_model="text-embedding-3-large")
    )
    assert "s" * 40 not in json.dumps(published)
    same = _settings(jwt_secret="s" * 40, embedding_model="text-embedding-3-large")
    assert dc.compare(same, published) == []
    other = _settings(jwt_secret="t" * 40, embedding_model="fake-ngram-3072")
    problems = dc.compare(other, published)
    assert len(problems) == 3
    assert any("LOOM_JWT_SECRET" in p for p in problems)
    assert any("LOOM_EMBEDDING_MODEL" in p for p in problems)
    assert any("LOOM_EMBEDDING_PROVIDER='fake'" in p for p in problems)  # as the model implies


@pytest.mark.usefixtures("deployment")
def test_a_row_without_the_root_fingerprint_is_drift_and_the_leader_keeps_it() -> None:
    """It proves nothing about the root secret: no process is ready against it,
    and only `loom deployment publish` replaces it."""
    stored = {k: v for k, v in dc.snapshot(settings).items() if k != dc.KEYS_ENTRY}
    ch = _Ch(stored)
    asyncio.run(dc.check(ch))
    assert any("root-secret fingerprint" in p for p in dc.drift())
    assert not dc.readiness_check().ok
    assert not asyncio.run(dc.publish(ch, live=[]))
    assert ch.inserted == []


@pytest.mark.usefixtures("deployment")
def test_drift_follows_the_published_snapshot() -> None:
    theirs = dc.snapshot(_settings(jwt_secret="x" * 40))
    asyncio.run(dc.check(_Ch(theirs)))
    assert dc.drift() and "LOOM_JWT_SECRET" in dc.drift()[0]
    asyncio.run(dc.check(_Ch(dc.snapshot(settings))))
    assert dc.drift() == []


@pytest.mark.usefixtures("deployment")
def test_an_unreadable_snapshot_keeps_the_last_verdict() -> None:
    asyncio.run(dc.check(_Ch(dc.snapshot(_settings(jwt_secret="x" * 40)))))
    before = dc.drift()
    asyncio.run(dc.check(_Ch(fail=True)))
    assert dc.drift() == before and before
    assert dc.STATE["error"] == "ConnectionError"


@pytest.mark.usefixtures("deployment")
def test_not_ready_until_the_first_read_of_the_snapshot_succeeds() -> None:
    asyncio.run(dc.check(_Ch(fail=True)))
    got = dc.readiness_check()
    assert not got.ok and got.reason.endswith(": ConnectionError")
    assert "ClickHouse down" not in got.reason
    asyncio.run(dc.check(_Ch()))  # nothing published yet: nothing to drift from
    assert dc.readiness_check().ok


def test_a_process_outside_the_deployment_has_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment", False)
    monkeypatch.setitem(dc.STATE, "drift", ["stale"])
    asyncio.run(dc.check(_Ch(dc.snapshot(_settings(jwt_secret="x" * 40)))))
    assert dc.drift() == []


def test_drift_is_a_health_problem() -> None:
    from server.observability.problems import HealthState, collect_problems

    problems = collect_problems(settings, HealthState(config_drift=("keys differ",)))
    [p] = [p for p in problems if p.code == "config_drift"]
    assert p.severity == "error" and "keys differ" in p.message
    assert not [p for p in collect_problems(settings, HealthState()) if p.code == "config_drift"]


# ---- publish --------------------------------------------------------------


def _live(config_fp: str, instance_id: str = "other") -> dict[str, Any]:
    return {"instance_id": instance_id, "config_fp": config_fp}


@pytest.mark.usefixtures("deployment")
def test_the_leader_publishes_when_nothing_is_published() -> None:
    ch = _Ch()
    assert asyncio.run(dc.publish(ch, live=[]))
    assert ch.stored == dc.snapshot(settings)
    assert ch.inserted[0]["name"] == "settings"


@pytest.mark.usefixtures("deployment")
def test_an_empty_row_is_nothing_published() -> None:
    ch = _Ch({})
    asyncio.run(dc.check(ch))
    assert dc.drift() == []
    assert asyncio.run(dc.publish(ch, live=[]))
    assert ch.stored == dc.snapshot(settings)


@pytest.mark.usefixtures("deployment")
def test_the_same_snapshot_is_not_rewritten() -> None:
    ch = _Ch(dc.snapshot(settings))
    assert not asyncio.run(dc.publish(ch, live=[]))
    assert ch.inserted == []


@pytest.mark.usefixtures("deployment")
def test_two_snapshot_shapes_never_flap_the_snapshot() -> None:
    """Two builds' leaders in turn, publishing different snapshot shapes, while
    both run: the stored snapshot is the first one's until no live instance
    runs it."""
    theirs = {**dc.snapshot(settings), "note": "another build's snapshot"}
    ch = _Ch(theirs)
    live = [_live(dc._digest(theirs))]
    assert not asyncio.run(dc.publish(ch, live=live))
    assert ch.stored == theirs
    # This process's own row running it does not hold the snapshot.
    assert asyncio.run(dc.publish(ch, live=[_live(dc._digest(theirs), instances.instance_id())]))
    assert ch.stored == dc.snapshot(settings)


@pytest.mark.usefixtures("deployment")
def test_the_leader_adopts_its_env_once_the_old_one_is_gone() -> None:
    theirs = {**dc.snapshot(settings), "note": "another build's snapshot"}
    ch = _Ch(theirs)
    mine = dc.fingerprints(settings)["config_fp"]
    assert asyncio.run(dc.publish(ch, live=[_live(mine)]))
    assert ch.stored == dc.snapshot(settings)


@pytest.mark.usefixtures("deployment")
def test_a_drifted_leader_never_publishes_over_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G publishes; B, with another root secret, wins the lease while G restarts
    (no live row runs the stored snapshot): B keeps G's snapshot, so G comes back
    ready and B stays drifted."""
    good = _settings(deployment=True, jwt_secret="g" * 40)
    bad = _settings(deployment=True, jwt_secret="b" * 40)
    ch = _Ch()
    monkeypatch.setattr(dc, "settings", good)
    assert asyncio.run(dc.publish(ch, live=[]))
    monkeypatch.setattr(dc, "settings", bad)
    assert not asyncio.run(dc.publish(ch, live=[]))
    assert dc.compare(good, ch.stored) == []
    assert dc.compare(bad, ch.stored)


@pytest.mark.usefixtures("deployment")
def test_a_new_embedding_space_takes_the_operator() -> None:
    theirs = dc.snapshot(_settings(embedding_model="text-embedding-3-large"))
    ch = _Ch(theirs)
    assert not asyncio.run(dc.publish(ch, live=[]))
    assert ch.stored == theirs


@pytest.mark.usefixtures("deployment")
def test_loom_deployment_publish_names_what_changes_and_wants_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import deployment as cli

    theirs = dc.snapshot(_settings(embedding_model="text-embedding-3-large"))
    ch = _Ch(theirs)
    monkeypatch.setattr(cli, "run_connected", lambda run: asyncio.run(run(ch)))
    assert cli._cmd_publish(argparse.Namespace(yes=False, check=False)) == 2
    assert ch.stored == theirs
    assert "LOOM_EMBEDDING_MODEL" in capsys.readouterr().out
    assert cli._cmd_publish(argparse.Namespace(yes=True, check=False)) == 0
    assert ch.stored == dc.snapshot(settings)
    assert ch.inserted[-1]["published_by"].startswith("cli:")


@pytest.mark.usefixtures("deployment")
def test_loom_deployment_publish_fails_when_the_write_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import deployment as cli

    class _Refused(_Ch):
        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            raise ConnectionError("insert refused")

    monkeypatch.setattr(cli, "run_connected", lambda run: asyncio.run(run(_Refused())))
    assert cli._cmd_publish(argparse.Namespace(yes=True, check=False)) == 1
    assert "insert refused" in capsys.readouterr().err
    ch = _Ch(dc.snapshot(settings))
    monkeypatch.setattr(cli, "run_connected", lambda run: asyncio.run(run(ch)))
    assert cli._cmd_publish(argparse.Namespace(yes=True, check=False)) == 0
    assert "already published" in capsys.readouterr().out and ch.inserted == []


def test_a_branch_server_never_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server import boot

    monkeypatch.setattr(settings, "deployment", False)
    spawned: list[str] = []
    monkeypatch.setattr(boot, "_spawn_boot_task", lambda coro, name: spawned.append(name))
    boot.publish_deployment_config(object())
    assert spawned == []
    ch = _Ch()
    assert not asyncio.run(dc.publish(ch, live=[]))
    assert ch.inserted == []


def test_one_label_and_width_on_two_endpoints_are_two_spaces() -> None:
    model = {"embedding_model": "text-embedding-3-large", "embedding_dim": 3072}
    published = dc.snapshot(_settings(embedding_base_url="https://one.example/v1", **model))
    same = _settings(embedding_base_url="https://one.example/v1/", **model)
    assert dc.compare(same, published) == []  # a trailing slash is the same endpoint
    other = _settings(embedding_base_url="https://two.example/v1", **model)
    assert any("LOOM_EMBEDDING_BASE_URL" in p for p in dc.compare(other, published))


@pytest.mark.usefixtures("deployment")
def test_a_dashboard_model_switch_is_no_drift_and_a_fresh_replica_starts_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed provider transition replaces the live embedding settings on
    every replica alike; the published snapshot stays the env's. A process
    whose env differs is still drift."""
    from server.insights.app import provider_runtime

    env = {"embedding_provider": "", "embedding_model": "fake-ngram-3072", "embedding_dim": 3072}
    for name, value in env.items():
        monkeypatch.setattr(settings, name, value)
    published = dc.snapshot(settings)
    for replica in ("the leader", "a replica started after the switch"):
        monkeypatch.setattr(provider_runtime, "_environment_settings", None)
        provider_runtime.environment_settings()  # captured before any dashboard apply
        monkeypatch.setattr(settings, "embedding_provider", "compatible")
        monkeypatch.setattr(settings, "embedding_model", "dash-embed")
        monkeypatch.setattr(settings, "embedding_dim", 8)
        asyncio.run(dc.check(_Ch(published)))
        assert dc.drift() == [], replica
        assert dc.snapshot(settings) == published  # the leader republishes nothing
        for name, value in env.items():
            monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(provider_runtime, "_environment_settings", None)
    monkeypatch.setattr(settings, "embedding_dim", 1536)  # this replica's env differs
    asyncio.run(dc.check(_Ch(published)))
    assert any("LOOM_EMBEDDING_DIM" in p for p in dc.drift())


def test_the_embedding_endpoint_is_published_and_compared_without_credentials() -> None:
    url = "https://u:secret@emb.example.com:8443/v1/?key=tok"
    s = settings.model_copy(update={"embedding_base_url": url})
    snap = dc.snapshot(s)
    assert snap["embedding_base_url"] == "https://emb.example.com:8443/v1"
    assert "u:secret" not in json.dumps(snap) and "key=tok" not in json.dumps(snap)
    # A credential rotated inside the URL is the same space.
    rotated = settings.model_copy(update={"embedding_base_url": url.replace("secret", "new")})
    assert dc.compare(rotated, snap) == []
    other = settings.model_copy(update={"embedding_base_url": "https://u:secret@other.example/v1"})
    (problem,) = dc.compare(other, snap)
    assert "u:secret" not in problem


def test_a_published_row_is_judged_as_this_process_normalises_it() -> None:
    """A row holding the env's values as set (an empty provider, a trailing
    slash, userinfo) is the same space as that env."""
    s = settings.model_copy(
        update={"embedding_provider": "", "embedding_base_url": "https://u:p@emb.example.com/v1/"}
    )
    stored = {
        dc.KEYS_ENTRY: dc.key_fingerprints(s),
        "embedding_provider": "",
        "embedding_model": s.embedding_model,
        "embedding_dim": s.embedding_dim,
        "embedding_base_url": "https://emb.example.com/v1/",
    }
    assert dc.compare(s, stored) == []


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://h:99999/v1", "https://h:99999/v1"),  # a typo'd port is a value, not a crash
        ("http://u:p@[::1]:8080/v1/", "http://[::1]:8080/v1"),
        (
            "https://gw/v1?api-version=2024-02&api-key=s3cr3t&deployment=a",
            "https://gw/v1?api-version=2024-02&deployment=a",
        ),
        ("https://g/v1?apikey=S1&accessToken=S2&auth=S3&x=1", "https://g/v1"),
        ("HTTPS://Emb.Example.com/v1", "https://emb.example.com/v1"),
    ],
)
def test_the_public_endpoint_keeps_what_names_the_space(url: str, public: str) -> None:
    from server.core import embedding_space

    s = settings.model_copy(update={"embedding_base_url": url})
    assert dc.snapshot(s)["embedding_base_url"] == public
    assert embedding_space.public_identity(s.model_dump())[3] == public


@pytest.mark.usefixtures("deployment")
def test_loom_deployment_publish_check_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check is the pre-rollback gate: exit 2 on drift, 0 on a match, and
    never a write, so a rolled-back env cannot become the published one."""
    import argparse

    from server.cli import deployment as cli

    theirs = dc.snapshot(_settings(embedding_model="text-embedding-3-large"))
    ch = _Ch(theirs)
    monkeypatch.setattr(cli, "run_connected", lambda run: asyncio.run(run(ch)))
    assert cli._cmd_publish(argparse.Namespace(yes=True, check=True)) == 2
    assert "LOOM_EMBEDDING_MODEL" in capsys.readouterr().out and ch.inserted == []
    ch = _Ch(dc.snapshot(settings))
    monkeypatch.setattr(cli, "run_connected", lambda run: asyncio.run(run(ch)))
    assert cli._cmd_publish(argparse.Namespace(yes=False, check=True)) == 0
    assert ch.inserted == []
