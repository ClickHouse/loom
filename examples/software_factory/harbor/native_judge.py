"""Run the blinded assessment with the pinned Claude Code agent image."""

import json
import math
import os
import subprocess
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path

from .provision import OWNER_LABEL, Provisioner
from .runtime import read


@contextmanager
def judge(protocol, task_id, sections, schema):
    settings = protocol["judge"]
    model = settings["model_id"]
    if settings["runtime"] != "claude-code" or model != protocol["model_id"]:
        raise PermissionError("The judge runtime and model must match the frozen protocol")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise PermissionError("The judge requires the run's Anthropic credential")
    output = Path(protocol["output_root"]).resolve().parent
    launch = read(output / "launch.json")
    if launch["kind"] != "loom-factory-launch" or launch["output_root"] != str(output):
        raise PermissionError("The judge requires the recorded factory run")
    resources = Provisioner(Path(launch["source_root"]), output, launch["owner"])
    image = protocol["harbor_images"][task_id]["agent"]["baseline"]
    resources._owned("image", image)
    name = resources.prefix + "-judge-" + uuid.uuid4().hex[:12]
    container = resources.create_container(
        name,
        [
            "docker",
            "create",
            "--interactive",
            "--name",
            name,
            "--label",
            f"{OWNER_LABEL}={resources.owner}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--workdir",
            "/tmp",
            "--user",
            "agent",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env",
            "HOME=/tmp",
            "--env",
            "CLAUDE_CONFIG_DIR=/tmp/.claude",
            "--env",
            "ANTHROPIC_API_KEY",
            "--env",
            "DISABLE_PROMPT_CACHING=1",
            "--env",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "--entrypoint",
            "/home/agent/.local/bin/claude",
            image,
            "--bare",
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--model",
            model,
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            sections[0]["text"],
        ],
    )
    prompt = "\n\n".join(section["text"] for section in sections[1:])
    try:
        result = resources._cmd(
            ["docker", "start", "--attach", "--interactive", container], data=prompt
        )
        yield _receipt(json.loads(result.stdout), model)
    finally:
        try:
            resources.remove_container(name, container)
        except (OSError, subprocess.CalledProcessError):
            warnings.warn("Judge container remains in the run cleanup journal", stacklevel=2)


def _receipt(response, model):
    cost = response.get("total_cost_usd")
    if (
        response.get("type") != "result"
        or response.get("subtype") != "success"
        or response.get("is_error") is not False
        or set(response.get("modelUsage") or {}) != {model}
        or type(cost) not in (int, float)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("Claude Code returned no qualified judge receipt")
    return {
        "structured_output": response.get("structured_output"),
        "generation_cost_usd": cost,
        "usage": response.get("usage"),
        "model": model,
        "duration_api_ms": response.get("duration_api_ms"),
        "num_turns": response.get("num_turns"),
    }
