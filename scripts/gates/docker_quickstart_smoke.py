#!/usr/bin/env python3
"""Smoke the released Compose/installer path in an isolated, disposable stack.

    python scripts/gates/docker_quickstart_smoke.py --image loom:docker-smoke \
        --extractor-image loom-extractor:docker-smoke

Only this run's generated Compose project and volumes are removed on exit.
The script uses stdlib only and can run on a fresh CI runner without a Loom venv.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.build_stack_bundle import build  # noqa: E402


def request(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def run(*args: str, **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Replace managed entries without adding ambiguous duplicate dotenv keys."""
    pending = dict(updates)
    lines = []
    for line in path.read_text().splitlines():
        key = line.partition("=")[0]
        if key in updates:
            if key in pending:
                lines.append(f"{key}={pending.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in pending.items())
    path.write_text("\n".join(lines) + "\n")


def provider_fixture(image: str, root: Path) -> Path:
    """A network-only deterministic provider; no published ports or real keys."""
    override = root / "provider-smoke.json"
    source = ROOT / "scripts/gates/docker_provider_server.py"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "provider-smoke": {
                        "image": image,
                        "entrypoint": ["python", "/provider-smoke.py"],
                        "volumes": [f"{source}:/provider-smoke.py:ro"],
                        "healthcheck": {
                            "test": [
                                "CMD",
                                "python",
                                "-c",
                                "import urllib.request; urllib.request.urlopen('http://localhost:8080/health',timeout=3)",
                            ],
                            "interval": "2s",
                            "timeout": "5s",
                            "retries": 20,
                        },
                    },
                    "loom": {"depends_on": {"provider-smoke": {"condition": "service_healthy"}}},
                }
            }
        )
    )
    return override


def latest_release() -> str:
    """The exact image `clickhouse/clickhouse-server:latest` names now."""
    script = ROOT / "dev" / "clickhouse_release.sh"
    return subprocess.run(
        ["bash", str(script)], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def smoke(image: str, extractor_image: str, clickhouse_image: str) -> None:
    with tempfile.TemporaryDirectory(prefix="loom-docker-smoke-") as temp:
        root = Path(temp)
        archive, checksum = build("smoke", root / "bundle", clickhouse_image)
        stack = root / "stack"
        env = {
            **os.environ,
            "LOOM_INSTALL_DIR": str(stack),
            "LOOM_INSTALL_BUNDLE": str(archive),
            "LOOM_INSTALL_CHECKSUM": str(checksum),
            "LOOM_SKIP_START": "1",
        }
        run("sh", str(ROOT / "install.sh"), env=env)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        repository, tag = image.rsplit(":", 1)
        extractor_repository, extractor_tag = extractor_image.rsplit(":", 1)
        # compose.yaml runs both images at LOOM_VERSION.
        assert extractor_tag == tag, f"{extractor_image} is not tagged {tag}, as {image} is"
        update_env(
            stack / ".env",
            {
                "LOOM_IMAGE_REPOSITORY": repository,
                "LOOM_EXTRACTOR_IMAGE_REPOSITORY": extractor_repository,
                "LOOM_VERSION": tag,
                "LOOM_PORT": str(port),
            },
        )
        compose = [
            "docker",
            "compose",
            "--project-directory",
            str(stack),
            "--env-file",
            str(stack / ".env"),
            "-f",
            str(stack / "compose.yaml"),
            "-f",
            str(provider_fixture(image, root)),
        ]
        base = f"http://127.0.0.1:{port}"
        identity = {"org": "dev", "namespace": "local", "agent": "docker-smoke"}
        try:
            # Match the installer's pull step, including the selected CPU architecture.
            # Loom itself is already built locally or published by the release job.
            run(*compose, "pull", "clickhouse")
            run(*compose, "up", "-d", "--wait", "--wait-timeout", "240")
            health = request(base, "/v1/health")
            assert health["status"] == "ok", health
            assert not health.get("schema", {}).get("pending"), health
            with urllib.request.urlopen(base + "/", timeout=10) as response:
                assert response.status == 200 and b"<html" in response.read()
            request(
                base,
                "/v1/memory.set",
                {**identity, "memory_key": "docker/smoke", "value": "Docker memory persists"},
            )
            saved = request(base, "/v1/memory.get", {**identity, "memory_key": "docker/smoke"})
            assert saved["found"] and saved["value"] == "Docker memory persists", saved
            extractor = (ROOT / "scripts/gates/docker_extractor_smoke.py").read_text()
            run(*compose, "exec", "-T", "loom", "python", "-", input=extractor, text=True)
            # The extractor's network is internal: it reaches nothing outside.
            egress = subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "extractor",
                    "python",
                    "-c",
                    "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)",
                ],
                check=False,
            )
            assert egress.returncode != 0, "the extractor reached the internet"
            code = (ROOT / "scripts/gates/docker_code_smoke.py").read_text()
            run(*compose, "exec", "-T", "loom", "python", "-", input=code, text=True)
            run(*compose, "restart", "loom")
            run(*compose, "up", "-d", "--wait", "--wait-timeout", "120")
            saved = request(base, "/v1/memory.get", {**identity, "memory_key": "docker/smoke"})
            assert saved["found"] and saved["value"] == "Docker memory persists", saved
            run(
                *compose,
                "exec",
                "-T",
                "loom",
                "python",
                "-",
                "--verify-only",
                input=code,
                text=True,
            )
            # Exercise the documented environment update path without paid API
            # calls. This proves recreation/persistence, not provider quality.
            update_env(stack / ".env", {"LOOM_LLM_MODEL": "fake-echo-docker-update"})
            run(*compose, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120", "loom")
            models = request(base, "/v1/admin/models")
            assert models["llm_model"] == "fake-echo-docker-update", models
            saved = request(base, "/v1/memory.get", {**identity, "memory_key": "docker/smoke"})
            assert saved["found"] and saved["value"] == "Docker memory persists", saved
            from scripts.gates.docker_provider_smoke import smoke_providers

            smoke_providers(base, compose)
            print(
                "PASS Docker quickstart: healthy, dashboard, memory, extractor, "
                "compiler indexing, provider rebuild/recovery, restart persistence, "
                "environment update/recreation"
            )
        except BaseException:
            subprocess.run([*compose, "logs", "--tail", "100"], check=False)
            raise
        finally:
            subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="A built or published image repository:tag")
    parser.add_argument(
        "--extractor-image", required=True, help="the extractor image, tagged as --image is"
    )
    parser.add_argument(
        "--clickhouse-image", help="the release the bundle names; default: the latest, resolved now"
    )
    args = parser.parse_args()
    smoke(args.image, args.extractor_image, args.clickhouse_image or latest_release())
