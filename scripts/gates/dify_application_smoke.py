"""Run one Dify application scenario on its own GitHub-hosted VM."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx

DIFY = "7bef4e1b2dc21b2c694d3243425dd3fa6288f916"
SERVICES = {
    "agent_backend",
    "agent_ssrf_proxy",
    "api",
    "db_postgres",
    "init_permissions",
    "local_sandbox",
    "nginx",
    "plugin_daemon",
    "redis",
    "sandbox",
    "ssrf_proxy",
    "web",
    "worker",
    "worker_beat",
}
LOCAL_DOCKER = "unix:///var/run/docker.sock"
EXPECTED_PLUGIN_FILES = frozenset(
    (
        ".difyignore",
        "LICENSE",
        "PRIVACY.md",
        "README.md",
        "_assets/icon.svg",
        "client.py",
        "main.py",
        "manifest.yaml",
        "provider/__init__.py",
        "provider/loom.py",
        "provider/loom.yaml",
        "requirements.txt",
        "tools/__init__.py",
        "tools/recall.py",
        "tools/recall.yaml",
        "tools/remember.py",
        "tools/remember.yaml",
        "tools/search_knowledge.py",
        "tools/search_knowledge.yaml",
    )
)
GIB = 1024**3
PORTS = (17881, 17882, 17883, 18471, 18472, 7871)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def available():
    return (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        * 1024
    )


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]), int(fields[2]), int(fields[3])
    except (FileNotFoundError, ProcessLookupError):
        return None


def initial_password():
    """The pinned normal /init model accepts at most thirty characters."""
    return "Aa7" + secrets.token_hex(12)


def verify_docker_selection(environ, resolve_endpoint):
    require(
        not any(environ.get(name) for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")),
        "Docker host/context/config overrides are forbidden",
    )
    endpoint = resolve_endpoint()
    require(endpoint == LOCAL_DOCKER, "Effective Docker context is not the hosted local socket")
    return endpoint


def docker_arguments(args, endpoint):
    require(endpoint == LOCAL_DOCKER, "Docker endpoint is unverified")
    return ["docker", "--host", endpoint, *args]


def validate_archive_members(members):
    names = [item.filename for item in members]
    require(not any(item.is_dir() for item in members), "Plugin directory entry is unreviewed")
    require(
        len(names) == len(set(names)) and set(names) == EXPECTED_PLUGIN_FILES,
        "Plugin archive members differ from reviewed set",
    )


def image_volume_mounts(service_name, service, image):
    covered = {mount["target"] for mount in service.get("volumes", [])}
    missing = sorted(set(image.get("Config", {}).get("Volumes") or {}) - covered)
    mounts, volumes = [], {}
    for index, target in enumerate(missing):
        require(
            target.startswith("/") and target != "/" and ".." not in Path(target).parts,
            "Image volume target is invalid",
        )
        name = f"image-{service_name}-{index}"
        mounts.append({"type": "volume", "source": name, "target": target})
        volumes[name] = {}
    return mounts, volumes


def validate_platform_services(config):
    require(set(config["services"]) == SERVICES, "Official service closure differs")
    # These workflows call plugin tools; the host's optional dataset subsystem is unused.
    # An explicit empty selector preserves its documented no-vector-store mode.
    for name in ("api", "worker", "worker_beat"):
        require(
            (config["services"][name].get("environment") or {}).get("VECTOR_STORE") == "",
            "Tools-only vector configuration differs",
        )


def plugin_storage_source(services, root):
    expected = root / "docker/volumes/plugin_daemon"
    daemon = services["plugin_daemon"]
    for service, target in (
        (daemon, "/app/storage"),
        (services["init_permissions"], "/app/plugin_daemon/storage"),
    ):
        mounts = [mount for mount in service.get("volumes", []) if mount["target"] == target]
        require(
            len(mounts) == 1
            and mounts[0]["type"] == "bind"
            and mounts[0]["source"] == str(expected)
            and not mounts[0].get("read_only"),
            "Plugin storage binding differs",
        )
    require(
        not daemon.get("user")
        and daemon.get("depends_on", {}).get("init_permissions", {}).get("condition")
        == "service_completed_successfully",
        "Plugin storage initialization dependency differs",
    )
    return expected


def read_session(pid):
    snapshot = {}
    for item in Path("/proc").iterdir():
        if item.name.isdigit() and (identity := birth(int(item.name))):
            if int(item.name) == pid or identity[2] == pid:
                try:
                    snapshot[int(item.name)] = (*identity, item.stat().st_uid)
                except (FileNotFoundError, ProcessLookupError):
                    pass
    return snapshot


def authorized_session(saved, observed, uid):
    """An absent leader permits only previously saved exact member generations."""
    pid, started = saved["pid"], saved["start"]
    leader = observed.get(pid)
    if leader is not None:
        require(leader == (started, pid, pid, uid), "Session leader generation changed")
    known = {int(key): tuple(value) for key, value in saved["members"].items()}
    for member, identity in observed.items():
        require(identity[2:] == (pid, uid), "Session member owner changed")
        if leader is None:
            require(known.get(member) == identity, "Orphan member generation is unproven")
    return observed


def stop_owned_session(
    saved,
    *,
    observe,
    current_birth,
    send,
    pause,
    now,
    uid,
    remember=lambda: None,
    reap=lambda: None,
):
    for sig, seconds in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
        members = authorized_session(saved, observe(saved["pid"]), uid)
        if not members:
            return
        saved["members"].update(members)
        remember()
        for pid, identity in members.items():
            if current_birth(pid) == identity[:3]:
                try:
                    send(pid, sig)
                except ProcessLookupError:
                    pass
        end = now() + seconds
        while now() < end:
            reap()
            if not authorized_session(saved, observe(saved["pid"]), uid):
                return
            pause(0.1)
    require(
        not authorized_session(saved, observe(saved["pid"]), uid),
        "Owned session remains after cleanup",
    )


def stop_monitor(runtime):
    runtime.finished.set()
    if runtime.monitor_thread is not None:
        runtime.monitor_thread.join(timeout=5)
        require(not runtime.monitor_thread.is_alive(), "Resource monitor did not stop")


def final_failure(failure, monitor_error):
    return failure or monitor_error


def wait_fixture_ready(client, url, seconds, *, now=time.monotonic, pause=time.sleep):
    end = now() + seconds
    observations = []
    while now() < end:
        try:
            status = client.get(url).status_code
        except httpx.TransportError as error:
            observations.append({"error_type": type(error).__name__})
        else:
            observations.append({"status": status})
            require(status not in (401, 403), "Fixture readiness permission refused")
            if status == 200:
                return observations
        pause(1)
    raise RuntimeError("Owned fixture readiness failed: " + json.dumps(observations[-3:]))


class Runtime:
    def __init__(self):
        require(
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("RUNNER_OS") == "Linux",
            "GitHub-hosted Linux job required",
        )

        self.workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        sys.path.insert(0, str(self.workspace))
        self.root = Path(os.environ["RUNNER_TEMP"]).resolve() / ("dify-smoke-" + uuid.uuid4().hex)
        self.root.mkdir(mode=0o700)
        self.evidence = self.workspace / "dify-application-evidence"
        self.evidence.mkdir(mode=0o700)
        self.project = "loom-dify-" + uuid.uuid4().hex[:16]
        self.path = self.root / "dify"
        self.plugin_dir = self.root / "plugin"
        self.plugin_python = self.root / "plugin-venv/bin/python"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("LOOM_", "UV_", "PYTHON"))
        }
        self.env.update(
            COMPOSE_PROFILES="postgresql",
            VECTOR_STORE="",
            DB_TYPE="postgresql",
            EXPOSE_PLUGIN_DEBUGGING_HOST="127.0.0.1",
            EXPOSE_PLUGIN_DEBUGGING_PORT="17883",
        )
        self.env["PATH"] = str(self.workspace / ".venv/bin") + os.pathsep + self.env["PATH"]
        self.secrets = []
        for key in (
            "SECRET_KEY",
            "DB_PASSWORD",
            "REDIS_PASSWORD",
            "PLUGIN_DAEMON_KEY",
            "PLUGIN_DIFY_INNER_API_KEY",
            "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN",
            "DIFY_AGENT_API_TOKEN",
            "SANDBOX_API_KEY",
        ):
            self.env[key] = self.secret("Aa7" + secrets.token_hex(24))
        self.env["INIT_PASSWORD"] = self.secret(initial_password())
        self.env["CODE_EXECUTION_API_KEY"] = self.env["SANDBOX_API_KEY"]
        self.env["DIFY_AGENT_SERVER_SECRET_KEY"] = self.secret(
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        )
        self.env.update(
            LOOM_ENV_FILE="",
            LOOM_CH_HOST="127.0.0.1",
            LOOM_CH_PORT="18471",
            LOOM_CH_USER="loom",
            LOOM_CH_READER_USER="loom_reader",
            LOOM_CH_PASSWORD="loomdev",
            LOOM_CH_DATABASE="loom",
            LOOM_CH_SECURE="false",
            LOOM_AUTH_REQUIRED="true",
            LOOM_JWT_SECRET=self.secret(secrets.token_hex(32)),
            LOOM_BASE_URL="http://127.0.0.1:7871",
            LOOM_LLM_PROVIDER="fake",
            LOOM_EMBEDDING_MODEL="fake-ngram-1024",
            LOOM_BACKGROUND_WORKERS="off",
            LOOM_INSTANCE_LABEL="ci:dify-application",
            LOOM_WEB_WORKERS="1",
            LOOM_RUNTIME_DIR=str(self.root / "loom-runtime"),
            PORT="7871",
        )
        self.primary_profile = self.env
        self.processes, self.cleanup_callbacks = [], []
        self.containers, self.volumes, self.networks = {}, {}, {}
        self.expected_images = {}
        self.finished = threading.Event()
        self.monitor_error = None
        self.monitor_thread = None
        self.docker_endpoint = None
        self.cleaning = False
        self.deadline = time.monotonic() + 1800
        self.phase = "admission"
        self.created = False
        self.started = datetime.now(UTC).isoformat()
        self.before = {}
        self.threads = []
        self.state_path = Path(os.environ["RUNNER_TEMP"]) / "dify-smoke-ownership.json"
        require(not self.state_path.exists(), "Ownership state already exists")
        self.persist()

    def persist(self):
        state = {
            "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "root": str(self.root),
            "project": self.project,
            "started": self.started,
            "before": {key: sorted(value) for key, value in self.before.items()},
            "images": self.expected_images,
            "processes": [
                {"pid": item["child"].pid, "start": item["birth"], "members": item["members"]}
                for item in self.processes
            ],
            "ch_attempted": getattr(self, "ch_attempted", False),
            "ch_image": getattr(self, "ch_image", None),
            "compose_attempted": self.created,
            "docker_endpoint": self.docker_endpoint,
        }
        temporary = self.state_path.with_suffix(".new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output)
        temporary.replace(self.state_path)

    def secret(self, value):
        self.secrets.append(value)
        return value

    def redact(self, value):
        text = str(value)
        for secret in sorted(set(self.secrets), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def record(self, name, value):
        # Only explicit metadata reaches this method; never env, config or inspect dumps.
        text = self.redact(json.dumps(value, default=str, indent=2))
        require(len(text) <= 256_000, "Evidence record exceeds limit")
        with (self.evidence / name).open("x") as target:
            target.write(text + "\n")

    def launch(self, args, *, cwd=None, env=None, logged=False):
        child = subprocess.Popen(
            args,
            cwd=cwd or self.workspace,
            env=env or self.env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        identity = birth(child.pid)
        require(
            identity is not None and identity[1:] == (child.pid, child.pid),
            "Child session identity unavailable",
        )
        entry = {
            "child": child,
            "birth": identity[0],
            "logs": deque(maxlen=200),
            "logged": logged,
            "members": {child.pid: (*identity, os.getuid())},
        }
        self.processes.append(entry)
        self.persist()
        if logged:

            def drain():
                for line in iter(child.stdout.readline, ""):
                    entry["logs"].append(self.redact(line[:4096]))

            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            self.threads.append(thread)
        return child

    def command(self, args, *, timeout=120, cwd=None, check=True, docker_metadata=False):
        if args[0] == "docker":
            if docker_metadata:
                require(
                    args
                    == [
                        "docker",
                        "context",
                        "inspect",
                        "--format",
                        "{{json .Endpoints.docker.Host}}",
                    ],
                    "Unexpected Docker metadata request",
                )
            else:
                args = docker_arguments(args[1:], self.docker_endpoint)
        child = self.launch(args, cwd=cwd)
        entry = next(item for item in self.processes if item["child"] is child)
        try:
            output, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop_group(entry)
            raise RuntimeError("Command timeout in " + self.phase) from None
        self.retire(entry)
        if check and child.returncode:
            # Commands may print environment-derived secrets; redact before retaining a tail.
            raise RuntimeError(
                "Command failed in " + self.phase + ": " + self.redact(output)[-2000:]
            )
        return subprocess.CompletedProcess(args, child.returncode, output, "")

    def docker_json(self, *args):
        return json.loads(self.command(["docker", *args]).stdout)

    def ids(self, kind, *filters):
        return set(self.command(["docker", kind, "ls", "-q", *filters]).stdout.split())

    def compose(self, *args, timeout=120):
        files = ["docker-compose.yaml", "task-loopback.override.yaml"]
        if (self.path / "docker/task-resource.override.json").exists():
            files.append("task-resource.override.json")
        command = ["docker", "compose", "--project-name", self.project]
        for file in files:
            command += ["-f", file]
        return self.command(command + list(args), timeout=timeout, cwd=self.path / "docker")

    def retire(self, entry):
        saved = {"pid": entry["child"].pid, "start": entry["birth"], "members": entry["members"]}
        require(
            not authorized_session(saved, read_session(saved["pid"]), os.getuid()),
            "Completed command session is not empty",
        )
        self.processes.remove(entry)
        self.persist()

    def stop_group(self, entry):
        child = entry["child"]
        saved = {"pid": child.pid, "start": entry["birth"], "members": entry["members"]}
        stop_owned_session(
            saved,
            observe=read_session,
            current_birth=birth,
            send=os.kill,
            pause=time.sleep,
            now=time.monotonic,
            uid=os.getuid(),
            remember=self.persist,
            reap=child.poll,
        )
        child.wait(timeout=2)
        self.retire(entry)

    def admit(self):
        self.docker_endpoint = verify_docker_selection(
            self.env,
            lambda: json.loads(
                self.command(
                    ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
                    docker_metadata=True,
                ).stdout
            ),
        )
        # Also binds the existing image resolver's internal Docker commands to this same endpoint.
        self.env["DOCKER_HOST"] = self.docker_endpoint
        self.persist()
        require(available() >= 12 * GIB, "CI admission requires 12 GiB MemAvailable")
        require(shutil.disk_usage(self.root).free >= 10 * GIB, "CI admission requires 10 GiB disk")
        require((os.cpu_count() or 0) >= 4, "CI admission requires four logical CPUs")
        version = (
            self.command(["docker", "compose", "version", "--short"]).stdout.strip().lstrip("v")
        )
        parts = version.split(".")
        require(
            len(parts) >= 3
            and all(part.isdigit() for part in parts[:3])
            and tuple(map(int, parts[:3])) >= (2, 35, 0),
            "Docker Compose 2.35.0+ required",
        )
        for port in PORTS:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        self.before = {
            kind: self.ids(
                kind,
                *(
                    ["-a", "--no-trunc"]
                    if kind == "container"
                    else ["--no-trunc"]
                    if kind == "network"
                    else []
                ),
            )
            for kind in ("container", "network", "volume")
        }
        self.persist()
        # This job owns its VM; a pre-existing workload is not adopted or removed.
        require(not self.before["container"], "Unexpected pre-existing container")
        self.record(
            "admission.json",
            {
                "candidate": self.command(["git", "rev-parse", "HEAD"]).stdout.strip(),
                "dify_commit": DIFY,
                "project": self.project,
                "mem_available": available(),
                "disk_free": shutil.disk_usage(self.root).free,
                "cpu_count": os.cpu_count(),
                "ci_limits": {
                    "start_memory_gib": 12,
                    "start_disk_gib": 10,
                    "stop_memory_gib": 2,
                    "stop_disk_gib": 3,
                    "seconds": 1800,
                },
            },
        )
        self.monitor_thread = threading.Thread(target=self.monitor, daemon=True)
        self.monitor_thread.start()

    def monitor(self):
        while not self.finished.wait(1):
            try:
                require(available() >= 2 * GIB, "CI memory reserve crossed")
                require(shutil.disk_usage(self.root).free >= 3 * GIB, "CI disk reserve crossed")
                require(time.monotonic() < self.deadline, "CI controller deadline crossed")
                if getattr(self, "loom_pid", None):
                    rss = 0
                    for item in Path("/proc").iterdir():
                        if item.name.isdigit() and (ident := birth(int(item.name))):
                            if ident[1:] == (self.loom_pid, self.loom_pid):
                                try:
                                    rss += (
                                        int(
                                            next(
                                                line.split()[1]
                                                for line in (item / "status")
                                                .read_text()
                                                .splitlines()
                                                if line.startswith("VmRSS:")
                                            )
                                        )
                                        * 1024
                                    )
                                except (FileNotFoundError, ProcessLookupError, StopIteration):
                                    pass
                    require(rss <= 2 * GIB, "Owned Loom RSS cap crossed")
            except Exception as error:  # noqa: BLE001 — Stop owned work on a failed resource observation.
                self.monitor_error = str(error)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    def platform(self):
        self.phase = "official-source"
        self.command(["git", "init", str(self.path)])
        self.command(
            [
                "git",
                "-C",
                str(self.path),
                "fetch",
                "--depth=1",
                "https://github.com/langgenius/dify.git",
                DIFY,
            ],
            timeout=180,
        )
        self.command(["git", "-C", str(self.path), "checkout", "--detach", "FETCH_HEAD"])
        require(
            self.command(["git", "-C", str(self.path), "rev-parse", "HEAD"]).stdout.strip() == DIFY,
            "Official source commit differs",
        )
        # Official root .env is a service env_file, not just Compose interpolation.
        # This private file contains only this job's fresh values and is never an artifact.
        env_keys = (
            "COMPOSE_PROFILES",
            "VECTOR_STORE",
            "DB_TYPE",
            "SECRET_KEY",
            "DB_PASSWORD",
            "REDIS_PASSWORD",
            "PLUGIN_DAEMON_KEY",
            "PLUGIN_DIFY_INNER_API_KEY",
            "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN",
            "DIFY_AGENT_API_TOKEN",
            "DIFY_AGENT_SERVER_SECRET_KEY",
            "SANDBOX_API_KEY",
            "CODE_EXECUTION_API_KEY",
            "INIT_PASSWORD",
        )
        descriptor = os.open(self.path / "docker/.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write("".join(key + "=" + self.env[key] + "\n" for key in env_keys))
        shutil.copyfile(
            self.workspace / "scripts/gates/dify-loopback.override.yaml",
            self.path / "docker/task-loopback.override.yaml",
        )
        self.cfg = json.loads(self.compose("config", "--format", "json").stdout)
        validate_platform_services(self.cfg)
        self.record(
            "host-scope.json",
            {
                "tools": ["remember", "recall", "search_knowledge"],
                "host_vector_store": None,
                "host_dataset_retrieval_tested": False,
            },
        )
        require(
            str(
                self.cfg["services"]["plugin_daemon"]["environment"]["FORCE_VERIFYING_SIGNATURE"]
            ).lower()
            == "true",
            "Signature verification differs",
        )
        published = []
        for service in self.cfg["services"].values():
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    require(
                        Path(mount["source"]).resolve().is_relative_to(self.path), "Foreign bind"
                    )
            for port in service.get("ports", []):
                require(port.get("host_ip") == "127.0.0.1", "Non-loopback publication")
                published.append(int(port["published"]))
        require(sorted(published) == [17881, 17882, 17883], "Unexpected published port")
        self.phase = "official-image-pull"
        self.compose("pull", timeout=600)
        override = {"services": {}, "volumes": {}}
        for name, service in self.cfg["services"].items():
            (image,) = self.docker_json("image", "inspect", service["image"])
            mounts, volumes = image_volume_mounts(name, service, image)
            require(
                not (volumes.keys() & self.cfg.get("volumes", {}).keys()), "Volume name collision"
            )
            override["volumes"].update(volumes)
            self.expected_images[name] = image["Id"]
            cap = (
                2 * GIB
                if name in ("api", "worker")
                else GIB
                if name in ("web", "agent_backend", "plugin_daemon")
                else GIB // 2
            )
            override["services"][name] = {"image": image["Id"], "mem_limit": cap, "cpus": 1.0}
            if mounts:
                override["services"][name]["volumes"] = mounts
        (self.path / "docker/task-resource.override.json").write_text(json.dumps(override))
        self.cfg = json.loads(self.compose("config", "--format", "json").stdout)
        for name, service in self.cfg["services"].items():
            (image,) = self.docker_json("image", "inspect", service["image"])
            covered = {mount["target"] for mount in service.get("volumes", [])}
            require(image["Id"] == self.expected_images[name], "Pinned image changed")
            require(
                not (set(image.get("Config", {}).get("Volumes") or {}) - covered),
                "Unreviewed anonymous image volume",
            )
        self.record("platform-caps.json", override)
        self.created = True
        self.persist()
        self.prepare_plugin_storage()
        self.phase = "official-platform-start"
        self.compose("up", "-d", "--pull", "never", timeout=300)
        self.discover()
        require(
            {row["service"] for row in self.containers.values()} >= SERVICES,
            "Missing official service container",
        )

    def prepare_plugin_storage(self):
        self.phase = "official-storage-prepare"
        service = self.cfg["services"]["plugin_daemon"]
        (image,) = self.docker_json("image", "inspect", service["image"])
        require(
            image["Id"] == self.expected_images["plugin_daemon"]
            and image["Config"]["User"] == "dify",
            "Plugin storage image differs",
        )
        storage = plugin_storage_source(self.cfg["services"], self.path)
        require(
            not storage.exists()
            and not storage.is_symlink()
            and storage.parent.resolve() == storage.parent,
            "Plugin storage must be fresh",
        )
        # The official init service changes ownership only after this fresh bind is checked.
        storage.mkdir(mode=0o700, parents=True)
        original = storage.stat()
        require(
            original.st_uid == os.getuid() and not any(storage.iterdir()),
            "Plugin storage owner differs",
        )
        self.plugin_storage = storage
        self.plugin_storage_identity = original.st_dev, original.st_ino

    def verify_plugin_storage(self, checkpoint):
        storage = self.plugin_storage
        current = storage.lstat()
        marker = storage / ".init_permissions"
        require(
            not storage.is_symlink()
            and storage.is_dir()
            and (current.st_dev, current.st_ino) == self.plugin_storage_identity
            and (current.st_uid, current.st_gid) == (1001, 1001)
            and marker.is_file()
            and not marker.is_symlink(),
            "Plugin storage initialization differs",
        )
        self.record(
            "plugin-storage-" + checkpoint + ".json",
            {
                "image": self.expected_images["plugin_daemon"],
                "user": "dify",
                "uid": current.st_uid,
                "gid": current.st_gid,
                "initializer": "init_permissions",
                "device": current.st_dev,
                "inode": current.st_ino,
            },
        )

    def platform_ready(self, checkpoint):
        end = time.monotonic() + 180
        while time.monotonic() < end:
            ready = True
            for ident, owner in self.containers.items():
                (row,) = self.docker_json("container", "inspect", ident)
                state = row["State"]
                require(not state.get("OOMKilled"), "Official service was OOM-killed")
                if owner["service"] == "init_permissions":
                    require(state["Status"] in ("created", "running", "exited"), "Init failed")
                    ready &= state["Status"] == "exited" and state["ExitCode"] == 0
                else:
                    require(state["Status"] in ("created", "running"), "Official service exited")
                    ready &= (
                        state["Status"] == "running"
                        and state.get("Health", {}).get("Status", "healthy") == "healthy"
                    )
            if ready:
                self.verify_plugin_storage(checkpoint)
                return
            time.sleep(2)
        raise RuntimeError("Official service readiness deadline exceeded")

    def discover(self):
        containers = {}
        for ident in self.ids(
            "container",
            "-a",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + self.project,
        ):
            (data,) = self.docker_json("container", "inspect", ident)
            labels = data["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            require(
                ident not in self.before["container"]
                and data["Id"] == ident
                and labels.get("com.docker.compose.project") == self.project
                and service in SERVICES
                and data["Created"] >= self.started
                and data["Image"] == self.expected_images[service],
                "Container ownership differs",
            )
            expected_cap = (
                2 * GIB
                if service in ("api", "worker")
                else GIB
                if service in ("web", "agent_backend", "plugin_daemon")
                else GIB // 2
            )
            require(data["HostConfig"]["Memory"] == expected_cap, "Container memory cap differs")
            for mount in data["Mounts"]:
                if mount["Type"] == "bind":
                    require(
                        Path(mount["Source"]).resolve().is_relative_to(self.path),
                        "Foreign runtime bind",
                    )
                elif mount["Type"] == "volume":
                    (volume,) = self.docker_json("volume", "inspect", mount["Name"])
                    require(
                        volume["Name"] not in self.before["volume"]
                        and (volume.get("Labels") or {}).get("com.docker.compose.project")
                        == self.project,
                        "Volume ownership differs",
                    )
                    self.volumes[volume["Name"]] = volume["CreatedAt"]
                else:
                    require(mount["Type"] == "tmpfs", "Unexpected mount type")
            containers[ident] = {"service": service, "created": data["Created"]}
        for name in self.ids(
            "volume", "--filter", "label=com.docker.compose.project=" + self.project
        ):
            (row,) = self.docker_json("volume", "inspect", name)
            require(
                name not in self.before["volume"]
                and (row.get("Labels") or {}).get("com.docker.compose.project") == self.project
                and datetime.fromisoformat(row["CreatedAt"].replace("Z", "+00:00"))
                >= datetime.fromisoformat(self.started).replace(microsecond=0),
                "Declared volume differs",
            )
            self.volumes[name] = row["CreatedAt"]
        for ident in self.ids(
            "network", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project
        ):
            (data,) = self.docker_json("network", "inspect", ident)
            require(
                ident not in self.before["network"]
                and data["Created"] >= self.started
                and (data.get("Labels") or {}).get("com.docker.compose.project") == self.project,
                "Network ownership differs",
            )
            self.networks[ident] = data["Created"]
        self.containers = containers

    def fixture(self):
        self.phase = "loom-fixture"
        image = self.command(["bash", "dev/clickhouse_release.sh"], timeout=120).stdout.strip()
        self.command(["docker", "pull", image], timeout=300)
        (data,) = self.docker_json("image", "inspect", image)
        self.ch_image = data["Id"]
        args = [
            "docker",
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            self.project + "-ch",
            "--label",
            "loom.dify-smoke=" + self.project,
            "--memory",
            "1536m",
            "--cpus",
            "1",
            "-p",
            "127.0.0.1:18471:8123",
            "-p",
            "127.0.0.1:18472:9000",
            "-e",
            "CLICKHOUSE_DB=loom",
            "-e",
            "CLICKHOUSE_USER=loom",
            "-e",
            "CLICKHOUSE_PASSWORD=loomdev",
            "-e",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--ulimit",
            "nofile=262144:262144",
        ]
        mounts = [("deploy/lemmagen3", "/var/lib/clickhouse/lemmagen3")]
        mounts += [
            ("deploy/clickhouse-config/" + name, target + name)
            for name, target in (
                ("lemmatizers.xml", "/etc/clickhouse-server/config.d/"),
                ("nlp_user_settings.xml", "/etc/clickhouse-server/users.d/"),
                ("keeper.xml", "/etc/clickhouse-server/config.d/"),
                ("access_control.xml", "/etc/clickhouse-server/users.d/"),
                ("system_logs.xml", "/etc/clickhouse-server/config.d/"),
            )
        ]
        mounts += [
            ("dev/clickhouse-ci/" + name, "/etc/clickhouse-server/users.d/" + name)
            for name in ("log_queries_readonly.xml", "custom_settings_prefixes.xml")
        ]
        for source, target in mounts:
            args += ["-v", str(self.workspace / source) + ":" + target + ":ro"]
        self.ch_attempted = True
        self.persist()
        self.command(args + [self.ch_image])
        self.capture_ch()
        self.phase = "clickhouse-readiness"
        with httpx.Client(timeout=3, trust_env=False) as client:
            self.record(
                "clickhouse-readiness.json",
                wait_fixture_ready(client, "http://127.0.0.1:18471/ping", 80),
            )
        self.phase = "loom-migrations"
        self.command([sys.executable, "-m", "server.core.schema.migrate"], timeout=300)
        self.loom_pid = self.launch(["sh", "scripts/serve.sh"], logged=True).pid
        self.phase = "loom-readiness"
        with httpx.Client(timeout=5, trust_env=False) as client:
            self.record(
                "loom-readiness.json",
                wait_fixture_ready(client, self.env["LOOM_BASE_URL"] + "/v1/health", 120),
            )
            require(
                client.get(self.env["LOOM_BASE_URL"] + "/v1/me/service_accounts").status_code
                == 401,
                "Owned fixture did not enforce authentication",
            )
        # Same operator signing path and namespace registration as auth-on handoff CI.
        os.environ.update(
            {key: value for key, value in self.env.items() if key.startswith("LOOM_")}
        )
        from loom import Agent

        from server.auth import keyring
        from server.auth.jwt import mint_token

        keyring.sign_with_root_only()
        self.org = "it-dify-app-" + secrets.token_hex(4)
        self.namespace, self.agent = "dify-app", "dify-app"
        admin = self.secret(
            mint_token(
                subject="dify-smoke-setup",
                org=self.org,
                namespaces=[self.namespace],
                roles=["admin"],
                ttl_seconds=600,
                is_service_account=True,
            )
        )
        self.token = self.secret(
            mint_token(
                subject="dify-smoke-plugin",
                org=self.org,
                namespaces=[self.namespace],
                roles=["writer"],
                ttl_seconds=600,
                is_service_account=True,
            )
        )
        from tests.integration._resources import ch_client_from_env, purge

        def purge_own_org():
            client = ch_client_from_env()
            try:
                require(
                    not purge(client, "loom", frozenset({self.org})), "Owned org purge left rows"
                )
                self.record("loom-org-purge.json", {"org": self.org, "remaining_rows": {}})
            finally:
                client.close()

        self.cleanup_callbacks.append(purge_own_org)

        async def register():
            async with Agent(
                self.org,
                self.namespace,
                "dify-smoke-setup",
                auto_run=False,
                base_url=self.env["LOOM_BASE_URL"],
                api_key=admin,
            ) as agent:
                await agent.namespaces.register(owner_email="dify-smoke@example.test")

        asyncio.run(register())

    def capture_ch(self):
        rows = self.docker_json("container", "inspect", self.project + "-ch")
        (data,) = rows
        require(
            data["Id"] not in self.before["container"]
            and data["Image"] == self.ch_image
            and data["Created"] >= self.started
            and (data["Config"].get("Labels") or {}).get("loom.dify-smoke") == self.project,
            "ClickHouse ownership differs",
        )
        self.ch_id = data["Id"]
        self.ch_volumes = {}
        for mount in data["Mounts"]:
            if mount["Type"] == "volume":
                (volume,) = self.docker_json("volume", "inspect", mount["Name"])
                require(
                    volume["Name"] not in self.before["volume"], "Pre-existing ClickHouse volume"
                )
                self.ch_volumes[volume["Name"]] = volume["CreatedAt"]

    def artifact(self):
        self.phase = "candidate-wheel"
        wheels = list((self.workspace / "dify-sdk-wheel").glob("clickhouse_loom-*.whl"))
        require(len(wheels) == 1, "Exactly one candidate wheel required")
        (wheel,) = wheels
        archive = self.workspace / "loom-0.1.0.difypkg"
        self.plugin_dir.mkdir()
        with zipfile.ZipFile(archive) as package:
            validate_archive_members(package.infolist())
            for member in package.infolist():
                target = self.plugin_dir / member.filename
                require(
                    target.resolve().is_relative_to(self.plugin_dir), "Unsafe plugin archive path"
                )
                require(
                    (member.external_attr >> 16) & 0o170000 != 0o120000, "Plugin archive symlink"
                )
                if not member.is_dir():
                    source = self.workspace / "integrations/dify" / member.filename
                    require(
                        source.is_file() and package.read(member) == source.read_bytes(),
                        "Packaged source differs from candidate",
                    )
            package.extractall(self.plugin_dir)
        self.command([sys.executable, "-m", "venv", str(self.root / "plugin-venv")])
        self.command(
            [
                str(self.plugin_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
                "-r",
                str(self.plugin_dir / "requirements.txt"),
            ],
            timeout=180,
        )
        provenance = self.command(
            [
                str(self.plugin_python),
                "-c",
                "import importlib.metadata,json,loom; "
                "print(json.dumps({'version':importlib.metadata.version('clickhouse-loom'),"
                "'module':loom.__file__}))",
            ],
            cwd=self.plugin_dir,
        ).stdout
        installed = json.loads(provenance)
        require(
            Path(installed["module"]).resolve().is_relative_to(self.root / "plugin-venv"),
            "Plugin did not import isolated wheel installation",
        )
        with zipfile.ZipFile(wheel) as package:
            module_root = Path(installed["module"]).parent.parent
            members = [
                name
                for name in package.namelist()
                if name.startswith("loom/") and not name.endswith("/")
            ]
            require(bool(members), "Candidate wheel has no SDK package")
            require(
                all(
                    (module_root / name).is_file()
                    and (module_root / name).read_bytes() == package.read(name)
                    for name in members
                ),
                "Installed SDK bytes differ from candidate wheel",
            )
        self.record(
            "artifact.json",
            {
                "sdk_files_matched": len(members),
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "installed_version": installed["version"],
                "isolated_import": True,
                "registry_publication_proven": False,
            },
        )

    def track_plugin(self, child):
        identity = birth(child.pid)
        require(
            identity is not None and identity[1:] == (child.pid, child.pid),
            "Plugin session differs",
        )
        self.processes.append(
            {
                "child": child,
                "birth": identity[0],
                "logs": deque(),
                "logged": False,
                "members": {child.pid: (*identity, os.getuid())},
            }
        )
        self.persist()

    def cleanup(self):
        self.cleaning = True
        errors = []
        try:
            stop_monitor(self)
        except Exception as error:  # noqa: BLE001 — Cleanup continues after a failed monitor join.
            errors.append(self.redact(error))
        # Close/purge while the fixture is alive; the plugin must stop before its log reader joins.
        for entry in reversed(list(self.processes)):
            if entry["child"].args[:1] == [str(self.plugin_python)]:
                try:
                    self.stop_group(entry)
                except Exception as error:  # noqa: BLE001 — Preserve all cleanup failures.
                    errors.append(self.redact(error))
        for callback in self.cleanup_callbacks:
            try:
                callback()
            except Exception as error:  # noqa: BLE001 — Remaining owned resources still need cleanup.
                errors.append(self.redact(error))
        for entry in reversed(list(self.processes)):
            try:
                self.stop_group(entry)
            except Exception as error:  # noqa: BLE001 — Remaining owned resources still need cleanup.
                errors.append(self.redact(error))
        for thread in self.threads:
            thread.join(timeout=2)
            if thread.is_alive():
                errors.append("Owned log reader remains")
        try:
            if self.created:
                self.discover()
                for ident in self.containers:
                    (current,) = self.docker_json("container", "inspect", ident)
                    require(
                        current["Created"] == self.containers[ident]["created"],
                        "Container birth differs",
                    )
                    self.command(["docker", "container", "stop", "--time", "10", ident], timeout=20)
                    self.command(["docker", "container", "rm", "-v", ident])
                for name, created in self.volumes.items():
                    if name in self.ids("volume"):
                        (current,) = self.docker_json("volume", "inspect", name)
                        require(current["CreatedAt"] == created, "Volume birth differs")
                        self.command(["docker", "volume", "rm", name])
                for ident, created in self.networks.items():
                    (current,) = self.docker_json("network", "inspect", ident)
                    require(current["Created"] == created, "Network birth differs")
                    self.command(["docker", "network", "rm", ident])
                require(
                    not self.ids(
                        "container",
                        "-a",
                        "--filter",
                        "label=com.docker.compose.project=" + self.project,
                    )
                    and not self.ids(
                        "network", "--filter", "label=com.docker.compose.project=" + self.project
                    )
                    and not self.ids(
                        "volume", "--filter", "label=com.docker.compose.project=" + self.project
                    ),
                    "Compose resources remain",
                )
            if getattr(self, "ch_attempted", False):
                self.capture_ch()
                self.command(
                    ["docker", "container", "stop", "--time", "10", self.ch_id], timeout=20
                )
                self.command(["docker", "container", "rm", "-v", self.ch_id])
                require(
                    self.ch_id not in self.ids("container", "-a", "--no-trunc"),
                    "ClickHouse remains",
                )
                require(
                    not (set(self.ch_volumes) & self.ids("volume")), "ClickHouse volumes remain"
                )
        except Exception as error:  # noqa: BLE001 — Unverified cleanup must invalidate the result.
            errors.append(self.redact(error))
        self.record(
            "cleanup.json",
            {
                "verified": not errors,
                "errors": errors,
                "runtime_files_removed": False,
                "ephemeral_vm_disposal_is_not_cleanup_proof": True,
            },
        )
        require(not errors, "Owned cleanup incomplete")


def cleanup_only():
    """Recover exact owned resources after an interrupted controller, then fail that run."""
    require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("RUNNER_OS") == "Linux",
        "GitHub-hosted local Docker required",
    )

    def resolve_endpoint():
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        require(result.returncode == 0, "Cannot resolve effective Docker context")
        return json.loads(result.stdout)

    endpoint = verify_docker_selection(os.environ, resolve_endpoint)
    docker_env = dict(os.environ, DOCKER_HOST=endpoint)
    evidence = Path(os.environ["GITHUB_WORKSPACE"]) / "dify-application-evidence"
    completed = evidence / "cleanup.json"
    if completed.is_file() and json.loads(completed.read_text()).get("verified") is True:
        return
    state_path = Path(os.environ["RUNNER_TEMP"]) / "dify-smoke-ownership.json"
    require(state_path.is_file() and not state_path.is_symlink(), "Missing cleanup ownership state")
    state = json.loads(state_path.read_text())
    require(state["docker_endpoint"] == endpoint, "Cleanup Docker endpoint changed")
    require(
        state["run_id"] == os.environ["GITHUB_RUN_ID"]
        and state["attempt"] == os.environ["GITHUB_RUN_ATTEMPT"],
        "Wrong job cleanup state",
    )
    root = Path(state["root"])
    require(
        root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
        and root.name.startswith("dify-smoke-")
        and root.stat().st_uid == os.getuid(),
        "Wrong cleanup root",
    )
    errors = []

    def command(*args):
        result = subprocess.run(
            docker_arguments(args, endpoint),
            env=docker_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(result.returncode == 0, "Docker cleanup operation failed")
        return result.stdout

    def inspect(kind, ident):
        (row,) = json.loads(command(kind, "inspect", ident))
        return row

    try:
        for entry in reversed(state["processes"]):
            stop_owned_session(
                entry,
                observe=read_session,
                current_birth=birth,
                send=os.kill,
                pause=time.sleep,
                now=time.monotonic,
                uid=os.getuid(),
            )
        project = state["project"]
        before = state["before"]
        if state["compose_attempted"]:
            identifiers = command(
                "container",
                "ls",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ).split()
            volumes = {}
            for ident in identifiers:
                row = inspect("container", ident)
                labels = row["Config"].get("Labels") or {}
                service = labels.get("com.docker.compose.service")
                require(
                    ident not in before["container"]
                    and row["Created"] >= state["started"]
                    and labels.get("com.docker.compose.project") == project
                    and service in SERVICES
                    and row["Image"] == state["images"][service],
                    "Recovered container differs",
                )
                for mount in row["Mounts"]:
                    if mount["Type"] == "bind":
                        require(
                            Path(mount["Source"]).resolve().is_relative_to(root / "dify"),
                            "Recovered bind differs",
                        )
                    elif mount["Type"] == "volume":
                        volume = inspect("volume", mount["Name"])
                        require(
                            mount["Name"] not in before["volume"]
                            and (volume.get("Labels") or {}).get("com.docker.compose.project")
                            == project,
                            "Recovered volume differs",
                        )
                        volumes[mount["Name"]] = volume["CreatedAt"]
                command("container", "stop", "--time", "10", ident)
                command("container", "rm", "-v", ident)
            for name in command(
                "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
            ).split():
                row = inspect("volume", name)
                require(
                    name not in before["volume"]
                    and (row.get("Labels") or {}).get("com.docker.compose.project") == project
                    and datetime.fromisoformat(row["CreatedAt"].replace("Z", "+00:00"))
                    >= datetime.fromisoformat(state["started"]).replace(microsecond=0),
                    "Recovered declared volume differs",
                )
                volumes[name] = row["CreatedAt"]
            remaining_volumes = command("volume", "ls", "-q").split()
            for name, created in volumes.items():
                if name in remaining_volumes:
                    require(
                        inspect("volume", name)["CreatedAt"] == created,
                        "Recovered volume birth differs",
                    )
                    command("volume", "rm", name)
            for ident in command(
                "network",
                "ls",
                "-q",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ).split():
                row = inspect("network", ident)
                require(
                    ident not in before["network"]
                    and row["Created"] >= state["started"]
                    and (row.get("Labels") or {}).get("com.docker.compose.project") == project,
                    "Recovered network differs",
                )
                command("network", "rm", ident)
        if state["ch_attempted"]:
            matches = command(
                "container",
                "ls",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=loom.dify-smoke=" + project,
            ).split()
            require(len(matches) <= 1, "Ambiguous recovered ClickHouse")
            for ident in matches:
                row = inspect("container", ident)
                require(
                    ident not in before["container"]
                    and row["Created"] >= state["started"]
                    and row["Image"] == state["ch_image"]
                    and (row["Config"].get("Labels") or {}).get("loom.dify-smoke") == project,
                    "Recovered ClickHouse differs",
                )
                command("container", "stop", "--time", "10", ident)
                command("container", "rm", "-v", ident)
    except Exception as error:  # noqa: BLE001 — Preserve interrupted cleanup as a failure.
        errors.append(type(error).__name__)
    evidence.mkdir(exist_ok=True)
    (evidence / "cleanup-fallback.json").write_text(
        json.dumps(
            {
                "errors": errors,
                "original_controller_cleanup_verified": False,
                "status": "INCOMPLETE",
                "vm_disposal_is_not_cleanup_proof": True,
            }
        )
        + "\n"
    )
    # Recovered containment does not manufacture a successful controller or purge receipt.
    raise SystemExit(1)


def main():
    runtime = Runtime()

    def interrupt(signum, frame):
        if not runtime.cleaning:
            raise RuntimeError("Interrupted signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    failure = None
    try:
        runtime.admit()
        runtime.artifact()
        runtime.platform()
        runtime.fixture()
        runtime.platform_ready("before-tools")
        # The scenario is ordinary tool use against this fixture, never the parent deployment.
        from dify_application_scenario import run

        runtime.phase = "native-application"
        run(runtime)
        runtime.platform_ready("after-tools")
        require(runtime.monitor_error is None, "Resource monitor failed")
    except Exception as error:  # noqa: BLE001 — Record a bounded failure before owned cleanup.
        failure = runtime.redact(error)
        runtime.record(
            "failure.json",
            {"phase": runtime.phase, "error_type": type(error).__name__, "error": failure},
        )
    finally:
        try:
            runtime.cleanup()
        except Exception as error:  # noqa: BLE001 — Cleanup failure overrides an apparent success.
            failure = failure or runtime.redact(error)
    failure = final_failure(failure, runtime.monitor_error)
    runtime.record(
        "result.json",
        {
            "passed": failure is None,
            "failure": failure,
            "monitor_error": runtime.monitor_error,
            "scope": "CE normal setup, workspace debug, three native workflow tools",
            "cloud_marketplace_browser_protected_family_qualified": False,
        },
    )
    if failure:
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--cleanup-only"]:
        cleanup_only()
    else:
        require(not sys.argv[1:], "Unexpected arguments")
        main()
