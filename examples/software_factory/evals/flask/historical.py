"""Trusted historical source preparation and fixed, isolated objective grading.

No provider, network, index ingestion or reference patch is exposed by this module.
The controller supplies independently frozen private grading inputs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import resource
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from examples.software_factory.factory.model import Task, digest
from examples.software_factory.factory.workspace import Checkout
from examples.software_factory.harbor.cleanup import DOCKER_CONTROL_TIMEOUT_S, remove_owned


def controller_hashes() -> dict[str, str]:
    return {
        str(path.relative_to(Path(__file__).parents[2])): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(Path(__file__).parents[2].rglob("*.py"))
    }


def ordinary(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and all(part not in {".", ".."} and not part.startswith(".") for part in path.parts)
        and str(path) == name
    )


def fixture_path(name: str) -> bool:
    """Allow base-revision dotfiles without allowing path escape or Git metadata."""
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and str(path) == name
        and bool(path.parts)
        and all(part not in {".", "..", ".git"} for part in path.parts)
    )


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    root = root.resolve(strict=True)
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *args,
        ],
        input=data,
        capture_output=True,
        env=env,
        timeout=20,
        check=True,
    )
    return result.stdout


def bound_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))


def blob_bytes(mirror: Path, entries: list[tuple[str, str, int]]) -> dict[str, bytes]:
    if not entries:
        return {}
    raw = git(
        mirror, "cat-file", "--batch", data="".join(oid + "\n" for _, oid, _ in entries).encode()
    )
    result, cursor = {}, 0
    for name, oid, size in entries:
        end = raw.index(b"\n", cursor)
        if raw[cursor:end] != f"{oid} blob {size}".encode():
            raise PermissionError("Source blob identity differs from its tree entry")
        cursor = end + 1
        content = raw[cursor : cursor + size]
        cursor += size
        if len(content) != size or raw[cursor : cursor + 1] != b"\n":
            raise PermissionError("Source blob is truncated")
        cursor += 1
        result[name] = content
    if cursor != len(raw):
        raise PermissionError("Unexpected bytes after the source export")
    return result


class FrozenSource:
    """Read only exact-base blobs; the agent never receives the object database."""

    def __init__(self, mirror: Path, commit: str):
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("A full source commit is required")
        self.mirror, self.commit = mirror.resolve(strict=True), commit
        if git(self.mirror, "rev-parse", commit + "^{commit}").decode().strip() != commit:
            raise PermissionError("Source base differs from the frozen commit")

    def files(self, prefix: str | None = None) -> dict[str, str]:
        args = ["ls-tree", "-rlz", self.commit]
        if prefix is not None:
            if not ordinary(prefix):
                raise PermissionError("Invalid implementation root")
            args += ["--", prefix]
        entries, total = [], 0
        for record in git(self.mirror, *args).split(b"\0"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode, kind, oid, raw_size = metadata.decode().split()
            name = raw_name.decode()
            if not ordinary(name):
                continue
            if mode not in {"100644", "100755"} or kind != "blob":
                raise PermissionError("Source tree contains a nonregular file")
            size = int(raw_size)
            total += size
            if size < 0 or size > 1024 * 1024 or total > 20 * 1024 * 1024:
                raise ValueError("Frozen source exceeds the preparation budget")
            entries.append((name, oid, size))
        files = {}
        for name, content in blob_bytes(self.mirror, entries).items():
            try:
                files[name] = content.decode("utf-8")
            except UnicodeDecodeError:
                if prefix is not None:
                    raise ValueError("Implementation source must be UTF-8") from None
        if not files:
            raise ValueError("No ordinary source files found")
        return files

    def fixtures(self) -> dict[str, str]:
        """Base64 blobs omitted by files(); never part of editable Task.files."""
        entries, total = [], 0
        for record in git(self.mirror, "ls-tree", "-rlz", self.commit).split(b"\0"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode, kind, oid, raw_size = metadata.decode().split()
            name = raw_name.decode()
            if not fixture_path(name) or mode not in {"100644", "100755"} or kind != "blob":
                raise PermissionError("Fixture tree contains an unsafe path or nonregular file")
            size = int(raw_size)
            total += size
            if size < 0 or size > 1024 * 1024 or total > 20 * 1024 * 1024:
                raise ValueError("Frozen source exceeds the preparation budget")
            entries.append((name, oid, size))
        fixtures = {}
        for name, content in blob_bytes(self.mirror, entries).items():
            if ordinary(name):
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    continue
            fixtures[name] = base64.b64encode(content).decode("ascii")

        return fixtures

    def task(self, public: dict, namespace: str, symbol: str) -> Task:
        if public["base_commit"] != self.commit or public["repository"] != "pallets/flask":
            raise PermissionError("Public task and source provenance disagree")
        files = self.files("src/flask")
        return Task(
            public["task_id"],
            "historical-flask",
            public["repository"],
            self.commit,
            namespace,
            public["problem"],
            symbol,
            files,
            digest(files),
            {name: content for name, content in self.files().items() if name not in files},
        )


def junit_statuses(path: Path) -> dict[str, str]:
    """Reconstruct exact legacy JUnit identifiers, including parameter values."""
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("JUnit result exceeds the output budget")
    statuses = {}
    root = ET.parse(path).getroot()
    if any(int(s.get("errors", "0")) for s in root.iter("testsuite")):
        raise ValueError("Collection or fixture errors invalidate the attempt")
    for row in root.iter("testcase"):
        filename, name, classname = (
            row.get("file", ""),
            row.get("name", ""),
            row.get("classname", ""),
        )
        if (
            not ordinary(filename)
            or not filename.startswith("tests/")
            or not filename.endswith(".py")
            or not name
        ):
            raise ValueError("JUnit test path is not a frozen test file")
        module = filename[:-3].replace("/", ".")
        if classname != module and not classname.startswith(module + "."):
            raise ValueError("JUnit class and file disagree")
        nested = classname[len(module) :].strip(".").replace(".", "::")
        node = filename + "::" + (nested + "::" if nested else "") + name
        if node in statuses:
            raise ValueError("Duplicate JUnit test identifier")
        statuses[node] = (
            "FAILED"
            if row.find("failure") is not None
            else (
                "ERROR"
                if row.find("error") is not None
                else "SKIPPED"
                if row.find("skipped") is not None
                else "PASSED"
            )
        )
    if not statuses:
        raise ValueError("JUnit contains no tests")
    return statuses


def selected_statuses(statuses: dict[str, str], selectors: list[str]) -> dict[str, str]:
    selected = {}
    for node in selectors:
        matches = [
            status
            for key, status in statuses.items()
            if key == node or ("[" not in node and key.startswith(node + "["))
        ]
        selected[node] = (
            "PASSED"
            if matches and all(s == "PASSED" for s in matches)
            else ("FAILED" if matches and all(s == "FAILED" for s in matches) else "ERROR")
        )
    return selected


class DockerTests:
    """One fixed focused pytest invocation, no host credentials or writable source."""

    def __init__(
        self,
        image: str,
        selectors: list[str],
        output: Path,
        *,
        timeout_s: int = 30,
        docker_binary: str = "docker",
    ):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Tests require an already local immutable image")
        if (
            not selectors
            or len(selectors) > 64
            or not all(
                isinstance(n, str)
                and "::" in n
                and ordinary(n.split("::")[0])
                and n.startswith("tests/")
                and n.split("::")[0].endswith(".py")
                for n in selectors
            )
        ):
            raise ValueError("Freeze explicit focused pytest node identifiers")
        if not 1 <= timeout_s <= 30:
            raise ValueError("Test timeout exceeds the focused limit")
        self.image, self.selectors, self.output, self.timeout_s = (
            image,
            selectors,
            output,
            timeout_s,
        )
        self.docker_binary = docker_binary

    def command(self, workspace: Path, results: Path, name: str, owner: str) -> list[str]:
        return [
            self.docker_binary,
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            "loom.factory.run=" + owner,
            "--log-driver=none",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=512m",
            "--cpus=1",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount",
            f"type=bind,source={workspace},target=/workspace,readonly",
            "--mount",
            f"type=bind,source={results},target=/results",
            "--workdir=/workspace",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_ADDOPTS=",
            "--env=PYTHONPATH=/workspace/src",
            self.image,
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_family=legacy",
            "--junitxml=/results/junit.xml",
            *self.selectors,
        ]

    def run(self, workspace: Path) -> dict:
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        results = self.output / "results"
        results.mkdir()
        config = self.output / "docker-config"
        config.mkdir(mode=0o700)
        env = {
            "PATH": os.defpath,
            "DOCKER_CONFIG": str(config),
            "HOME": str(config),
            "LANG": "C.UTF-8",
        }
        owner = uuid.uuid4().hex
        name = "loom-factory-" + owner
        command = self.command(workspace.resolve(strict=True), results.resolve(), name, owner)
        (self.output / "command.json").write_text(json.dumps(command))
        receipt = self.output / "factory-container.json"
        receipt.write_text(
            json.dumps(
                {
                    "kind": "factory-container-ownership-v1",
                    "name": name,
                    "owner": owner,
                    "cleaned": False,
                }
            )
        )
        started = time.monotonic()
        try:
            subprocess.run(
                command, env=env, capture_output=True, timeout=DOCKER_CONTROL_TIMEOUT_S, check=True
            )
            # The container log driver is disabled so a candidate cannot fill shared logs.
            with (
                (self.output / "stdout").open("wb") as stdout,
                (self.output / "stderr").open("wb") as stderr,
            ):
                run = subprocess.run(
                    [self.docker_binary, "start", "--attach", name],
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=self.timeout_s,
                    preexec_fn=bound_output,
                    check=False,
                )
            statuses = selected_statuses(junit_statuses(results / "junit.xml"), self.selectors)
            inspected = subprocess.run(
                [self.docker_binary, "inspect", "--format", "{{json .State}}", name],
                env=env,
                capture_output=True,
                text=True,
                timeout=DOCKER_CONTROL_TIMEOUT_S,
                check=True,
            )
            state = json.loads(inspected.stdout)
            (self.output / "runtime.json").write_text(
                json.dumps({"transport_exit_code": run.returncode, "state": state}, indent=2) + "\n"
            )
            if (
                type(state.get("ExitCode")) is not int
                or run.returncode not in (0, state["ExitCode"])
                or state.get("Running") is not False
                or state.get("OOMKilled") is not False
                or state.get("Error")
            ):
                raise RuntimeError("Test transport or container runtime failed")
            exit_code = state["ExitCode"]
            return {
                "passed": exit_code == 0 and all(v == "PASSED" for v in statuses.values()),
                "selected": statuses,
                "exit_code": exit_code,
                "transport_exit_code": run.returncode,
                "duration_s": time.monotonic() - started,
                "image": self.image,
            }
        finally:
            remove_owned(receipt, docker_binary=self.docker_binary)


class HistoricalGrader:
    """Hold tests outside the agent checkout and overlay only allowlisted source.

    Hidden inputs are supplied by a trusted controller and never returned to the
    agent. Qualification runs base/reference controls before any model cohort.
    """

    def __init__(
        self,
        source: FrozenSource,
        image: str,
        visible: list[str],
        hidden: list[str],
        hidden_test_patch: str,
        output: Path,
        *,
        max_visible_calls: int | None = 3,
        docker_binary: str = "docker",
    ):
        self.source, self.image, self.visible, self.hidden = source, image, visible, hidden
        self.hidden_test_patch, self.output = hidden_test_patch, output
        self.base = source.files()
        self.max_visible_calls, self.calls = max_visible_calls, 0
        self.docker_binary = docker_binary
        self.sha256 = digest(
            {
                "base": source.commit,
                "files": digest(self.base),
                "image": image,
                "visible": visible,
                "hidden": hidden,
                "patch": hidden_test_patch,
                "max_visible_calls": max_visible_calls,
            }
        )

    def run(self, checkout: Checkout, *, hidden: bool = False) -> dict:
        if checkout.original != {k: v for k, v in self.base.items() if k.startswith("src/flask/")}:
            raise PermissionError("Candidate base differs from the frozen implementation")
        if (
            not hidden
            and self.max_visible_calls is not None
            and self.calls >= self.max_visible_calls
        ):
            raise TimeoutError("Visible test budget exhausted")
        if not hidden:
            self.calls += 1
        out = self.output / ("hidden" if hidden else f"visible-{self.calls}")
        workspace = out / "workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        for name, content in {**self.base, **checkout.files()}.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if hidden:
            headers = [
                line.split()
                for line in self.hidden_test_patch.splitlines()
                if line.startswith("diff --git ")
            ]
            if not headers or any(
                len(parts) != 4
                or not all(
                    p[:2] in {"a/", "b/"} and ordinary(p[2:]) and p[2:].startswith("tests/")
                    for p in parts[2:]
                )
                for parts in headers
            ):
                raise PermissionError("Hidden patch may only change fixed test files")
            for record in git(
                workspace, "apply", "--numstat", "-z", "-", data=self.hidden_test_patch.encode()
            ).split(b"\0"):
                if record:
                    fields = record.decode().split("\t")
                    if (
                        len(fields) != 3
                        or not ordinary(fields[2])
                        or not fields[2].startswith("tests/")
                    ):
                        raise PermissionError("Hidden patch changes an evaluation-external file")
            git(workspace, "apply", "-", data=self.hidden_test_patch.encode())
        runner = DockerTests(
            self.image,
            self.hidden if hidden else self.visible,
            out / "test",
            docker_binary=self.docker_binary,
        )
        result = runner.run(workspace)
        if hidden:
            result = {k: v for k, v in result.items() if k != "selected"}
        return {**result, "state": checkout.state(), "grader_sha256": self.sha256}
