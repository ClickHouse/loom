"""Own an isolated native Loom deployment and qualify public history before inference."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from examples.software_factory.factory.model import digest

from .arms import ALL, validate_schedule
from .index import snapshot
from .memory import treatment_fixture, validate_history_certificate
from .source_tree import source_files


def remote_identity(task):
    from server.code.domain.index.remote import parse_remote, repo_tag

    parsed = parse_remote(task.get("git_remote") or task["repository"])
    return parsed.url, repo_tag(parsed.url)


OWNER_LABEL = "loom.factory.owner"
CH_IMAGE = (
    "clickhouse/clickhouse-server:26.10.4.207@sha256:"
    "4f491abc44d9f4a1747c033c35ddd5bc385521305d85ca8251ba0824ff597035"
)
IDENTITY = dict(agent="factory-pilot", user_id="-", session_id="-")


def save(path, value, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermissionError("Refuse a setup artifact symlink")
    with path.open("w") as stream:
        if private:
            os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise PermissionError(message)


class Provisioner:
    def __init__(
        self,
        source_root: Path,
        output: Path,
        owner: str,
        *,
        before_paid=None,
        after_paid=None,
        gateway=None,
        run=subprocess.run,
        http_client=None,
    ):
        require(
            isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{32}", owner),
            "Use the controller run UUID as owner",
        )
        self.source_root = Path(source_root).resolve(strict=True)
        self.output = Path(output).resolve()
        self.owner = owner
        self.before_paid, self.after_paid, self.gateway = before_paid, after_paid, gateway
        self.run, self.http = run, http_client
        self._owns_http = http_client is None
        self.runtime = None
        self.output.mkdir(parents=True, exist_ok=True)
        identity = self.output / "owner.json"
        if identity.exists():
            require(
                json.loads(identity.read_text())
                == {"owner": owner, "server_source_root": str(self.source_root)},
                "Runtime output belongs to another run",
            )
        else:
            if any(self.output.iterdir()):
                launch = self.output / "launch.json"
                require(
                    launch.is_file() and not launch.is_symlink(),
                    "Refuse an existing unowned output directory",
                )
                proof = json.loads(launch.read_text())
                require(
                    proof.get("owner") == owner
                    and isinstance(proof.get("source_root"), str)
                    and proof["source_root"]
                    and Path(proof["source_root"]).resolve() == self.source_root,
                    "Launcher ownership or source differs",
                )
            save(identity, {"owner": owner, "server_source_root": str(self.source_root)})
        (self.output / "private").mkdir(mode=0o700, exist_ok=True)
        (self.output / "mirrors").mkdir(exist_ok=True)
        (self.output / "runtime-config").mkdir(exist_ok=True)
        self.journal = self.output / "resources.jsonl"
        self.prefix = "factory-" + owner[:16]

    def _record(self, event, **fields):
        row = {"event": event, "owner": self.owner, "time": time.time(), **fields}
        with self.journal.open("a") as stream:
            stream.write(json.dumps(row, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return row

    def _cmd(self, command, *, data=None, timeout=1800):
        try:
            result = self.run(
                command, input=data, capture_output=True, text=True, check=True, timeout=timeout
            )
        except subprocess.CalledProcessError as error:
            self._build_log(command, error.stdout, error.stderr, error.returncode)
            raise
        if command[:2] == ["docker", "build"]:
            self._build_log(command, result.stdout, result.stderr, result.returncode)
        return result

    def _build_log(self, command, stdout, stderr, returncode):
        folder = self.output / "logs"
        folder.mkdir(exist_ok=True)
        content = json.dumps(
            {"command": command, "stdout": stdout, "stderr": stderr, "returncode": returncode}
        )
        for name in ("ANTHROPIC_API_KEY", "HTTPS_PROXY"):
            if value := os.environ.get(name):
                content = content.replace(value, "[redacted]")
        prefix = "build-" if command[:2] == ["docker", "build"] else "command-failure-"
        path = folder / (prefix + uuid.uuid4().hex + ".json")
        path.write_text(content + "\n")
        path.chmod(0o600)

    def _inspect(self, kind, identifier):
        noun = (
            "image"
            if kind == "image"
            else "network"
            if kind == "network"
            else "volume"
            if kind == "volume"
            else "container"
        )
        return json.loads(self._cmd(["docker", noun, "inspect", identifier]).stdout)[0]

    def _owned(self, kind, identifier):
        obj = self._inspect(kind, identifier)
        labels = (
            obj.get("Config", {}).get("Labels", {})
            if kind in {"container", "image"}
            else obj.get("Labels", {})
        )
        require(
            labels and labels.get(OWNER_LABEL) == self.owner,
            "Refuse a resource whose ownership label differs",
        )
        return obj

    def _create(self, kind, name, command):
        self._record("intent", kind=kind, name=name)
        identifier = self._cmd(command).stdout.strip()
        require(identifier, "Docker returned no resource identity")
        self._owned(kind, identifier)
        self._record("created", kind=kind, name=name, id=identifier)
        return identifier

    def _rpc(self, operation, body=None):
        payload = json.dumps({"operation": operation, "body": body or {}})
        raw = self._cmd(
            [
                "docker",
                "exec",
                "-i",
                self.runtime["server_container"],
                "/opt/venv/bin/python",
                "-m",
                "examples.software_factory.harbor.native_setup",
            ],
            data=payload,
        )
        return json.loads(raw.stdout)

    def _intent(self, phase, operation, details):
        self._record("operation_intent", phase=phase, operation=operation, details=details)
        if self.before_paid:
            self.before_paid(phase, operation, details)

    def _observed(self, phase, operation, receipt):
        if self.after_paid:
            self.after_paid(phase, operation, receipt)

    def _api(self, operation, body, token, *, phase):
        if operation in {"memory.set", "memory.update"}:
            self._intent(
                phase,
                operation,
                {
                    "org": body["org"],
                    "namespace": body["namespace"],
                    "memory_key": body.get("memory_key"),
                    "value_sha256": digest(body.get("value")),
                },
            )
        if self.http is None:
            import httpx

            self.http = httpx.Client(timeout=120)
        started = time.monotonic()
        try:
            response = self.http.post(
                self.runtime["loom_url"] + "/v1/" + operation,
                json=body,
                headers={"Authorization": "Bearer " + token},
            )
        except Exception as error:  # Preserve an uncertain setup write without replaying it.
            event = {
                "operation": operation,
                "phase": phase,
                "request": body,
                "request_id": None,
                "response": None,
                "status_code": None,
                "error_type": type(error).__name__,
                "physical_requests": None,
                "duration_s": time.monotonic() - started,
            }
            with (self.output / "setup-events.jsonl").open("a") as stream:
                stream.write(json.dumps(event, default=str) + "\n")
            self._observed(phase, operation, event)
            raise
        try:
            wire = response.json()
        except ValueError:
            wire = {"unparsed_response_sha256": hashlib.sha256(response.content).hexdigest()}
        data = wire.get("data", wire)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        event = {
            "operation": operation,
            "phase": phase,
            "request_id": request_id,
            "request": body,
            "response": data,
            "status_code": response.status_code,
            "duration_s": time.monotonic() - started,
            "physical_requests": 1,
        }
        with (self.output / "setup-events.jsonl").open("a") as stream:
            stream.write(json.dumps(event, default=str) + "\n")
        self._observed(phase, operation, event)
        response.raise_for_status()
        require(
            request_id and isinstance(data, dict) and not data.get("ignored_args"),
            "Native response has no request identity or ignored arguments",
        )
        return event

    def _mint(self, org, roles, name):
        summary = self._rpc("mint", {"subject": "sa:" + name, "org": org, "ns_roles": roles})
        require(
            summary.get("ns_roles") == roles and summary.get("org") == org and summary.get("token"),
            "Native token scope differs",
        )
        path = self.output / "private" / (name + ".token")
        path.write_text(summary.pop("token"))
        os.chmod(path, 0o600)
        save(path.with_suffix(".claims.json"), summary, private=True)
        return path

    def _mint_setup(self, org, namespaces, name):
        summary = self._rpc(
            "mint_setup",
            {
                "subject": "sa:" + name,
                "org": org,
                "namespaces": namespaces,
            },
        )
        require(
            summary.get("org") == org
            and summary.get("namespaces") == namespaces
            and summary.get("roles") == ["admin"]
            and summary.get("token"),
            "Installer token differs from the owned setup scope",
        )
        path = self.output / "private" / (name + ".token")
        path.write_text(summary.pop("token"))
        path.chmod(0o600)
        save(path.with_suffix(".claims.json"), summary, private=True)
        return path

    def _build_context(self):
        import shutil

        context = self.output / "build-source"
        require(not context.exists(), "A partial build context requires owned cleanup before retry")
        names = self._cmd(
            [
                "git",
                "-C",
                str(self.source_root),
                "ls-files",
                "-z",
                "--",
                "server",
                "sdk",
                "schemas",
                "scripts/scip_harness",
            ]
        ).stdout.split("\0")
        names = [name for name in names if name]
        names += [
            "Dockerfile",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_NOTICES",
        ]
        names += [
            "examples/software_factory/" + part
            for part in (
                "factory/model.py",
                "harbor/native_setup.py",
                "harbor/index_setup.py",
                "harbor/source_tree.py",
                "harbor/Provisioner.Dockerfile",
            )
        ]
        require(names, "No public native source files were selected")
        manifest = {}
        for name in names:
            source = self.source_root / name
            require(
                source.is_file() and not source.is_symlink(),
                "Native build input is not a regular source file",
            )
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            manifest[name] = sha(target)
        from .build_recipe import native_recipe

        production = context / "Dockerfile"
        production.write_text(native_recipe(production.read_text()))
        manifest["Dockerfile"] = sha(production)
        save(self.output / "build-source-manifest.json", manifest)
        return context

    def _build_images(self, context):
        builder = self.output / "local-models.image"
        image = self.output / "runtime.image"
        for target, path, extra in [
            ("local-models", builder, ["--target", "local-models"]),
            ("runtime", image, []),
        ]:
            if target == "runtime":
                identifier = builder.read_text().strip()
                builder_image = self._owned("image", identifier)
                require(
                    self._inspect("image", self.prefix + "-local-models")["Id"]
                    == builder_image["Id"],
                    "Native model image tag differs from its recorded image ID",
                )
                extra = [
                    "--file",
                    str(context / "examples/software_factory/harbor/Provisioner.Dockerfile"),
                    "--build-arg",
                    "LOCAL_MODELS_IMAGE=" + self.prefix + "-local-models",
                ]
            self._record("intent", kind="image", name=self.prefix + "-" + target, iidfile=str(path))
            self._cmd(
                [
                    "docker",
                    "build",
                    "--label",
                    OWNER_LABEL + "=" + self.owner,
                    "--tag",
                    self.prefix + "-" + target,
                    "--iidfile",
                    str(path),
                    *extra,
                    str(context),
                ],
                timeout=3600,
            )
            identifier = path.read_text().strip()
            self._owned("image", identifier)
            self._record("created", kind="image", id=identifier, name=self.prefix + "-" + target)
        return image

    def start(self):
        gateway = self.gateway or {}
        require(
            all(gateway.get(key) for key in ("url", "token", "controller_container")),
            "A budgeted private supplier gateway is required",
        )
        require(
            isinstance(gateway["token"], str)
            and len(gateway["token"]) >= 16
            and not any(character in gateway["token"] for character in "\r\n"),
            "Invalid opaque gateway credential",
        )
        parsed = urlparse(gateway["url"])
        alias = parsed.hostname
        require(
            parsed.username is None and parsed.password is None,
            "Gateway URL must not contain credentials",
        )
        require(
            parsed.scheme == "http"
            and alias not in {None, "localhost", "127.0.0.1", "::1"}
            and re.fullmatch(r"[A-Za-z0-9_.-]+", alias),
            "Gateway must address the owned controller on its private network",
        )
        state = self.output / "runtime.json"
        if state.exists():
            runtime = json.loads(state.read_text())
            require(runtime["owner"] == self.owner, "Runtime receipt owner differs")
            self._owned("container", runtime["server_container"])
            self._owned("container", runtime["clickhouse_container"])
            require(
                runtime["gateway"]
                == {
                    "url": gateway["url"],
                    "token_sha256": hashlib.sha256(gateway["token"].encode()).hexdigest(),
                    "controller_container": gateway["controller_container"],
                },
                "Gateway identity changed; refuse an ambiguous resume",
            )
            require(
                all(
                    sha(path) == expected
                    for path, expected in runtime["server_source_identity"]["files"].items()
                ),
                "Native source changed since the owned runtime started",
            )
            self.runtime = runtime
            self._healthy()
            return runtime
        commit = self._cmd(["git", "-C", str(self.source_root), "rev-parse", "HEAD"]).stdout.strip()
        require(
            re.fullmatch(r"[0-9a-f]{40}", commit),
            "Runtime source requires a full committed revision",
        )
        dirty = self._cmd(
            [
                "git",
                "-C",
                str(self.source_root),
                "status",
                "--porcelain",
                "--",
                "server",
                "sdk",
                "schemas",
                "pyproject.toml",
                "uv.lock",
                "Dockerfile",
                "scripts/scip_harness",
            ]
        ).stdout
        require(not dirty, "Native runtime inputs differ from source HEAD")
        context = self._build_context()
        network = self._create(
            "network",
            self.prefix,
            ["docker", "network", "create", "--label", OWNER_LABEL + "=" + self.owner, self.prefix],
        )
        controller = self._inspect("container", gateway["controller_container"])
        require(
            controller.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) == self.owner,
            "Gateway controller belongs to another run",
        )
        self._record(
            "intent",
            kind="network_attachment",
            name=gateway["controller_container"],
            id=controller["Id"],
            network=network,
        )
        self._cmd(["docker", "network", "connect", "--alias", alias, network, controller["Id"]])
        self._record(
            "created",
            kind="network_attachment",
            id=controller["Id"],
            name=gateway["controller_container"],
            network=network,
        )
        image = self._build_images(context)
        ch_secret = secrets.token_hex(24)
        jwt = secrets.token_hex(32)
        ch_env = self.output / "private/ch.env"
        ch_env.write_text(
            "CLICKHOUSE_DB=loom\nCLICKHOUSE_USER=loom\nCLICKHOUSE_PASSWORD="
            + ch_secret
            + "\nCLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1\n"
        )
        os.chmod(ch_env, 0o600)
        settings = {
            "version": 1,
            "settings": {
                "background_workers": "off",
                "auth_required": True,
                "require_real_providers": True,
                "llm_provider": "anthropic",
                "embedding_model": "local:bge-small",
                "embedding_provider": "local",
                "ch_host": self.prefix + "-ch",
                "ch_port": 8123,
                "ch_database": "loom",
                "storage_authority": "clickhouse",
                "memory_code_links_settle_seconds": 10,
                "inference_customer_header": "X-Factory-Org",
                "inference_gateway_hosts": alias,
                "inference_feature_header": "X-Factory-Feature",
                "inference_identity_headers_json": json.dumps(
                    {"X-Factory-Namespace": "{namespace}"}
                ),
            },
        }
        save(self.output / "runtime-config/settings.json", settings)
        (self.output / "runtime-config/repos.env").write_text("")
        server_env = self.output / "private/server.env"
        server_env.write_text(
            "LOOM_CH_USER=loom\nLOOM_CH_PASSWORD="
            + ch_secret
            + "\nLOOM_JWT_SECRET="
            + jwt
            + "\nANTHROPIC_API_KEY="
            + gateway["token"]
            + "\nLOOM_ANTHROPIC_BASE_URL="
            + gateway["url"]
            + "\nLOOM_SETTINGS_FILE=/runtime-config/settings.json"
            + "\nLOOM_ENV_FILE=/runtime-config/repos.env\nLOOM_BUILD_COMMIT="
            + commit
            + "\nLOOM_INSTANCE_LABEL="
            + self.prefix
            + "\nLOOM_BIND_HOST=127.0.0.1\n"
        )
        os.chmod(server_env, 0o600)
        volume = self._create(
            "volume",
            self.prefix + "-data",
            [
                "docker",
                "volume",
                "create",
                "--label",
                OWNER_LABEL + "=" + self.owner,
                self.prefix + "-data",
            ],
        )
        ch_name = self.prefix + "-ch"
        cfg = self.source_root / "deploy/clickhouse-config"
        command = [
            "docker",
            "create",
            "--name",
            ch_name,
            "--label",
            OWNER_LABEL + "=" + self.owner,
            "--network",
            network,
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--env-file",
            str(ch_env),
            "--tmpfs",
            "/var/log/clickhouse-server:rw,nosuid,nodev,size=128m,mode=1777",
            "--ulimit",
            "nofile=262144:262144",
            "--mount",
            "type=volume,source=" + volume + ",target=/var/lib/clickhouse",
        ]
        for name, target in [
            ("keeper.xml", "config.d"),
            ("access_control.xml", "config.d"),
            ("system_logs.xml", "config.d"),
            ("nlp_user_settings.xml", "users.d"),
            ("lemmatizers.xml", "config.d"),
        ]:
            command += [
                "--mount",
                f"type=bind,source={cfg / name},"
                f"target=/etc/clickhouse-server/{target}/{name},readonly",
            ]
        command += [
            "--mount",
            f"type=bind,source={self.source_root}/deploy/lemmagen3,target=/var/lib/clickhouse/lemmagen3,readonly",
            CH_IMAGE,
        ]
        ch = self._create("container", ch_name, command)
        self._cmd(["docker", "start", ch])
        self._wait_clickhouse(ch_name, ch_secret)
        server_name = self.prefix + "-loom"
        command = [
            "docker",
            "create",
            "--init",
            "--name",
            server_name,
            "--label",
            OWNER_LABEL + "=" + self.owner,
            "--network",
            network,
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env-file",
            str(server_env),
            "--publish",
            "127.0.0.1::7777",
            "--mount",
            f"type=bind,source={self.output}/runtime-config,target=/runtime-config,readonly",
            "--mount",
            f"type=bind,source={self.output}/mirrors,target={self.output}/mirrors,readonly",
            image.read_text().strip(),
        ]
        server = self._create("container", server_name, command)
        self._cmd(["docker", "start", server])
        self.runtime = {
            "schema_version": 1,
            "owner": self.owner,
            "server_container": server,
            "clickhouse_container": ch,
            "network": network,
            "loom_url": "http://" + server_name + ":7777",
            "server_commit": commit,
            "container_source_root": "/app",
            "server_image": image.read_text().strip(),
            "clickhouse_image": CH_IMAGE,
            "gateway": {
                "url": gateway["url"],
                "token_sha256": hashlib.sha256(gateway["token"].encode()).hexdigest(),
                "controller_container": gateway["controller_container"],
            },
            "server_source_root": str(self.source_root),
        }
        self._healthy()
        model = self._rpc("embedding")
        self.runtime["native_model"] = {
            "provider": model["llm_provider"],
            "model_id": model["llm_model"],
            "source": "product-default",
        }
        self.runtime["embedding"] = {
            key: model[key]
            for key in ("provider", "embedding_model", "dimension", "source", "revision", "files")
        }
        self.runtime["require_real_providers"] = model["require_real_providers"]
        from .contracts import requests

        self.runtime["request_contracts"] = self._rpc("request_contracts", {"requests": requests()})
        tracked = self._cmd(
            ["git", "-C", str(self.source_root), "ls-files", "-z", "--", "server", "schemas", "sdk"]
        ).stdout.split("\0")
        relative = {name: sha(self.source_root / name) for name in tracked if name}
        actual = self._rpc("source_identity", {"paths": ["/app/" + path for path in relative]})
        require(
            actual == {"/app/" + path: hashed for path, hashed in relative.items()},
            "Built native source differs from controller bytes",
        )
        self.runtime["server_source_identity"] = {
            "commit": commit,
            "dirty": False,
            "files": {str(self.source_root / path): hashed for path, hashed in relative.items()},
            "relative_files": relative,
            "container_source_root": "/app",
            "container_bytes_verified": True,
        }
        save(state, self.runtime)
        return self.runtime

    def _wait_clickhouse(self, name, password):
        import httpx

        deadline = time.monotonic() + 180
        with httpx.Client(timeout=2, auth=("loom", password)) as client:
            while time.monotonic() < deadline:
                state = self._owned("container", name)["State"]
                if not state["Running"]:
                    raise RuntimeError("Owned ClickHouse exited before becoming ready")
                try:
                    response = client.get("http://" + name + ":8123", params={"query": "SELECT 1"})
                    response.raise_for_status()
                    if response.text.strip() == "1":
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
        raise TimeoutError("Owned ClickHouse did not accept queries")

    def _healthy(self):
        if self.http is None:
            import httpx

            self.http = httpx.Client(timeout=120)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = self._owned("container", self.runtime["server_container"])["State"]
            if not state["Running"]:
                raise RuntimeError("Owned native Loom exited before becoming healthy")
            try:
                response = self.http.get(self.runtime["loom_url"] + "/v1/health")
                response.raise_for_status()
                data = response.json()
                if (
                    data.get("status") == "ok"
                    and data.get("instance_label") == self.prefix
                    and data.get("build_commit") == self.runtime["server_commit"]
                ):
                    return
            except Exception:  # noqa: BLE001 — startup readiness may lag the owned migration.
                pass
            time.sleep(1)
        raise TimeoutError("Owned native Loom did not become healthy")

    def _fixtures(self, tasks, fixtures):
        require(
            set(tasks) == set(fixtures), "Every task requires an explicit frozen public fixture"
        )
        result = {}
        for task_id in tasks:
            frozen = fixtures[task_id]
            path = Path(frozen["fixture_file"])
            require(
                path.is_file()
                and not path.is_symlink()
                and isinstance(frozen.get("fixture_sha256"), str)
                and sha(path) == frozen["fixture_sha256"],
                "Public fixture changed or has no immutable identity",
            )
            data = json.loads(path.read_text())
            require(data["case_id"] == task_id, "Fixture names another task")
            result[task_id] = {"fixture_file": str(path), "fixture_sha256": sha(path), "data": data}
        return result

    def _layout(self, tasks, schedule, fixtures):
        require(
            set(validate_schedule(schedule)) == set(tasks) == set(fixtures),
            "Schedule, tasks and public fixtures differ",
        )
        groups = {}
        bindings = {}
        for slot in schedule:
            task_id = slot["task_id"]
            task = tasks[task_id]
            tier = slot["index_tier"]
            arm = ALL[slot["arm"]]
            require(
                re.fullmatch(r"[0-9a-f]{40}", task["base_commit"]) and task["source_roots"],
                "Task source metadata is incomplete",
            )
            key = task_id + ":" + str(tier)
            if tier and key not in groups:
                slug = hashlib.sha256((self.owner + key).encode()).hexdigest()[:16]
                groups[key] = {
                    "org": "factory-" + slug,
                    "namespace": "code-" + slug,
                    "task_id": task_id,
                    "index_tier": tier,
                    "memory_slots": [],
                }
            group = groups.get(key)
            memory = "memory-" + str(slot["order"]) if arm.memory_enabled else None
            if memory:
                group["memory_slots"].append(slot)
            tag = remote_identity(task)[1]
            bindings[str(slot["order"])] = {
                "org": group["org"] if group else "-",
                "code_namespace": group["namespace"] if group else "-",
                "memory_namespace": memory,
                "token_file": None,
                "task_id": task_id,
                "index_tier": tier,
                "base_commit": task["base_commit"],
                "repository_tag": tag,
                "compiler_rows": None,
                "source_scope": None,
                "memory_history_sha256": digest(
                    treatment_fixture(fixtures[task_id]["data"], slot["arm"])["public_documents"]
                )
                if memory
                else None,
            }
        return groups, bindings

    def _source(self, task, fixture, revision):
        head = fixture.get("revisions", {}).get(revision, {}).get("commit")
        require(
            isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head),
            "Public source revision must name its exact commit",
        )
        files = source_files(task["source_git"], head, task["source_roots"])
        require(
            digest(files) == fixture["source_sha256"][revision],
            "Public Git source differs from fixture hash",
        )
        return head, files

    def _backfill(self, group, task, head):
        self._intent(
            "index",
            "code.backfill",
            {"org": group["org"], "namespace": group["namespace"], "commit": head},
        )
        result = self._rpc(
            "backfill", {"org": group["org"], "namespace": group["namespace"], "commit": head}
        )
        self._observed("index", "code.backfill", result)

    def _compile(self, group, head, revision, compiler, token, allowed):
        artifact = compiler["revisions"][group["task_id"] + ":" + revision]
        entry = artifact["files"]["delta.json"]
        path = Path(entry["path"])
        require(
            artifact.get("schema_validated") is True
            and artifact["base_commit"] == head
            and sha(path) == entry["sha256"],
            "Compiler artifact identity differs",
        )
        delta = json.loads(path.read_text())
        require(
            delta["commit"] == head
            and {row["path"] for row in delta["files"]} <= set(allowed)
            and set(delta.get("deleted_files", [])) <= set(allowed),
            "Compiler payload leaves the source allowlist",
        )
        payload = path.read_text()
        parts = [payload[i : i + 500000] for i in range(0, len(payload), 500000)] or [""]
        batch = str(uuid.uuid4())
        for seq, part in enumerate(parts):
            self._api(
                "code.scip_ingest",
                {
                    "org": group["org"],
                    "namespace": group["namespace"],
                    "batch_id": batch,
                    "seq": seq,
                    "total_parts": len(parts),
                    "commit_sha": head,
                    "base_sha": "",
                    "producer": artifact["producer"],
                    "payload": part,
                },
                token,
                phase="index-" + revision,
            )
        self._rpc("scip_apply")
        status = self._api(
            "code.scip_status",
            {"org": group["org"], "namespace": group["namespace"], "batch_id": batch},
            token,
            phase="index-" + revision,
        )
        require(
            status["response"].get("status") == "applied"
            and status["response"].get("commit_sha") == head,
            "Native compiler batch was not applied",
        )

    def _status(self, group, head, token, revision):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self._api(
                "code.index_status",
                {"org": group["org"], "namespace": group["namespace"], "brief": True},
                token,
                phase="index-" + revision,
            )
            data = status["response"]
            if (
                data.get("org") == group["org"]
                and data.get("namespace") == group["namespace"]
                and data.get("git_head") == head
                and data.get("build_commit") == self.runtime["server_commit"]
            ):
                if group["index_tier"] == "treesitter":
                    return status
                expected = {
                    "producer": group["language"],
                    "tool": group["producer"],
                    "watermark_key": group["watermark_key"],
                    "commit": head,
                }
                if data.get(group["watermark_key"]) == head and any(
                    all(row.get(key) == value for key, value in expected.items())
                    for row in data.get("compiler_tiers", [])
                ):
                    return status
            time.sleep(1)
        raise TimeoutError("Native index status did not confirm the actual revision/tier")

    def _worker(self, group):
        self._intent(
            "links", "memory_code_links", {"org": group["org"], "namespace": group["namespace"]}
        )
        data = self._rpc("worker", {"org": group["org"], "namespace": group["namespace"]})
        data["run_id"] = uuid.uuid4().hex
        self._observed("links", "memory_code_links", data)
        save(self.output / "worker-passes" / (data["run_id"] + ".json"), data)
        return data

    def _gets(self, group, slot, keys, token, head, phase, events, *, expect):
        rows = {}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            worker = self._worker(group)
            rows = {}
            observations = []
            for key in keys:
                event = self._api(
                    "memory.get",
                    {
                        "org": group["org"],
                        "namespace": "memory-" + str(slot["order"]),
                        **IDENTITY,
                        "memory_key": key,
                    },
                    token,
                    phase=phase,
                )
                event["source_commit"] = head
                observations.append(event)
                rows[key] = event["response"]
            if all(expect(key, row) for key, row in rows.items()):
                events.extend(observations)
                return rows, worker
            time.sleep(1)
        raise TimeoutError("Native memory links did not reach the required observed state")

    def _mirror(self, task, mirror):
        self._cmd(["git", "init", "--bare", str(mirror)])
        self._cmd(
            [
                "git",
                "-C",
                str(mirror),
                "fetch",
                "--no-tags",
                "--update-shallow",
                "--no-write-fetch-head",
                "--",
                task["source_git"],
                task["base_commit"] + ":refs/heads/factory-frozen",
            ]
        )

    def prepare(self, tasks: dict, schedule: list, fixtures: dict, compiler_receipt: dict):
        require(self.runtime is not None, "Start the owned runtime before setup")
        completed = self.output / "setup.json"
        marker = self.output / "prepare-started.json"
        if completed.exists():
            result = json.loads(completed.read_text())
            require(
                json.loads(marker.read_text())["inputs_sha256"]
                == digest([tasks, schedule, fixtures, compiler_receipt]),
                "Setup inputs changed on resume",
            )
            require(
                all(sha(path) == expected for path, expected in result["files"].items()),
                "A qualified setup artifact changed",
            )
            return result
        require(
            not marker.exists(),
            "Interrupted native setup is not replayed; inspect receipts and clean the owned run",
        )
        save(
            marker,
            {
                "owner": self.owner,
                "inputs_sha256": digest([tasks, schedule, fixtures, compiler_receipt]),
            },
        )
        fixtures = self._fixtures(tasks, fixtures)
        groups, bindings = self._layout(tasks, schedule, fixtures)
        configs = []
        mirrors = {}
        for task_id, task in tasks.items():
            mirror = (
                self.output
                / "mirrors"
                / (hashlib.sha256(task_id.encode()).hexdigest()[:16] + ".git")
            )
            self._mirror(task, mirror)
            mirrors[task_id] = str(mirror)
        for group in groups.values():
            task = tasks[group["task_id"]]
            configs.append(
                {
                    "org": group["org"],
                    "namespace": group["namespace"],
                    "repo": task["repository"],
                    "git_dir": mirrors[group["task_id"]],
                    "git_remote": remote_identity(task)[0],
                    "git_branch": "factory-frozen",
                    "path_prefixes": ",".join(task["source_roots"]),
                    "org_readable": False,
                    "public": False,
                    "classification": "standard",
                    "scip_producer": compiler_receipt["language"]
                    if group["index_tier"] == "scip"
                    else "",
                }
            )
        repo_settings = self.output / "runtime-config/repos.env"
        repo_settings.write_text("LOOM_CODE_REPOS_JSON=" + json.dumps(json.dumps(configs)) + "\n")
        self._owned("container", self.runtime["server_container"])
        self._cmd(["docker", "restart", self.runtime["server_container"]])
        self._healthy()
        certificates = {}
        indexes = {task_id: {} for task_id in tasks}
        public_fixtures = {
            key: {k: v for k, v in row.items() if k != "data"} for key, row in fixtures.items()
        }
        for key, group in groups.items():
            group["producer"] = compiler_receipt["producer"]
            group["language"] = compiler_receipt["language"]
            group["producer_count_fields"] = compiler_receipt["producer_count_fields"]
            group["watermark_key"] = self._rpc(
                "producer_metadata", {"producer": compiler_receipt["producer"]}
            )["watermark_key"]
            self._prepare_group(
                key,
                group,
                tasks,
                fixtures,
                schedule,
                bindings,
                compiler_receipt,
                public_fixtures,
                mirrors,
                certificates,
                indexes,
            )
        index_certificate = self.output / "index-certificate.json"
        save(index_certificate, {"schema_version": 1, "scopes": certificates})
        code_scopes = [
            {"org": group["org"], "namespace": group["namespace"]} for group in groups.values()
        ]
        content = snapshot(self.runtime["server_container"], code_scopes)
        content_path = self.output / "index-content.json"
        save(content_path, content)
        validation = {
            "suite": {"source_roots": next(iter(tasks.values()))["source_roots"]},
            "attempt_scopes": bindings,
            "memory_histories": public_fixtures,
        }
        for slot in schedule:
            if slot["memory_enabled"]:
                validate_history_certificate(validation, slot, bindings[str(slot["order"])])
        result = {
            "schema_version": 1,
            "runtime": self.runtime,
            "tasks": {
                task_id: {**task, "indexes": indexes[task_id]} for task_id, task in tasks.items()
            },
            "attempt_scopes": bindings,
            "memory_histories": public_fixtures,
            "index_certificate_file": str(index_certificate),
            "index_certificate_sha256": sha(index_certificate),
            "index_content_file": str(content_path),
            "index_content_sha256": digest(content),
            "shared_code_scopes": code_scopes,
            "setup_cost_basis": "Shared source/index/embedding setup once; "
            "private memory history by attempt; "
            "supplier ledger authoritative.",
        }
        self.runtime["memory_worker"] = self._rpc("start_memory_worker")
        save(self.output / "runtime.json", self.runtime)
        result["runtime"] = self.runtime
        fixed = [
            self.output / name
            for name in (
                "runtime.json",
                "owner.json",
                "index-certificate.json",
                "index-content.json",
                "build-source-manifest.json",
                "local-models.image",
                "runtime.image",
                "runtime-config/settings.json",
                "runtime-config/repos.env",
            )
        ]
        immutable = [
            path
            for name in ("worker-passes", "source-proofs", "history-certificates")
            for path in (self.output / name).rglob("*.json")
        ]
        result["files"] = {str(path): sha(path) for path in fixed + immutable if path.is_file()}
        result["files"].update(
            {row["fixture_file"]: row["fixture_sha256"] for row in public_fixtures.values()}
        )
        for row in compiler_receipt["revisions"].values():
            result["files"].update(
                {entry["path"]: entry["sha256"] for entry in row["files"].values()}
            )
        save(completed, result)
        return result

    def _prepare_group(
        self,
        key,
        group,
        tasks,
        fixtures,
        schedule,
        bindings,
        compiler_receipt,
        public_fixtures,
        mirrors,
        certificates,
        indexes,
    ):
        task = tasks[group["task_id"]]
        fixture = fixtures[group["task_id"]]["data"]
        seeded = fixture.get("kind") != "cold"
        authored = seeded and fixture.get("kind") != "historical-snapshot"
        slots = group["memory_slots"]
        history_slots = [slot for slot in slots if ALL[slot["arm"]].history_seeded]
        roles = {
            group["namespace"]: "admin",
            **{"memory-" + str(slot["order"]): "admin" for slot in slots},
        }
        installer = self._mint_setup(group["org"], list(roles), "register-" + group["namespace"])
        for namespace in roles:
            self._api(
                "namespace.register",
                {
                    "org": group["org"],
                    "namespace": namespace,
                    "owner_email": "sa:setup-" + group["namespace"],
                    "kind": "code" if namespace == group["namespace"] else "memory",
                    "visibility": "private",
                    "join_policy": "closed",
                    **({"code_change_policy": "flag"} if namespace != group["namespace"] else {}),
                },
                installer.read_text().strip(),
                phase="register",
            )
        admin_path = self._mint(group["org"], roles, "setup-" + group["namespace"])
        admin = admin_path.read_text().strip()
        self._rpc("init", {"org": group["org"], "namespace": group["namespace"]})
        events = {slot["order"]: [] for slot in slots}
        latest = {slot["order"]: {} for slot in slots}
        source_receipts = {}
        heads = {}
        phases = ("A", "B") if authored else ("B",)
        for revision in phases:
            head, files = self._source(task, fixture, revision)
            heads[revision] = head
            self._backfill(group, task, head)
            if group["index_tier"] == "scip":
                self._compile(group, head, revision, compiler_receipt, admin, files)
                # Compiler symbols change the resolver's name table; reconcile native
                # parser edges against that completed table before certifying it.
                self._backfill(group, task, head)
            if authored and revision == "B":
                old_keys = [
                    event["key"]
                    for event in fixture["history_events"]
                    if event["operation"] == "set" and event["revision"] == "A"
                ]
                a_files = self._source(task, fixture, "A")[1]
                changed = {path for path in files if a_files.get(path) != files[path]}
                anchor = {
                    event["key"]: event["files"]
                    for event in fixture["history_events"]
                    if event["operation"] == "set"
                }
                for slot in history_slots:

                    def stale(key, row, head=head, anchor=anchor, changed=changed):
                        links = row.get("code", [])
                        return row.get("found") is True and (
                            any(
                                link["state"] == "changed" and link.get("changed_since") == head
                                for link in links
                            )
                            if set(anchor[key]) & changed
                            else all(link["state"] == "fresh" for link in links)
                        )

                    latest[slot["order"]], worker = self._gets(
                        group,
                        slot,
                        old_keys,
                        admin,
                        head,
                        "B-stale",
                        events[slot["order"]],
                        expect=stale,
                    )
            for public in fixture["history_events"]:
                if public["revision"] != revision:
                    continue
                for slot in history_slots:
                    body = {
                        "org": group["org"],
                        "namespace": "memory-" + str(slot["order"]),
                        "memory_key": public["key"],
                        "value": public["value"],
                        **IDENTITY,
                    }
                    if public["operation"] == "set":
                        body.update(
                            files=public["files"],
                            tags=[bindings[str(slot["order"])]["repository_tag"]],
                            memory_type="semantic",
                        )
                    else:
                        body["if_match_sha256"] = latest[slot["order"]][public["key"]][
                            "content_sha256"
                        ]
                    event = self._api("memory." + public["operation"], body, admin, phase=revision)
                    event["source_commit"] = head
                    events[slot["order"]].append(event)
            keys = list(
                dict.fromkeys(
                    event["key"]
                    for event in fixture["history_events"]
                    if event["revision"] in (("A",) if revision == "A" else ("A", "B"))
                )
            )
            if seeded:
                current = {
                    event["key"]: event
                    for event in fixture["history_events"]
                    if event["revision"] in (("A",) if revision == "A" else ("A", "B"))
                }
                changed_keys = {
                    event["key"]
                    for event in fixture["history_events"]
                    if event["revision"] == "B" and event["operation"] == "update"
                }
                for slot in history_slots:

                    def fresh(
                        key, row, current=current, revision=revision, changed_keys=changed_keys
                    ):
                        links = row.get("code", [])
                        return row.get("found") is True and (
                            bool(links) and all(link["state"] == "fresh" for link in links)
                            if current[key]["files"]
                            and (
                                revision == "A"
                                or key in changed_keys
                                or current[key]["revision"] == "B"
                            )
                            else not links
                            if not current[key]["files"]
                            else bool(links)
                        )

                    latest[slot["order"]], worker = self._gets(
                        group,
                        slot,
                        keys,
                        admin,
                        head,
                        revision,
                        events[slot["order"]],
                        expect=fresh,
                    )
            else:
                worker = self._worker(group)
            audit = self._rpc(
                "audit",
                {
                    "org": group["org"],
                    "namespace": group["namespace"],
                    "commit": head,
                    "source_git": mirrors[group["task_id"]],
                    "source_roots": task["source_roots"],
                    "source_sha256": digest(files),
                    "producer": group["producer"],
                    "producer_count_fields": group["producer_count_fields"],
                },
            )
            status = self._status(group, head, admin, revision)
            source_receipts[revision] = {
                "indexed_head": status["response"]["git_head"],
                "checked_head": worker["checked_head"],
                "source_sha256": digest(files),
                "source_scope": audit["source_scope"],
                "index_status": status["response"],
                "request_id": status["request_id"],
                "worker": worker,
            }
            save(
                self.output / "source-proofs" / (group["namespace"] + "-" + revision + ".json"),
                audit,
            )
        b = source_receipts["B"]
        counts = audit["compiler_rows"]
        if group["index_tier"] == "treesitter":
            require(
                not any(counts.values()) and not b["index_status"].get(group["watermark_key"]),
                "Tree-sitter scope contains compiler rows",
            )
        else:
            require(
                counts[group["producer_count_fields"]["definitions"]] > 0
                and counts[group["producer_count_fields"]["references"]] > 0
                and b["index_status"].get(group["watermark_key"]) == heads["B"],
                "SCIP scope fell back to parser rows",
            )
        certificates[key] = {
            **{
                k: b["index_status"][k]
                for k in ("org", "namespace", "repo", "git_head", "build_commit")
            },
            "compiler_rows": counts,
            "source_scope": b["source_scope"],
            "source_provenance": audit["provenance"],
            "source_provenance_file": str(
                self.output / "source-proofs" / (group["namespace"] + "-B.json")
            ),
            "source_provenance_sha256": sha(
                self.output / "source-proofs" / (group["namespace"] + "-B.json")
            ),
        }
        reader = self._mint(
            group["org"], {group["namespace"]: "viewer"}, "reader-" + group["namespace"]
        )
        indexes[group["task_id"]][group["index_tier"]] = {
            "reader_token_file": str(reader),
            "source_provenance_file": certificates[key]["source_provenance_file"],
            "source_provenance_sha256": certificates[key]["source_provenance_sha256"],
            "org": group["org"],
            "namespace": group["namespace"],
            "compiler_rows": counts,
            "source_scope": b["source_scope"],
        }
        for slot in schedule:
            binding = bindings[str(slot["order"])]
            if slot["task_id"] != group["task_id"] or slot["index_tier"] != group["index_tier"]:
                continue
            roles = {group["namespace"]: "viewer"}
            if slot["memory_enabled"]:
                roles[binding["memory_namespace"]] = "writer"
            token = (
                self._mint(group["org"], roles, "attempt-" + str(slot["order"]))
                if slot["memory_enabled"]
                else reader
            )
            binding.update(
                token_file=str(token), compiler_rows=counts, source_scope=b["source_scope"]
            )
        for slot in slots:
            binding = bindings[str(slot["order"])]
            history_seeded = ALL[slot["arm"]].history_seeded
            cert = {
                "schema_version": 1,
                "kind": "factory-native-history",
                "qualified": True,
                "slot_order": slot["order"],
                "task_id": slot["task_id"],
                "arm": slot["arm"],
                "repeat": slot["repeat"],
                "org": group["org"],
                "code_namespace": group["namespace"],
                "memory_namespace": binding["memory_namespace"],
                "repository_tag": binding["repository_tag"],
                "trunk_namespace": group["namespace"],
                "public_history_sha256": binding["memory_history_sha256"],
                "fixture_sha256": public_fixtures[group["task_id"]]["fixture_sha256"],
                "source_heads": heads if history_seeded else {"B": heads["B"]},
                "source_receipts": source_receipts
                if history_seeded
                else {"B": source_receipts["B"]},
                "events": events[slot["order"]],
                "final_snapshot": [
                    dict(row, memory_key=key) for key, row in latest[slot["order"]].items()
                ],
            }
            if not seeded or not history_seeded:
                listing = {
                    "org": group["org"],
                    "namespace": binding["memory_namespace"],
                    "agent": "*",
                    "user_id": "*",
                    "session_id": "*",
                    "include_tombstoned": True,
                    "include_duplicates": True,
                    "include_superseded": True,
                    "include_proposed": True,
                }
                cert["empty_namespace"] = self._api("memory.list", listing, admin, phase="B")
            path = self.output / "history-certificates" / (str(slot["order"]) + ".json")
            save(path, cert)
            binding.update(history_certificate_file=str(path), history_certificate_sha256=sha(path))

    def _recover_intents(self, rows):
        known = {(row.get("kind"), row.get("name")) for row in rows if row["event"] == "created"}
        for row in rows:
            if row["event"] != "intent" or (row["kind"], row["name"]) in known:
                continue
            kind = row["kind"]
            if kind == "network_attachment":
                self._owned("network", row["network"])
                obj = self._owned("container", row["id"])
                if any(
                    value.get("NetworkID") == row["network"]
                    for value in obj.get("NetworkSettings", {}).get("Networks", {}).values()
                ):
                    self._record(
                        "created",
                        kind=kind,
                        name=row["name"],
                        id=row["id"],
                        network=row["network"],
                        recovered_from_intent=True,
                    )
                continue
            if kind == "image":
                path = Path(row["iidfile"])
                if not path.exists():
                    continue
                identifier = path.read_text().strip()
            else:
                identifier = row["name"]
            try:
                obj = self._owned(kind, identifier)
            except subprocess.CalledProcessError as error:
                if "No such" in (error.stderr or ""):
                    continue
                raise
            resolved = obj.get("Id", obj.get("Name", identifier))
            self._record(
                "created", kind=kind, name=row["name"], id=resolved, recovered_from_intent=True
            )

    def cleanup(self):
        rows = (
            [json.loads(line) for line in self.journal.read_text().splitlines()]
            if self.journal.exists()
            else []
        )
        self._recover_intents(rows)
        rows = (
            [json.loads(line) for line in self.journal.read_text().splitlines()]
            if self.journal.exists()
            else []
        )
        removed = set(row["id"] for row in rows if row["event"] == "removed")
        receipts = []
        for row in reversed(rows):
            if row["event"] != "created" or row["id"] in removed:
                continue
            require(row["owner"] == self.owner, "Resource journal owner differs")
            kind, identifier = row["kind"], row["id"]
            if kind == "network_attachment":
                self._owned("network", row["network"])
                self._owned("container", identifier)
                self._cmd(
                    ["docker", "network", "disconnect", "--force", row["network"], identifier]
                )
            else:
                self._owned(kind, identifier)
                if kind == "container":
                    logs = self._cmd(["docker", "logs", identifier])
                    target = self.output / "logs" / (identifier + ".log")
                    target.parent.mkdir(exist_ok=True)
                    target.write_text(logs.stdout + logs.stderr)
                    os.chmod(target, 0o600)
                command = (
                    ["docker", "rm", "-f", "-v", identifier]
                    if kind == "container"
                    else ["docker", "image", "rm", identifier]
                    if kind == "image"
                    else ["docker", kind, "rm", identifier]
                )
                self._cmd(command)
            self._record("removed", kind=kind, id=identifier, name=row["name"])
            removed.add(identifier)
            receipts.append({"kind": kind, "id": identifier, "name": row["name"]})
        if self._owns_http and self.http is not None and hasattr(self.http, "close"):
            self.http.close()
        result = {"owner": self.owner, "complete": True, "removed": receipts}
        save(self.output / "cleanup.json", result)
        return result
