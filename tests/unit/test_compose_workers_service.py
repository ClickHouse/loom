"""The cloud compose runs the background workers in their own
container. The web service is a pure web tier (`LOOM_BACKGROUND_WORKERS=off`),
the worker service runs one process with the supervisor forced on and no
published port, both come from one image and share the flock volume, and
every compose deployment says so with `LOOM_DEPLOYMENT=1`. Parses the files
as YAML (merge keys included) — no docker needed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLOUD = ROOT / "dev" / "docker-compose.cloud.yml"
COLOUR = ROOT / "dev" / "docker-compose.colour.yml"
ALL_COMPOSE = (
    CLOUD,
    COLOUR,
    ROOT / "dev" / "docker-compose.dev.yml",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env(svc: dict) -> dict[str, str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def test_cloud_compose_has_a_web_tier_and_a_worker_service() -> None:
    d = _load(CLOUD)
    assert set(d["services"]) == {"loom", "loom-workers"}
    web, workers = d["services"]["loom"], d["services"]["loom-workers"]
    assert web["container_name"] == "loom-server"
    assert workers["container_name"] == "loom-workers"
    # One image: both services build the same Dockerfile from the same context.
    assert web["build"] == workers["build"]
    assert web["build"]["dockerfile"] == "Dockerfile"


def test_web_tier_runs_no_workers_and_the_worker_service_runs_only_them() -> None:
    d = _load(CLOUD)
    web, workers = _env(d["services"]["loom"]), _env(d["services"]["loom-workers"])
    assert web["LOOM_BACKGROUND_WORKERS"] == "off"
    assert workers["LOOM_BACKGROUND_WORKERS"] == "on"
    assert workers["LOOM_WEB_WORKERS"] == "1"
    # The operator's count from .env, else one process.
    assert web["LOOM_WEB_WORKERS"] == "${LOOM_WEB_WORKERS:-1}"


def test_the_read_replica_keeps_one_process_unless_env_names_a_count() -> None:
    d = _load(ROOT / "dev" / "docker-compose.read-replica.yml")
    (replica,) = d["services"].values()
    assert _env(replica)["LOOM_WEB_WORKERS"] == "${LOOM_WEB_WORKERS:-1}"


def test_worker_service_publishes_no_port_and_healthchecks_its_own() -> None:
    d = _load(CLOUD)
    web, workers = d["services"]["loom"], d["services"]["loom-workers"]
    assert "ports" not in workers
    # Published on loopback unless the operator says otherwise.
    assert web["ports"] == ["${LOOM_BIND_HOST:-127.0.0.1}:7777:7777"]
    wenv = _env(workers)
    assert wenv["PORT"] == "7778"
    assert "127.0.0.1:7778/v1/health" in workers["healthcheck"]["test"][-1]
    assert "127.0.0.1:7777/v1/health" in web["healthcheck"]["test"][-1]


@pytest.mark.parametrize(("path", "service"), [(CLOUD, "loom-workers"), (COLOUR, "workers")])
def test_a_workers_container_listens_where_it_says_it_binds(path: Path, service: str) -> None:
    # LOOM_BIND_HOST=127.0.0.1 skips the reachable-server checks, so the
    # process must not listen on the Docker network either.
    workers = _load(path)["services"][service]
    assert _env(workers)["LOOM_BIND_HOST"] == "127.0.0.1"
    assert "--host 127.0.0.1 " in workers["command"][-1]


@pytest.mark.parametrize(("path", "service"), [(CLOUD, "loom-workers"), (COLOUR, "workers")])
def test_a_workers_container_runs_the_stdlib_loop(path: Path, service: str) -> None:
    # uvloop forks the whole process for each git or indexer spawn
    workers = _load(path)["services"][service]
    assert "--loop $${LOOM_UVICORN_LOOP:-asyncio} " in workers["command"][-1]


def test_both_services_share_the_flock_volume_and_the_data_mounts() -> None:
    d = _load(CLOUD)
    web, workers = d["services"]["loom"], d["services"]["loom-workers"]
    assert set(web["volumes"]) == set(workers["volumes"])
    assert "loom-run:/app/var/run" in web["volumes"]
    assert "loom-blob-data:/app/var/blobs" in web["volumes"]
    assert "../var/code_repos:/app/var/code_repos" in web["volumes"]
    assert set(d["volumes"]) == {"loom-blob-data", "loom-run"}
    for svc in (web, workers):
        assert _env(svc)["LOOM_SUPERVISOR_LOCK_PATH"] == "/app/var/run/supervisor.lock"
        assert svc["stop_grace_period"] == "45s"
        assert svc["ulimits"]["nofile"] == {"soft": 65536, "hard": 65536}
        assert svc["env_file"] == ["../.env"]


def test_the_worker_service_documents_why_it_shares_no_rate_table() -> None:
    text = CLOUD.read_text(encoding="utf-8")
    assert "LOOM_RATELIMIT_SHARED_PATH" not in text  # neither service sets one …
    assert "needs no table" in text  # … and the file says why


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_every_compose_deployment_says_it_is_the_deployment(path: Path) -> None:
    d = _load(path)
    loom_services = [s for name, s in d["services"].items() if name.startswith("loom")]
    assert loom_services, path.name
    for svc in loom_services:
        env = _env(svc)
        assert env.get("LOOM_DEPLOYMENT") == "1", (path.name, svc.get("container_name"))
        assert env.get("LOOM_INSTANCE_LABEL", "").startswith("deploy:"), path.name


def test_each_colour_is_a_web_tier_and_a_workers_container() -> None:
    """The blue/green colour carries the same split: the web service is off,
    the workers service contends for the shared lock (auto — the old colour's
    holds it through the overlap, and the lock retry hands it over), one
    process, no published port, same image."""
    d = _load(COLOUR)
    assert set(d["services"]) == {"loom", "workers"}
    web, workers = d["services"]["loom"], d["services"]["workers"]
    assert web["image"] == workers["image"]
    assert web["build"] == workers["build"]
    assert workers["container_name"] == "loom-${LOOM_COLOUR}-workers"
    wenv, env = _env(workers), _env(web)
    assert env["LOOM_BACKGROUND_WORKERS"] == "off"
    assert wenv["LOOM_BACKGROUND_WORKERS"] == "auto"
    assert wenv["LOOM_WEB_WORKERS"] == "1"
    assert env["LOOM_WEB_WORKERS"] == "${LOOM_WEB_WORKERS:-1}"
    assert "ports" not in workers and web["ports"]
    assert (
        wenv["LOOM_SUPERVISOR_LOCK_PATH"]
        == env["LOOM_SUPERVISOR_LOCK_PATH"]
        == "/app/var/shared/supervisor.lock"
    )
    assert "LOOM_RATELIMIT_SHARED_PATH" in env and "LOOM_RATELIMIT_SHARED_PATH" not in wenv
    assert set(web["volumes"]) == set(workers["volumes"])
    assert env["LOOM_INSTANCE_LABEL"] == "deploy:web:${LOOM_COLOUR}"
    assert wenv["LOOM_INSTANCE_LABEL"] == "deploy:workers:${LOOM_COLOUR}"


def test_redeploy_retires_and_rolls_back_the_workers_container_with_its_colour() -> None:
    sh = (ROOT / "redeploy.sh").read_text(encoding="utf-8")
    assert 'wait_workers "$idle"' in sh
    assert 'docker stop -t 45 "loom-$active-workers"' in sh
    assert 'docker start "loom-$prev-workers"' in sh


def test_labels_separate_the_two_deployment_processes() -> None:
    d = _load(CLOUD)
    assert _env(d["services"]["loom"])["LOOM_INSTANCE_LABEL"] == "deploy:web"
    assert _env(d["services"]["loom-workers"])["LOOM_INSTANCE_LABEL"] == "deploy:workers"


def test_launch_paths_outside_compose_carry_a_label() -> None:
    """CI boots the server (uvicorn or the image's launcher) from the job env;
    `make server` and `loom serve` name themselves. Without these the
    launcher-identity rule would refuse each of them."""
    ci = _load(ROOT / ".github" / "workflows" / "ci.yml")
    booting = 0
    for name, job in ci["jobs"].items():
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            if "uvicorn server.main:app" not in run and "scripts/serve.sh" not in run:
                continue
            booting += 1
            env = {**(job.get("env") or {}), **(step.get("env") or {})}
            assert env.get("LOOM_INSTANCE_LABEL"), f"{name}: a server boot without a label"
    assert booting >= 1
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "dev:make-server" in makefile
    cli = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "server" / "cli").glob("*.py"))
    )
    assert 'setdefault("LOOM_INSTANCE_LABEL"' in cli
    assert "LOOM_INSTANCE_LABEL" in (ROOT / ".env.example").read_text(encoding="utf-8")
