"""Hosted private-source CLI qualification against the normally installed server and SDK.

The functional qualification owner supplies a real admitted private MCP source and
its expected content marker. This file is selected explicitly after that fixture is
provisioned; it is not part of local unit discovery. Missing setup fails the run.
"""

import json
import os
import subprocess
import sys
from uuid import uuid4

from loom.cli_context import CONTEXT_ENV, session_environment
from loom.session_protection import SessionProtection
from loom.source_cli import save_session_pointer

ENTRY = "from server.cli.app import main; raise SystemExit(main())"
LEGACY_ENTRY = """
import json
import os
import sys
from server.cli._client import post_bytes, post_json, post_op
from server.cli._session_context import CLIContextError
mode, base, body = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
token = os.environ['LOOM_API_KEY']
try:
    if mode == 'file':
        post_bytes(base + '/v1/knowledge.upload', b'blocked-file', token)
    elif mode == 'json':
        post_json(base + '/v1/memory.set', body, token)
    else:
        post_op(base, 'memory.set', body, token)
except CLIContextError:
    print('protection refused', file=sys.stderr)
    raise SystemExit(2)
raise SystemExit('protected legacy request was not suppressed')
"""


def cli(env, *argv):
    return subprocess.run(
        [sys.executable, "-c", ENTRY, *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_actual_private_source_then_other_command_and_explicit_write(tmp_path):
    base = os.environ["LOOM_CLI_QUAL_BASE_URL"]
    org = os.environ["LOOM_CLI_QUAL_ORG"]
    token = os.environ["LOOM_CLI_QUAL_TOKEN"]
    other_token = os.environ["LOOM_CLI_QUAL_OTHER_TOKEN"]
    destination = os.environ["LOOM_CLI_QUAL_WRITE_NAMESPACE"]
    request = json.loads(os.environ["LOOM_CLI_QUAL_PRIVATE_READ_REQUEST"])
    marker = os.environ["LOOM_CLI_QUAL_PRIVATE_MARKER"]
    assert request["namespace"] != destination
    assert other_token != token
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    family = SessionProtection.create(base=root / "families")
    pointer = root / "pointer.json"
    save_session_pointer(pointer, family)
    env = {**os.environ, **session_environment(str(pointer)), "LOOM_API_KEY": token}
    source = root / "read.json"
    source.write_text(json.dumps(request))
    read = cli(
        env,
        "knowledge",
        "source",
        "read",
        "--server-url",
        base,
        "--org",
        org,
        "--session-file",
        str(pointer),
        "--request-file",
        str(source),
    )
    assert read.returncode == 0, read.stderr
    assert marker in json.dumps(json.loads(read.stdout))
    assert family.protected
    assert SessionProtection.open(family.pointer()).protected

    key = "cli-context-" + uuid4().hex
    fields = ["org=" + org, "namespace=" + destination, "key=" + key, "value=explicit-user-value"]
    # A separate process uses the same normal root CLI and a different installed client path.
    denied = cli(
        {**env, "LOOM_API_KEY": other_token}, "call", "memory.set", "--server", base, *fields
    )
    assert denied.returncode == 2 and "protection refused" in denied.stderr
    assert marker not in denied.stderr and marker not in denied.stdout
    # An unreachable origin distinguishes local suppression from an HTTP/auth failure.
    origin = cli(
        {**env, "LOOM_API_KEY": other_token},
        "call",
        "memory.set",
        "--server",
        "https://127.0.0.1:1",
        *fields,
    )
    assert origin.returncode == 2 and "protection refused" in origin.stderr
    for mode in ("file", "json", "op"):
        legacy = subprocess.run(
            [
                sys.executable,
                "-c",
                LEGACY_ENTRY,
                mode,
                "https://127.0.0.1:1",
                json.dumps(
                    {"org": org, "namespace": destination, "key": key, "value": "blocked-legacy"}
                ),
            ],
            env={**env, "LOOM_API_KEY": other_token},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert legacy.returncode == 2 and "protection refused" in legacy.stderr
    ordinary_read = cli(
        env,
        "call",
        "memory.keys",
        "--server",
        base,
        "org=" + org,
        "namespace=" + destination,
    )
    assert ordinary_read.returncode == 0, ordinary_read.stderr
    assert key not in ordinary_read.stdout

    positive = cli(
        env, "--request-intent", "explicit", "call", "memory.set", "--server", base, *fields
    )
    assert positive.returncode == 0, positive.stderr
    verify = cli(
        env,
        "call",
        "memory.get",
        "--server",
        base,
        "org=" + org,
        "namespace=" + destination,
        "key=" + key,
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["value"] == "explicit-user-value"
    # Explicit intent ended with that process; the unchanged capsule remains protected.
    again = cli(env, "call", "memory.set", "--server", base, *fields)
    assert again.returncode == 2 and "protection refused" in again.stderr

    original_capsule = env[CONTEXT_ENV]
    pointer.unlink()
    missing = cli(
        env,
        "call",
        "memory.get",
        "--server",
        base,
        "org=" + org,
        "namespace=" + destination,
        "key=" + key,
    )
    assert missing.returncode == 2 and not pointer.exists()
    assert env[CONTEXT_ENV] == original_capsule
