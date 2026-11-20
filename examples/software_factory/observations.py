"""Bounded stdout-only observations from an isolated candidate process."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from .cleanup import remove_owned
from .historical import bound_output, container_prefix


class CandidateFailure(RuntimeError):
    """A bounded candidate execution failed; the host oracle still works."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail[:4096]
        super().__init__(self.detail)


class DockerObservations:
    def __init__(self, image: str, output: Path):
        self.image, self.output = image, output

    def command(self, workspace: Path, name: str, owner: str) -> list[str]:
        bridge = Path(__file__).with_name("bridge.py").resolve(strict=True)
        return container_prefix(self.image, workspace, name, owner) + [
            "--interactive",
            "--mount",
            f"type=bind,source={bridge},target=/bridge.py,readonly",
            self.image,
            "python",
            "/bridge.py",
        ]

    def run(self, workspace: Path, requests: list[dict]) -> bytes:
        payload = json.dumps(requests, allow_nan=False).encode()
        if len(payload) > 65536 or not 1 <= len(requests) <= 32:
            raise ValueError("Observation input budget exceeded")
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        config = self.output / "docker-config"
        config.mkdir(mode=0o700)
        env = {"PATH": os.defpath, "DOCKER_CONFIG": str(config), "HOME": str(config)}
        owner = uuid.uuid4().hex
        name = "loom-factory-" + owner
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
        command = self.command(workspace.resolve(strict=True), name, owner)
        (self.output / "command.json").write_text(json.dumps(command))
        (self.output / "requests.json").write_bytes(payload)
        started = time.monotonic()
        try:
            subprocess.run(command, env=env, capture_output=True, timeout=10, check=True)
            with (
                (self.output / "stdout").open("wb") as stdout,
                (self.output / "stderr").open("wb") as stderr,
            ):
                try:
                    run = subprocess.run(
                        ["docker", "start", "--attach", "--interactive", name],
                        input=payload,
                        env=env,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=30,
                        preexec_fn=bound_output,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    (self.output / "runtime.json").write_text(
                        json.dumps(
                            {
                                "candidate_timeout_s": 30,
                                "elapsed_s": time.monotonic() - started,
                            }
                        )
                        + "\n"
                    )
                    raise CandidateFailure(
                        "candidate_timeout", "Candidate exceeded30seconds"
                    ) from error
            sizes = {
                stream: (self.output / stream).stat().st_size for stream in ("stdout", "stderr")
            }
            # Overflow can stop attach before container exit; cleanup still runs below.
            if any(size > 65536 for size in sizes.values()):
                (self.output / "runtime.json").write_text(
                    json.dumps(
                        {
                            "transport_exit_code": run.returncode,
                            "output_bytes": sizes,
                            "elapsed_s": time.monotonic() - started,
                        }
                    )
                    + "\n"
                )
                raise CandidateFailure("candidate_output_limit", "Observation stream exceeds64KiB")
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", name],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            state = json.loads(inspected.stdout)
            (self.output / "runtime.json").write_text(
                json.dumps(
                    {
                        "transport_exit_code": run.returncode,
                        "state": state,
                        "elapsed_s": time.monotonic() - started,
                    },
                    indent=2,
                )
                + "\n"
            )
            if (
                type(state.get("ExitCode")) is not int
                or run.returncode not in (0, state["ExitCode"])
                or state.get("Running") is not False
                or type(state.get("OOMKilled")) is not bool
                or state.get("Error")
            ):
                raise RuntimeError("Candidate observation transport or state is invalid")
            if state["ExitCode"] != 0 or state["OOMKilled"]:
                detail = (self.output / "stderr").read_bytes()[-4096:].decode(errors="replace")
                raise CandidateFailure("candidate_exit", f"Exit{state['ExitCode']}: {detail}")
            output = self.output / "stdout"
            return output.read_bytes()
        finally:
            remove_owned(receipt)
