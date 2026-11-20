"""Run the placement drill's shell wiring without a container daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PRIMARY = "loom-scale-place-drill-pg"
STUB = r"""
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "args": args,
                             "dsn": os.environ.get("LOOM_CELL_PLACE_DRILL_DSN")}) + "\n")
if tool == "pytest":
    sys.exit(int(os.environ["PYTEST_EXIT"]))
if args[:2] in (["container", "inspect"], ["network", "inspect"]):
    sys.exit(0 if args[0] == os.environ["EXISTING_RESOURCE"] else 1)
if args[0] == "run" and "127.0.0.1:55432:5432" in args:
    print("failed to bind host port 127.0.0.1:55432/tcp: address already in use", file=sys.stderr)
    sys.exit(125)
if args[0] == "port":
    print(os.environ["PORT_BINDING"])
    sys.exit(int(os.environ["MAPPING_EXIT"]))
if args[0] == "exec" and "pg_stat_replication" in " ".join(args):
    print("sync")
"""


def _run_step(tmp_path, binding, *, pytest_exit=0, mapping_exit=0, existing=""):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["integration-postgres"]["steps"]
    step = next(
        s
        for s in steps
        if s.get("name") == "pytest placement durability on a private physical standby"
    )
    for name in ("docker", "pytest"):
        stub = tmp_path / name
        stub.write_text(f"#!{sys.executable}\n{STUB}")
        stub.chmod(0o700)
    log = tmp_path / "commands.jsonl"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            **{key: str(value) for key, value in step["env"].items()},
            "PATH": f"{tmp_path}:{os.defpath}",
            "COMMAND_LOG": str(log),
            "PORT_BINDING": binding,
            "PYTEST_EXIT": str(pytest_exit),
            "MAPPING_EXIT": str(mapping_exit),
            "EXISTING_RESOURCE": existing,
        },
        text=True,
        capture_output=True,
        timeout=20,
    )
    return result, [json.loads(line) for line in log.read_text().splitlines()]


def _assert_cleanup(commands):
    docker = [c["args"] for c in commands if c["tool"] == "docker"]
    assert ["rm", "-f", "-v", PRIMARY, f"{PRIMARY}-standby"] in docker
    assert ["network", "rm", f"{PRIMARY}-net"] in docker


@pytest.mark.parametrize("port", [1, 43127, 65535])
def test_placement_drill_uses_its_assigned_loopback_port(tmp_path, port):
    result, commands = _run_step(tmp_path, f"127.0.0.1:{port}")
    assert result.returncode == 0, result.stderr
    primary = next(c["args"] for c in commands if c["args"][:1] == ["run"])
    assert primary[primary.index("-p") + 1] == "127.0.0.1:0:5432"
    assert any(c["args"] == ["port", PRIMARY, "5432/tcp"] for c in commands)
    tests = [c for c in commands if c["tool"] == "pytest"]
    assert len(tests) == 1
    assert tests[0]["args"] == ["-q", "tests/integration/test_cell_place_durability.py"]
    assert tests[0]["dsn"] == f"postgres://loom:loomdev@127.0.0.1:{port}/loom"
    _assert_cleanup(commands)


@pytest.mark.parametrize(
    "binding",
    [
        "",
        "0.0.0.0:43127",
        "[::1]:43127",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:043127",
        "127.0.0.1:nope",
        "127.0.0.1:43127\n127.0.0.1:43128",
    ],
)
def test_placement_drill_rejects_unusable_mappings_before_pytest(tmp_path, binding):
    result, commands = _run_step(tmp_path, binding)
    assert result.returncode != 0
    assert "invalid placement drill loopback mapping" in result.stderr
    assert not any(c["tool"] == "pytest" for c in commands)
    _assert_cleanup(commands)


def test_placement_drill_preserves_pytest_failure_and_cleans_up(tmp_path):
    result, commands = _run_step(tmp_path, "127.0.0.1:43127", pytest_exit=42)
    assert result.returncode == 42
    _assert_cleanup(commands)


def test_placement_drill_mapping_lookup_failure_cleans_up(tmp_path):
    result, commands = _run_step(tmp_path, "127.0.0.1:43127", mapping_exit=7)
    assert result.returncode == 7
    assert not any(c["tool"] == "pytest" for c in commands)
    _assert_cleanup(commands)


@pytest.mark.parametrize("existing", ["container", "network"])
def test_placement_drill_keeps_existing_resource_guards(tmp_path, existing):
    result, commands = _run_step(tmp_path, "127.0.0.1:43127", existing=existing)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not any(c["tool"] == "pytest" or c["args"][0] in {"run", "rm"} for c in commands)
