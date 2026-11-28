"""Run the joined Compose deployment on an ephemeral GitHub-hosted Linux runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixture.py").resolve()
WORKER = "source-worker"
PROXY = "source-worker-proxy"
SOCKET_DIR = "/run/loom-source-worker/socket"
CONFIG_DIR = "/etc/loom-source-worker"
CREDENTIAL_DIR = "/run/loom-source-worker/credentials"


def command(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, text=True, capture_output=capture, timeout=360)
    if check and result.returncode != 0:
        raise RuntimeError(f"{args[:2]} exited with code {result.returncode}")
    return result


def output(*args: str) -> str:
    return command(*args, capture=True).stdout.strip()


def inspect(kind: str, name: str) -> dict:
    return json.loads(output("docker", kind, "inspect", name))[0]


def assert_service(
    container: dict, network: str, own_mounts: dict[str, Path], *, proxy: bool
) -> None:
    config, host = container["Config"], container["HostConfig"]
    assert container["State"]["Running"], "service is not running"
    assert config["User"] == "10001:10001", "service UID/GID changed"
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}.intersection(
        host["SecurityOpt"]
    )
    assert not host["PortBindings"] and not host["PublishAllPorts"], "published ports"
    assert set(container["NetworkSettings"]["Networks"]) == {network}
    mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
    if proxy:
        assert set(host["Tmpfs"]) == {"/tmp"}
        if "/tmp" in mounts:
            assert mounts.pop("/tmp")["Type"] == "tmpfs"
    assert set(mounts) == own_mounts.keys() | {SOCKET_DIR}, "unexpected service mounts"
    for destination, source in own_mounts.items():
        mount = mounts[destination]
        assert not mount["RW"] and mount["Type"] == "bind" and mount["Source"] == str(source)
    assert mounts[SOCKET_DIR]["Type"] == "volume" and mounts[SOCKET_DIR]["RW"] != proxy
    if proxy:
        assert host["LogConfig"]["Type"] == "none", "proxy logging is enabled"


def qualify(root: Path) -> None:
    project = root.name.replace("_", "-")
    worker_image = f"{project}-{WORKER}"
    kv_name = f"{project}-kv"
    caller_name = f"{project}-caller"
    setup_name = f"{project}-setup"
    ingress, egress = f"{project}_worker-ingress", f"{project}_worker-egress"
    volume = f"{project}_worker-socket"
    source = root / "source_worker"
    source.mkdir()
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "compose.yaml", "proxy.conf"):
        shutil.copyfile(SOURCE / name, source / name)
    shutil.copytree(SOURCE / "loom_source_worker", source / "loom_source_worker")

    compose = (
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        project,
        "-f",
        str(source / "compose.yaml"),
    )
    enabled = (*compose, "--profile", "source-worker")
    operator = source / "operator"
    helper = (
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--mount",
        f"type=bind,src={FIXTURE},dst=/fixture.py,readonly",
        "--entrypoint",
        "python",
    )

    def start_caller(mode: str) -> None:
        command(
            *helper,
            "--name",
            caller_name,
            "--network",
            ingress,
            "--mount",
            f"type=bind,src={root / 'caller'},dst=/caller,readonly",
            "--mount",
            f"type=bind,src={root / 'signals'},dst=/signals,readonly",
            worker_image,
            "-IBu",
            "/fixture.py",
            mode,
        )

    try:
        disabled = json.loads(output(*compose, "config", "--format", "json"))
        active = json.loads(output(*enabled, "config", "--format", "json"))
        assert not disabled.get("services"), "worker profile is enabled by default"
        assert set(active["services"]) == {WORKER, PROXY}, "unexpected enabled services"
        proxy_image = active["services"][PROXY]["image"]
        assert re.fullmatch(r"nginx:[^@]+@sha256:[0-9a-f]{64}", proxy_image), "proxy is not pinned"
        command(*enabled, "build", "--pull", WORKER)
        command(*enabled, "pull", PROXY)
        for image in (worker_image, proxy_image):
            info = inspect("image", image)
            print(
                f"{image}: {info['Os']}/{info['Architecture']} {info['Id']} {info['RepoDigests']}"
            )

        command(
            *helper,
            "--name",
            setup_name,
            "--network",
            "none",
            "--mount",
            f"type=bind,src={root},dst=/work",
            worker_image,
            "-IBu",
            "/fixture.py",
            "prepare",
        )
        command(
            "sudo",
            "chown",
            "root:10001",
            str(operator / "worker/credentials/vault-token"),
            str(operator / "proxy/server-key.pem"),
        )
        command(*enabled, "up", "-d", "--no-build", "--pull", "never")
        command(
            *helper,
            "-d",
            "--name",
            kv_name,
            "--network",
            egress,
            "--network-alias",
            "synthetic-kv",
            "--mount",
            f"type=bind,src={root / 'kv'},dst=/kv,readonly",
            "--mount",
            f"type=bind,src={root / 'signals'},dst=/signals",
            worker_image,
            "-IBu",
            "/fixture.py",
            "serve",
        )

        ingress_info = inspect("network", ingress)
        assert ingress_info["Internal"] and not ingress_info["EnableIPv6"]
        assert ingress_info["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"
        worker_id, proxy_id = (output(*enabled, "ps", "-q", service) for service in (WORKER, PROXY))
        worker, proxy = (inspect("container", name) for name in (worker_id, proxy_id))
        assert_service(
            worker,
            egress,
            {
                CONFIG_DIR: operator / "worker/config",
                CREDENTIAL_DIR: operator / "worker/credentials",
            },
            proxy=False,
        )
        assert_service(
            proxy,
            ingress,
            {
                "/etc/nginx/nginx.conf": source / "proxy.conf",
                **{
                    f"/etc/nginx/tls/{name}": operator / "proxy" / name
                    for name in ("server-chain.pem", "server-key.pem", "client-ca.pem")
                },
            },
            proxy=True,
        )
        assert all(
            next(mount for mount in service["Mounts"] if mount["Destination"] == SOCKET_DIR)["Name"]
            == volume
            for service in (worker, proxy)
        ), "services do not share exactly the owned socket volume"

        start_caller("probe")
        proxy_address = proxy["NetworkSettings"]["Networks"][ingress]["IPAddress"]
        command(
            "docker", "exec", kv_name, "python", "-IB", "/fixture.py", "isolated", proxy_address
        )
        kv_inputs = json.loads((root / "kv/inputs.json").read_text())
        for name in ("vault_token", "upstream_token"):
            assert all(kv_inputs[name] not in json.dumps(service) for service in (worker, proxy))
        listeners = "".join(
            output("docker", "exec", worker_id, "cat", path) + "\n"
            for path in ("/proc/net/tcp", "/proc/net/tcp6")
        )
        assert not any(
            fields[3] == "0A" and fields[1].split(":")[0] != "0B00007F"
            for line in listeners.splitlines()
            if (fields := line.split()) and fields[0] != "sl"
        ), "worker has a TCP listener other than the Docker DNS stub"
        assert (
            output("docker", "exec", proxy_id, "stat", "-c", "%a:%u:%g", SOCKET_DIR)
            == "700:10001:10001"
        )
        socket_path = f"{SOCKET_DIR}/worker.sock"
        command("docker", "exec", proxy_id, "test", "-S", socket_path)
        assert (
            output("docker", "exec", proxy_id, "stat", "-c", "%a:%u:%g", socket_path)
            == "600:10001:10001"
        )
        command(*enabled, "stop", WORKER)
        command("docker", "exec", proxy_id, "test", "!", "-e", socket_path)
        start_caller("stopped")
        command(*enabled, "start", WORKER)
        start_caller("ready")
        print("PASS: profile, runtime isolation, socket ownership, stop and restart")
    finally:
        for name in (caller_name, kv_name, setup_name):
            command("docker", "rm", "-f", name, capture=True, check=False)
        command(*enabled, "down", "--volumes")
        assert not output(*enabled, "ps", "--all", "--quiet"), "Compose project containers remain"
        for kind, name in (("network", ingress), ("network", egress), ("volume", volume)):
            assert (
                command("docker", kind, "inspect", name, capture=True, check=False).returncode != 0
            )
        for name in (caller_name, kv_name, setup_name):
            assert (
                command(
                    "docker", "container", "inspect", name, capture=True, check=False
                ).returncode
                != 0
            )
        command("docker", "image", "rm", worker_image, capture=True, check=False)


def main() -> None:
    assert (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    )
    sha = os.environ["QUALIFICATION_SHA"]
    assert re.fullmatch(r"[0-9a-f]{40}", sha) and output("git", "rev-parse", "HEAD") == sha
    assert not os.environ.get("COMPOSE_PROFILES"), "unexpected active profiles"
    print(
        f"Checkout {sha}; runner {os.environ['RUNNER_OS']}/{os.environ['RUNNER_ARCH']}; "
        f"image {os.environ.get('ImageOS')}/{os.environ.get('ImageVersion')}"
    )
    command("docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}")
    command("docker", "compose", "version")
    with tempfile.TemporaryDirectory(prefix="mcp-deploy-", dir=os.environ["RUNNER_TEMP"]) as work:
        qualify(Path(work))


if __name__ == "__main__":
    main()
