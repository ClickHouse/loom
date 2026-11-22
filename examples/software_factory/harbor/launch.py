"""Run the frozen public evaluation with host Python and a local Docker daemon."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .arms import PRIMARY
from .build_recipe import PYTHON_IMAGE, native_recipe
from .catalog import load_evaluation
from .host_tunnel import HostTunnel

ROOT = Path(__file__).resolve().parents[3]
HERE = Path("examples/software_factory/harbor")
OWNER_LABEL = "loom.factory.owner"
OUTPUT_LABEL = "loom.factory.output"
SOURCE_LABEL = "loom.factory.source"
ARMS = tuple(PRIMARY)
IGNORED = {".git", ".venv", ".task-artifacts", "__pycache__", "node_modules", ".pytest_cache"}
HOST_ENV = {
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "TMPDIR",
    "TEMP",
    "SYSTEMROOT",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
}


class LaunchError(RuntimeError):
    """An actionable refusal that contains no credentials or provider payloads."""


class OwnershipError(LaunchError):
    """A stopped ownership guard must not trigger another mutation."""


def budget(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Budget must be a finite positive USD amount") from None
    return format(amount.normalize(), "f")


def read(path):
    if path.is_symlink():
        raise LaunchError("Refusing a symlinked ownership or cleanup record")
    return json.loads(path.read_text())


def save(path, data):
    if path.is_symlink():
        raise LaunchError("Refusing a symlinked ownership record")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def model_pin(root):
    tree = ast.parse((root / "server/embedding/local_models.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PINNED":
            for index, key in enumerate(node.value.keys):
                value = node.value.values[index]
                if ast.literal_eval(key) == "bge-small":
                    return {
                        keyword.arg: ast.literal_eval(keyword.value) for keyword in value.keywords
                    }
    raise LaunchError("The checked local embedding model pin is missing")


def native_model(root):
    tree = ast.parse((root / "server/core/llm_provider.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "DEFAULT_MODEL_BY_PROVIDER" for target in node.targets
        ):
            value = ast.literal_eval(node.value)["anthropic"]
            if isinstance(value, str) and value:
                return value
    raise LaunchError("The native Anthropic model default is missing")


def plan(root=ROOT, evaluation="flask"):
    descriptor = load_evaluation(root, evaluation)
    locked, histories, memories = (
        read(descriptor["paths"][name])
        for name in ("suite_lock", "scenario_manifest", "memory_catalog")
    )
    task_ids = {row["task_id"] for row in locked["tasks"]}
    history_ids = {row["case_id"] for row in histories["cases"]}
    cold, warm = len(task_ids - history_ids), len(history_ids)
    cases, repeats = len(task_ids | history_ids), 3
    memory_attempts = cases * repeats * sum(arm.memory_enabled for arm in PRIMARY.values())
    seeded_attempts = warm * repeats * sum(arm.history_seeded for arm in PRIMARY.values())
    records = memories["records"]
    linked = sum(bool(row["files"]) for row in records)
    if not cases or len({row["key"] for row in records}) != len(records):
        raise LaunchError("Public cases or project memory identities are invalid")
    return {
        "schema_version": 1,
        "evaluation": {
            key: descriptor[key] for key in ("id", "title", "adapter", "descriptor_sha256")
        },
        "model": locked["model_id"],
        "native_provider": "anthropic",
        "native_model": native_model(root),
        "arms": list(ARMS),
        "cases": cases,
        "repeats": repeats,
        "primary_attempts": cases * repeats * len(ARMS),
        "aa_attempts": cases * 2,
        "total_attempts": cases * (repeats * len(ARMS) + 2),
        "blind_judges": {
            "model": locked["model_id"],
            "fixed_only": True,
            "requests": cases * (repeats + 1),
        },
        "embedding": {"model": "local:bge-small", "provider": "local", **model_pin(root)},
        "memory": {
            "history_cases": warm,
            "cold_cases": cold,
            "project_records": len(records),
            "linked": linked,
            "unlinked": len(records) - linked,
            "equal_plain_notes_all_arms": False,
            "historical_knowledge": "Only history-seeded arms receive the frozen historical corpus",
            "memory_namespaces": memory_attempts,
            "seeded_memory_attempts": seeded_attempts,
            "cold_memory_attempts": memory_attempts - seeded_attempts,
            "cold_memory_records": 0,
            "delivery": "Optional tools; project records are not inserted into the prompt",
        },
        "paid_requests": False,
    }


def files(root, relative, output=None):
    path = root / relative
    candidates = []
    if path.is_file():
        candidates.append(path)
    else:
        for folder, directories, names in os.walk(path):
            directories[:] = [d for d in directories if d not in IGNORED and not d.startswith(".")]
            candidates.extend(Path(folder) / name for name in sorted(names))
    for item in candidates:
        if (
            item.name.startswith(".")
            or item.suffix == ".pyc"
            or (output and (item == output or output in item.parents))
        ):
            continue
        resolved = item.resolve()
        if root not in resolved.parents or any(
            part in IGNORED or part.startswith(".") for part in resolved.relative_to(root).parts
        ):
            raise LaunchError("A source link leaves the allowed checkout inputs")
        if item.is_file():
            yield item


def source_hash(root, output):
    hashes = {}
    for relative in (
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "server",
        "sdk",
        "schemas",
        "scripts",
        "examples/software_factory",
    ):
        for path in files(root, relative, output):
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def build_context(root, destination, output=None):
    production = native_recipe((root / "Dockerfile").read_text())
    marker = re.search(r"^FROM py-builder AS local-models\s*$", production, re.MULTILINE)
    fragment = (root / HERE / "controller.Dockerfile").read_text()
    if marker is None:
        raise LaunchError("The production Python builder stage cannot be identified")
    prefix = production[: marker.start()]
    base = re.search(r"^FROM (\S+@sha256:[0-9a-f]{64}) AS py-builder$", prefix, re.MULTILINE)
    if (
        base is None
        or base[1] != PYTHON_IMAGE
        or "FROM " + base[1] + " AS factory-controller" not in fragment
    ):
        raise LaunchError("Controller and core Python base images must have the same pinned ABI")
    if prefix.count("--extra local-embedder") != 1:
        raise LaunchError("The locked core dependency command differs")
    prefix = prefix.replace(
        "--extra local-embedder", "--extra local-embedder --extra scip --extra extractor"
    )
    (destination / "Dockerfile").write_text(prefix + "\n" + fragment)
    shutil.copyfile(root / HERE / "requirements.txt", destination / "controller-requirements.txt")
    for relative in ("pyproject.toml", "uv.lock", "README.md", "server", "sdk"):
        for path in files(root, relative, output):
            target = destination / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


class Docker:
    def __init__(self):
        self.process = None
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.proxy_url = self.proxy_host_url = self.proxy_token = None

    def environment(self, secret=False):
        env = {key: value for key, value in os.environ.items() if key in HOST_ENV}
        if secret:
            env["ANTHROPIC_API_KEY"] = self.key
        if self.proxy_url:
            env["HTTPS_PROXY"] = self.proxy_url
        return env

    def run(self, args, *, capture=False, missing=False, secret=False):
        env = self.environment(secret)
        if self.proxy_url and args[0] == "build":
            # Registry authentication runs on the host; build steps run in Docker.
            env["HTTPS_PROXY"] = self.proxy_host_url
            args = ["build", "--build-arg", "HTTPS_PROXY=" + self.proxy_url, *args[1:]]
        if not capture:
            process = subprocess.Popen(
                ["docker", *args],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            try:
                for line in process.stdout:
                    print(self.redact(line), end="", flush=True)
                if process.wait():
                    raise LaunchError("Docker command failed: " + args[0])
            except KeyboardInterrupt:
                process.send_signal(signal.SIGINT)
                try:
                    process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=15)
                raise
            return ""
        result = subprocess.run(
            ["docker", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            if missing and any(
                text in result.stderr.lower()
                for text in ("no such container", "no such image", "no such object")
            ):
                return None
            raise LaunchError("Docker command failed: " + args[0])
        if not capture and result.stdout:
            print(self.redact(result.stdout), end="")
        return result.stdout.strip()

    def redact(self, text):
        for value in (self.key, self.proxy_url, self.proxy_host_url, self.proxy_token):
            if value:
                text = text.replace(value, "[redacted]")
        return text

    def attach(self, name, *, secret=False):
        self.process = subprocess.Popen(
            ["docker", "start", "--attach", name],
            env=self.environment(secret),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in self.process.stdout:
            print(self.redact(line), end="", flush=True)
        return self.process.wait()

    def interrupt(self, name):
        self.run(["kill", "--signal", "SIGINT", name])
        try:
            remaining, _ = self.process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.run(["stop", "--timeout", "10", name])
            remaining, _ = self.process.communicate(timeout=15)
        if remaining:
            print(self.redact(remaining), end="")


def labels(record):
    return {
        OWNER_LABEL: record["owner"],
        OUTPUT_LABEL: hashlib.sha256(record["output_root"].encode()).hexdigest(),
        SOURCE_LABEL: record["source_sha256"],
    }


def inspect(docker, record, kind, name, expected=None):
    expression = '{"id":{{json .Id}},"labels":{{json .Config.Labels}}'
    if kind == "container":
        expression += ',"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}}'
    value = docker.run(
        [kind, "inspect", "--format", expression + "}", name], capture=True, missing=True
    )
    if value is None:
        return None
    found = json.loads(value)
    if (expected and found["id"] != expected) or any(
        found["labels"].get(k) != v for k, v in labels(record).items()
    ):
        raise OwnershipError("Refusing a resource whose recorded identity or ownership differs")
    return found


def container_args(record, action, name):
    root, output = record["source_root"], record["output_root"]
    args = ["create", "--name", name, "--init"]
    for key, value in labels(record).items():
        args.extend(["--label", key + "=" + value])
    paths = [root, str(Path(root) / "sdk/python")]
    gitfile = Path(root) / ".git"
    if gitfile.is_file():
        marker = gitfile.read_text().strip()
        if not marker.startswith("gitdir: "):
            raise LaunchError("Worktree Git metadata is malformed")
        directory = (Path(root) / marker.removeprefix("gitdir: ")).resolve(strict=True)
        commonfile = directory / "commondir"
        common = (directory / commonfile.read_text().strip()).resolve(strict=True)
        if not directory.is_relative_to(common) or not (common / "objects").is_dir():
            raise LaunchError("Worktree Git metadata has no containing object store")
        if not common.is_relative_to(Path(root)):
            paths.append(str(common))
    for path in paths:
        if "," in path:
            raise LaunchError("Docker bind paths must not contain commas")
        args.extend(["--mount", f"type=bind,src={path},dst={path},readonly"])
    if "," in output:
        raise LaunchError("Docker bind paths must not contain commas")
    args.extend(
        [
            "--mount",
            f"type=bind,src={output},dst={output}",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--workdir",
            root,
            "--env",
            f"PYTHONPATH={root}:{root}/sdk/python",
        ]
    )
    if action != "cleanup":
        args.extend(["--env", "ANTHROPIC_API_KEY"])
    if record.get("host_tunnel"):
        args.extend(["--env", "HTTPS_PROXY"])
    args.extend(
        [
            record["image_id"],
            action,
            "--output",
            output,
            "--owner",
            record["owner"],
            "--eval",
            record["evaluation"],
            "--source-root",
            root,
        ]
    )
    if action != "cleanup" and record["budget_usd"] is not None:
        args.extend(["--budget-usd", record["budget_usd"]])
    return args


def invoke(docker, record, action, path):
    if action != "cleanup" and cleaned(record):
        previous = Path(record["output_root"]) / "cleanup-receipt.json"
        archived = previous.with_name(f"cleanup-receipt-prior-{len(record['containers'])}.json")
        if archived.exists() or archived.is_symlink():
            raise OwnershipError("Refusing to overwrite a previous cleanup receipt")
        previous.rename(archived)
    name = "loom-factory-" + record["owner"] + "-" + str(len(record["containers"]))
    item = {"name": name, "id": None, "action": action}
    record["containers"].append(item)
    record["outer_cleanup_complete"] = False
    record["image_retained_for_resume"] = False
    save(path, record)
    identifier = docker.run(
        container_args(record, action, name), capture=True, secret=action != "cleanup"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise LaunchError("Docker did not return a controller identity")
    item["id"] = identifier
    save(path, record)
    inspect(docker, record, "container", name, identifier)
    record["started"] = True
    save(path, record)
    try:
        docker.attach(name, secret=action != "cleanup")
    except KeyboardInterrupt:
        inspect(docker, record, "container", name, identifier)
        docker.interrupt(name)
        raise
    observed = inspect(docker, record, "container", name, identifier)
    if observed is None or observed["running"]:
        raise LaunchError("Controller completion is unverified")
    return observed["exit_code"]


def cleaned(record):
    path = Path(record["output_root"]) / "cleanup-receipt.json"
    if not path.exists():
        return False
    receipt = read(path)
    if receipt.get("owner") != record["owner"]:
        raise LaunchError("Cleanup receipt belongs to another run")
    return receipt.get("complete") is True


def cleanup(docker, record, path, *, keep_image=False):
    for item in record["containers"]:
        found = inspect(docker, record, "container", item["name"], item["id"])
        if found and found["running"]:
            docker.run(["stop", "--timeout", "30", found["id"]])
    if record["started"] and not cleaned(record):
        if invoke(docker, record, "cleanup", path) != 0 or not cleaned(record):
            raise LaunchError("Inner cleanup is incomplete; exact owned resources remain recorded")
    for item in record["containers"]:
        found = inspect(docker, record, "container", item["name"], item["id"])
        if found:
            if found["running"]:
                raise LaunchError("A recorded controller is still running")
            docker.run(["rm", found["id"]])
    if not keep_image:
        found = inspect(docker, record, "image", record["image_tag"], record["image_id"])
        if found:
            docker.run(["image", "rm", record["image_tag"]])
    record["image_retained_for_resume"] = keep_image
    record["outer_cleanup_complete"] = True
    save(path, record)


@contextmanager
def output_lock(output):
    try:
        import fcntl
    except ImportError:
        raise LaunchError("Run from a Unix host or WSL with local Docker bind mounts") from None
    path = output / "launch.lock"
    if path.is_symlink():
        raise OwnershipError("Refusing a symlinked launch lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LaunchError(
                "Another launcher owns this output; no second action was started"
            ) from None
        yield
    finally:
        os.close(descriptor)


def execute(action, output, amount=None, *, root=ROOT, docker=None, evaluation="flask"):
    docker = docker or Docker()
    requested = Path(output).absolute()
    if requested.is_symlink():
        raise LaunchError("Output must not be a symlink")
    output, root = requested.resolve(), Path(root).resolve()
    descriptor = load_evaluation(root, evaluation)
    if action == "run":
        if not docker.key or any(c.isspace() for c in docker.key):
            raise LaunchError("Run requires ANTHROPIC_API_KEY")
        if output.exists():
            raise LaunchError("Run requires a fresh output path; use explicit resume or cleanup")
        if not (root / HERE / "one_shot.py").is_file():
            raise LaunchError("The inner controller entry point is missing")
        output.mkdir(parents=True, mode=0o700)
    with output_lock(output):
        use_tunnel = isinstance(docker, Docker) and sys.platform == "darwin"
        with HostTunnel() if use_tunnel else nullcontext() as tunnel:
            if tunnel is not None:
                docker.proxy_url, docker.proxy_token = tunnel.url, tunnel.token
                docker.proxy_host_url = tunnel.host_url
            try:
                return _execute(
                    action, output, amount, root=root, docker=docker, descriptor=descriptor
                )
            finally:
                if tunnel is not None:
                    docker.proxy_url = docker.proxy_host_url = docker.proxy_token = None


def _execute(action, output, amount, *, root, docker, descriptor):
    path = output / "launch.json"
    if action == "run":
        owner = uuid.uuid4().hex
        record = dict(
            schema_version=1,
            kind="loom-factory-launch",
            owner=owner,
            evaluation=descriptor["id"],
            source_root=str(root),
            output_root=str(output),
            source_sha256=source_hash(root, output),
            budget_usd=amount,
            image_tag="loom-factory-controller:" + owner,
            image_id=None,
            containers=[],
            started=False,
            host_tunnel=isinstance(docker, Docker) and bool(docker.proxy_url),
            state="building",
        )
        save(path, record)
    else:
        record = read(path)
        if (
            record.get("kind") != "loom-factory-launch"
            or record.get("schema_version") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", record.get("owner", ""))
            or record.get("source_root") != str(root)
            or record.get("evaluation") != descriptor["id"]
            or record.get("output_root") != str(output)
            or record.get("source_sha256") != source_hash(root, output)
        ):
            raise LaunchError("Frozen launch ownership or source inputs differ")
        if action == "resume" and (
            amount != record["budget_usd"] or not docker.key or record["state"] == "completed"
        ):
            raise LaunchError("Resume requires the same budget, credentials and an incomplete run")
    if os.environ.get("DOCKER_HOST", "unix://").startswith(("tcp:", "ssh:")):
        raise LaunchError("Use a local Docker daemon so bind paths refer to this checkout")
    docker.run(["version", "--format", "{{.Server.Version}}"], capture=True)
    if action == "cleanup":
        cleanup(docker, record, path)
        return 0
    if action == "resume":
        for item in record["containers"]:
            found = inspect(docker, record, "container", item["name"], item["id"])
            if found and found["running"]:
                raise LaunchError(
                    "The recorded controller is still running; no second run was started"
                )
    try:
        if action == "run":
            with tempfile.TemporaryDirectory(prefix="loom-factory-build-") as directory:
                context = Path(directory)
                build_context(root, context, output)
                args = ["build", "--target", "factory-controller", "--tag", record["image_tag"]]
                for key, value in labels(record).items():
                    args.extend(["--label", key + "=" + value])
                docker.run([*args, str(context)])
            found = inspect(docker, record, "image", record["image_tag"])
            if found is None:
                raise LaunchError("Controller image creation is unverified")
            record["image_id"] = found["id"]
            save(path, record)
        elif inspect(docker, record, "image", record["image_tag"], record["image_id"]) is None:
            raise LaunchError("The frozen controller image is missing")
        record["state"] = "running"
        if source_hash(root, output) != record["source_sha256"]:
            raise OwnershipError("Checkout inputs changed during controller preparation")
        save(path, record)
        result = invoke(docker, record, action, path)
        record["state"] = "completed" if result == 0 else "failed"
    except KeyboardInterrupt:
        record["state"] = "interrupted"
        result = 130
    except OwnershipError:
        record["state"] = "ownership_refused"
        save(path, record)
        raise
    except Exception:
        record["state"] = "failed"
        save(path, record)
        cleanup(docker, record, path, keep_image=record["started"])
        raise
    save(path, record)
    cleanup(docker, record, path, keep_image=result != 0)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    free = sub.add_parser("plan", help="Describe the frozen evaluation without Docker or providers")
    free.add_argument("--json", action="store_true")
    free.add_argument("--eval", default="flask", dest="evaluation")
    for name in ("run", "resume", "cleanup"):
        child = sub.add_parser(name)
        child.add_argument("--output", required=True)
        child.add_argument("--eval", default="flask", dest="evaluation")
        if name != "cleanup":
            child.add_argument("--budget-usd", type=budget)
    args = parser.parse_args(argv)
    if args.command == "plan":
        description = plan(evaluation=args.evaluation)
        if args.json:
            print(json.dumps(description, indent=2))
        else:
            print(
                f"{description['total_attempts']} attempts: {description['cases']} cases × "
                f"{description['repeats']} repeats × {len(ARMS)} arms + "
                f"{description['aa_attempts']} A/A controls"
            )
            print(json.dumps(description, indent=2))
        return 0
    previous = {}

    def interrupted(*_):
        raise KeyboardInterrupt

    try:
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, interrupted)
        return execute(
            args.command, args.output, getattr(args, "budget_usd", None), evaluation=args.evaluation
        )
    except (LaunchError, OSError, ValueError) as error:
        print(f"Factory launcher refused ({type(error).__name__}): {error}", file=sys.stderr)
        return 2
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)
