#!/usr/bin/env python3
"""Run the one-line installer, `curl -fsSL …/releases/latest/download/install.sh | sh`,
in clean Linux containers: install, a memory round trip, a re-run, a refused
downgrade, and an upgrade from the previous release.

A rehearsal serves a release's own files (`release/stack/`, as the release
workflow builds them) over HTTPS from this host, laid out as GitHub serves them
(`releases/latest/download/*` redirects to `releases/download/vX.Y.Z/*`), and
the images from a registry it starts on loopback. The upgrade installs the
previous release with that release's own install.sh and bundle
(scripts/gates/previous_release_files.sh), then runs the one-liner over it:

    python3 scripts/gates/installer_rehearsal.py --release-dir release/stack \\
        --upgrade --image loom:smoke --extractor-image loom-extractor:smoke

`--published` runs the real one-liner against github.com and ghcr.io instead,
signed in to nothing, for a release that is out (`--previous-version` names the
published release to upgrade from).

Each distribution gets an image of itself with curl and a Docker CLI, the
installer's prerequisites; the host's Compose plugin is mounted in, and the
installer runs as this host's user against this host's Docker daemon. The
upgrade runs on the first distribution only. The containers, stacks, registry
and image tags this run creates are removed by their exact names on exit;
images it pulls stay. Standard library only, so it runs on a fresh CI runner.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release import prepare  # noqa: E402
from scripts.release.build_stack_bundle import (  # noqa: E402
    VERSION_PATTERN,
    pinned_image,
    read_member,
)

RELEASES_PATH = "/ClickHouse/loom/releases"
SCRIPTS = ROOT / "scripts" / "gates"
GITHUB_RELEASES = f"https://github.com{RELEASES_PATH}"
# Each distribution's packages for curl and its packaged Docker CLI.
_APT = "apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates"
PREPARE = {
    "debian:trixie-slim": f"{_APT} docker-cli >/dev/null",
    "ubuntu:24.04": f"{_APT} docker.io >/dev/null",
    "alpine:3.22": "apk add -q --no-cache curl ca-certificates docker-cli",
}
DISTROS = list(PREPARE)
IDENTITY = {"org": "dev", "namespace": "local", "agent": "installer-rehearsal"}
# The daemon's socket, mounted into each container; a rootless or remote daemon
# is not supported.
DOCKER_SOCKET = "/var/run/docker.sock"
REGISTRY_IMAGE = (
    "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
)


def check(ok: bool, message: object) -> None:
    """A rehearsal check, which `python -O` keeps, unlike an assert."""
    if not ok:
        raise SystemExit(f"FAIL {message}")


def run(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run `args`; on a failure or timeout, print what it wrote before raising."""
    print("+", " ".join(args), flush=True)
    try:
        return subprocess.run(args, check=True, text=True, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        for out in (e.stdout, e.stderr):
            if out:
                # TimeoutExpired keeps bytes even with text=True.
                print(out.decode(errors="replace") if isinstance(out, bytes) else out, flush=True)
        raise


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=30) as response:
        return json.load(response)


def request(port: int, op: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/{op}",
        data=json.dumps({**IDENTITY, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def installed_version(home: Path, distro: str) -> str:
    """The version the installer wrote; a curl that failed into `sh` wrote nothing."""
    version_file = home / ".loom" / "VERSION"
    check(version_file.is_file(), f"{distro}: the installer wrote no {version_file}")
    return version_file.read_text().strip()


def read_env(home: Path) -> dict[str, str]:
    lines = (home / ".loom" / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class Releases(http.server.SimpleHTTPRequestHandler):
    """GitHub's two release URLs: `latest/download/F` redirects to the `latest`
    release's `download/vX.Y.Z/F`, which serves the file."""

    def __init__(self, *args: Any, latest: str, **kwargs: Any) -> None:
        self.latest = latest  # before the base class, which handles the request
        super().__init__(*args, **kwargs)

    def _redirected(self) -> bool:
        prefix = f"{RELEASES_PATH}/latest/download/"
        if not self.path.startswith(prefix):
            return False
        self.send_response(302)
        target = f"{RELEASES_PATH}/download/v{self.latest}/{self.path[len(prefix) :]}"
        self.send_header("Location", target)
        self.end_headers()
        return True

    def do_GET(self) -> None:
        if not self._redirected():
            super().do_GET()

    def do_HEAD(self) -> None:
        if not self._redirected():
            super().do_HEAD()

    def log_message(self, format: str, *args) -> None:
        print("served:", format % args, flush=True)


def release_server(root: Path, latest: str) -> http.server.ThreadingHTTPServer:
    """`root` served on a loopback port, `latest` redirecting to `latest`'s files."""
    return http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Releases, directory=str(root), latest=latest)
    )


def serve_releases(root: Path, latest: str, certs: Path) -> int:
    """HTTPS on loopback with a certificate for 127.0.0.1 from a CA of this run's
    own, which only this run's containers trust (curl's CURL_CA_BUNDLE): the
    installer keeps its https-only downloads."""
    run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=installer rehearsal CA", "-keyout", str(certs / "ca.key"),
        "-out", str(certs / "ca.pem"), capture_output=True,
    )  # fmt: skip
    (certs / "ext").write_text("subjectAltName=IP:127.0.0.1\n")
    run(
        "openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
        "-keyout", str(certs / "server.key"), "-out", str(certs / "server.csr"),
        capture_output=True,
    )  # fmt: skip
    run(
        "openssl", "x509", "-req", "-in", str(certs / "server.csr"), "-CA", str(certs / "ca.pem"),
        "-CAkey", str(certs / "ca.key"), "-CAcreateserial", "-days", "1",
        "-extfile", str(certs / "ext"), "-out", str(certs / "server.pem"), capture_output=True,
    )  # fmt: skip
    server = release_server(root, latest)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def publish(root: Path, version: str, files: Path) -> None:
    release = root / RELEASES_PATH.lstrip("/") / "download" / f"v{version}"
    release.mkdir(parents=True)
    for name in ("install.sh", "loom-stack.tar.gz", "loom-stack.tar.gz.sha256"):
        shutil.copyfile(files / name, release / name)


def bundle_version(files: Path) -> str:
    return read_member(files / "loom-stack.tar.gz", "loom-stack/VERSION").strip()


def published_installer_url(releases: str, version: str, latest: bool) -> str:
    """The one-liner's `latest/download/install.sh` for the Latest release, or a
    version's own (a prerelease, a patch to an older line). A version that should
    be Latest must be what `latest` redirects to, or the quickstart's one-liner
    was never checked."""
    one_liner = f"{releases}/latest/download/install.sh"
    if not latest:
        return f"{releases}/download/v{version}/install.sh"
    req = urllib.request.Request(one_liner, method="HEAD")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as response:
            answer, location = f"{response.status}", ""
    except urllib.error.HTTPError as e:
        with e:
            answer, location = f"{e.code}", e.headers.get("Location", "")
    check(
        f"/v{version}/" in location,
        f"latest answers {answer} to {location!r}, not a redirect to v{version}",
    )
    return one_liner


def slug(distro: str) -> str:
    """`debian:trixie-slim` as `debian-trixie-slim`, for names and paths."""
    return re.sub(r"[^a-z0-9]+", "-", distro)


def wait_until_listening(url: str, seconds: float = 30) -> None:
    """A started container is not yet a listening one."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            urllib.request.urlopen(url, timeout=5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def remove_stack_on_exit(home: Path, stacks: ExitStack) -> None:
    """Remove this install's containers and volumes when `stacks` closes, if
    the installer got as far as writing its .env; after a failure, print the
    server's logs first."""
    if not (home / ".loom" / ".env").is_file():
        return
    compose = ["docker", "compose", "--project-name", read_env(home)["COMPOSE_PROJECT_NAME"],
               "--project-directory", str(home / ".loom"), "--profile", "bundled"]  # fmt: skip

    def down(exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is not None:  # what the server said, before it is gone
            subprocess.run([*compose, "logs", "--tail", "100"], check=False)
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], check=False)

    stacks.push(down)


def remove_on_exit(cleanup: ExitStack, *args: str) -> None:
    """`docker *args` when `cleanup` closes."""
    cleanup.callback(subprocess.run, ["docker", *args], check=False)


class Release(NamedTuple):
    """Where the one-liner installs from."""

    version: str
    previous: str  # the release the upgrade starts from, or ""
    installer: str  # the one-liner's URL
    env: dict[str, str]  # for the installer's container
    ca: Path | None = None
    # The previous release's install.sh and bundle (previous_release_files.sh);
    # None in published mode (its one-liner installs it) or with no previous release.
    previous_files: Path | None = None


def published_release(args: argparse.Namespace) -> Release:
    installer = published_installer_url(GITHUB_RELEASES, args.version, latest=args.latest == "true")
    return Release(args.version, args.previous_version or "", installer, env={})


def previous_release(version: str, root: Path = ROOT) -> str:
    """The release the upgrade starts from, or "" for none: the release that is
    out when `version` is a final tagged on another commit (main, between
    releases), otherwise the highest final below `version`'s final."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    if not tags:  # a checkout without its tags, not a repository without releases
        raise SystemExit("FAIL no v* tags: fetch them (fetch-depth: 0)")
    final = prepare.final_of(version)
    if version == final and f"v{version}" in tags and not prepare.tag_on_head(version, root):
        return version
    try:
        return prepare.previous_release(final, root).removeprefix("v")
    except ValueError:  # no final release below this one
        return ""


def build_previous_release(release_dir: Path, version: str, work: Path) -> str:
    """The previous release's own installer, and its bundle built from its tag on
    the ClickHouse release this release's bundle names, in `work/previous`;
    returns its version, or "" when there is none."""
    previous = previous_release(version)
    if not previous:
        print(f"no final release below {version}: no upgrade rehearsed", flush=True)
        return ""
    clickhouse = pinned_image(release_dir / "loom-stack.tar.gz")
    run(
        "bash", str(SCRIPTS / "previous_release_files.sh"), previous, clickhouse,
        str(work / "previous"), cwd=ROOT,  # its tags
    )  # fmt: skip
    return previous


def serve_local_release(
    args: argparse.Namespace, cleanup: ExitStack, work: Path, run_id: str
) -> Release:
    """Serve the release files and push the images where the installer finds them."""
    for source in (args.image, args.extractor_image):
        if subprocess.run(["docker", "image", "inspect", source], capture_output=True).returncode:
            raise SystemExit(f"{source} is not a local image: build or pull it first")
    version = bundle_version(args.release_dir)
    root = work / "releases"
    publish(root, version, args.release_dir)
    previous = build_previous_release(args.release_dir, version, work) if args.upgrade else ""
    certs = work / "certs"
    certs.mkdir()
    port = serve_releases(root, version, certs)
    releases = f"https://127.0.0.1:{port}{RELEASES_PATH}"

    registry_port = free_port()
    registry = f"loom-rehearsal-registry-{run_id}"
    run("docker", "run", "-d", "--name", registry, "-p", f"127.0.0.1:{registry_port}:5000",
        REGISTRY_IMAGE, capture_output=True)  # fmt: skip
    remove_on_exit(cleanup, "rm", "-f", "-v", registry)  # and its volume of pushed layers
    host = f"127.0.0.1:{registry_port}"
    wait_until_listening(f"http://{host}/v2/")
    # The previous release's tag carries this build too: the upgrade checks the
    # installer's files, settings and volumes across bundles, not the older server.
    tags = [version] if previous in ("", version) else [version, previous]
    for source, name in ((args.image, "loom"), (args.extractor_image, "loom-extractor")):
        for tag in tags:
            target = f"{host}/clickhouse/{name}:{tag}"
            run("docker", "tag", source, target)
            remove_on_exit(cleanup, "rmi", target)
            run("docker", "push", "-q", target, capture_output=True)
    env = {
        "LOOM_RELEASES_URL": releases,
        "CURL_CA_BUNDLE": "/rehearsal-ca.pem",
        "LOOM_IMAGE_REPOSITORY": f"{host}/clickhouse/loom",
        "LOOM_EXTRACTOR_IMAGE_REPOSITORY": f"{host}/clickhouse/loom-extractor",
    }
    return Release(
        version,
        previous,
        # The local server's `latest` serves this version.
        installer=f"{releases}/latest/download/install.sh",
        env=env,
        ca=certs / "ca.pem",
        previous_files=work / "previous" if previous else None,
    )


class HostDocker(NamedTuple):
    """What the installer's containers mount from this host."""

    compose_plugin: str
    socket_group: int


def host_docker() -> HostDocker:
    """The host's Docker Compose CLI plugin and the Docker socket's group."""
    # The endpoint the CLI resolved, from DOCKER_HOST, DOCKER_CONTEXT or the current context.
    host = run(
        "docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}",
        capture_output=True,
    ).stdout.strip()  # fmt: skip
    on_the_mounted_socket = host.startswith("unix://") and (
        os.path.realpath(host.removeprefix("unix://")) == os.path.realpath(DOCKER_SOCKET)
    )
    if not on_the_mounted_socket:
        raise SystemExit(f"Docker talks to {host}; the containers mount {DOCKER_SOCKET}")
    try:
        group = os.stat(DOCKER_SOCKET).st_gid
    except FileNotFoundError:
        raise SystemExit(f"the containers mount {DOCKER_SOCKET}, which is missing") from None
    plugin = run(
        "docker", "info", "--format",
        '{{range .ClientInfo.Plugins}}{{if eq .Name "compose"}}{{.Path}}{{end}}{{end}}',
        capture_output=True,
    ).stdout.strip()  # fmt: skip
    if not plugin:
        raise SystemExit("the Docker Compose CLI plugin is required (docker compose version)")
    return HostDocker(plugin, group)


class Rehearsal:
    def __init__(
        self, release: Release, cleanup: ExitStack, work: Path, run_id: str, docker: HostDocker
    ) -> None:
        self.release = release
        self.cleanup = cleanup
        self.work = work
        self.run_id = run_id
        self.container_count = 0
        self.distro_images: set[str] = set()
        self.docker = docker

    def distro_image(self, distro: str) -> str:
        """The distribution with only the installer's prerequisites added, built
        once per run."""
        tag = f"loom-rehearsal-{slug(distro)}:{self.run_id}"
        if tag in self.distro_images:
            return tag
        self.distro_images.add(tag)
        dockerfile = f"FROM {distro}\nRUN {PREPARE[distro]}\n"
        run("docker", "build", "-q", "-t", tag, "-", input=dockerfile, capture_output=True)
        remove_on_exit(self.cleanup, "rmi", tag)
        return tag

    def container(
        self, image: str, home: Path, port: int, script: str, mounts: tuple[str, ...] = ()
    ) -> str:
        """`script` in a fresh container of `image`, as this host's user."""
        # A fresh HOME: the Docker client inside is signed in to nothing.
        env = {**self.release.env, "HOME": str(home), "LOOM_PORT": str(port)}
        # Named, so a container that outlives its `docker run` (a timeout) is removed.
        self.container_count += 1
        name = f"loom-rehearsal-{self.run_id}-{self.container_count}"
        remove_on_exit(self.cleanup, "rm", "-f", name)
        cmd = [
            "docker", "run", "--rm", "--name", name, "--network", "host",
            "--user", f"{os.getuid()}:{os.getgid()}", "--group-add", str(self.docker.socket_group),
            "-v", f"{DOCKER_SOCKET}:{DOCKER_SOCKET}",
            "-v", f"{self.docker.compose_plugin}:/usr/libexec/docker/cli-plugins/docker-compose:ro",
            # The installer's Compose files are bind-mounted by the host's daemon,
            # so the home directory has the same path on both sides.
            "-v", f"{home}:{home}",
        ]  # fmt: skip
        if self.release.ca:
            cmd += ["-v", f"{self.release.ca}:/rehearsal-ca.pem:ro"]
        for mount in mounts:
            cmd += ["-v", mount]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        # Bounded: the installer waits 180 s for health; a pull or a hang beyond
        # this fails the rehearsal with what the container printed.
        try:
            result = run(*cmd, image, "sh", "-c", script, capture_output=True, timeout=1200)
        except subprocess.TimeoutExpired:
            # Before the stack's `down`, which an installer still running would undo.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            raise
        print(result.stdout, result.stderr, sep="\n", flush=True)
        return result.stdout

    def one_liner(self, image: str, home: Path, port: int, url: str) -> str:
        return self.container(image, home, port, f'curl -fsSL "{url}" | sh')

    def install_previous(self, image: str, home: Path, port: int) -> None:
        """The previous release by its own install.sh: published, the release's
        copy; locally, the tag's copy with the tag's bundle mounted."""
        release = self.release
        files = release.previous_files
        if files is None:
            url = f"{GITHUB_RELEASES}/download/v{release.previous}/install.sh"
            self.one_liner(image, home, port, url)
            return
        self.container(
            image,
            home,
            port,
            "LOOM_INSTALL_BUNDLE=/previous/loom-stack.tar.gz"
            " LOOM_INSTALL_CHECKSUM=/previous/loom-stack.tar.gz.sha256 sh /previous/install.sh",
            mounts=(f"{files}:/previous:ro",),
        )

    def rehearse(self, distro: str, stacks: ExitStack) -> None:
        """One distribution installed by the one-liner; `stacks` removes its stack
        before the next one starts."""
        image = self.distro_image(distro)
        installer, version = self.release.installer, self.release.version

        home = self.work / f"home-{slug(distro)}"
        home.mkdir()
        port = free_port()
        try:
            self.one_liner(image, home, port, installer)
        finally:
            remove_stack_on_exit(home, stacks)
        installed = installed_version(home, distro)
        check(installed == version, f"{distro}: installed {installed}, not {version}")
        status = health(port)
        check(status["status"] == "ok", f"{distro}: health answered {status}")
        key = f"rehearsal/{distro}"
        request(port, "memory.set", {"memory_key": key, "value": f"Installed on {distro}"})
        hits = request(port, "memory.search", {"query": f"Installed on {distro}"})["results"]
        check(any(hit["memory_key"] == key for hit in hits), f"{distro}: search missed {key}")
        secret = read_env(home)["LOOM_JWT_SECRET"]

        # Re-run: the same files, settings and data.
        output = self.one_liner(image, home, port, installer)
        check(
            "Preserved existing" in output, f"{distro}: the re-run printed no 'Preserved existing'"
        )
        check(
            read_env(home)["LOOM_JWT_SECRET"] == secret, f"{distro}: a re-run replaced the secret"
        )
        found = request(port, "memory.get", {"memory_key": key})
        check(found["found"], f"{distro}: a re-run lost {key}")

        # The downgrade guard under this distribution's sh and awk: over a newer
        # install the one-liner refuses, and leaves VERSION and .env as they were.
        newer = next_release(version)
        version_file = home / ".loom" / "VERSION"
        version_file.write_text(f"{newer}\n")
        output = self.container(
            image, home, port, f'curl -fsSL "{installer}" | sh 2>&1; echo "exit $?"'
        )
        check(
            "installs the older" in output and output.rstrip().endswith("exit 1"),
            f"{distro}: the one-liner did not refuse to replace {newer}",
        )
        check(
            installed_version(home, distro) == newer,
            f"{distro}: the refused install rewrote VERSION",
        )
        pinned = read_env(home)["LOOM_VERSION"]
        check(pinned == version, f"{distro}: the refused install rewrote .env's LOOM_VERSION")
        print(f"PASS {distro}: install, round trip, re-run, downgrade refused", flush=True)

    def upgrade(self, distro: str, stacks: ExitStack) -> None:
        """The previous release installed by its own installer, then the one-liner over it."""
        image = self.distro_image(distro)
        release = self.release
        installer, version, previous = release.installer, release.version, release.previous
        home = self.work / f"home-{slug(distro)}-upgrade"
        home.mkdir()
        port = free_port()
        try:
            self.install_previous(image, home, port)
        finally:
            remove_stack_on_exit(home, stacks)
        installed = installed_version(home, distro)
        check(
            installed == previous,
            f"{distro}: the previous installer installed {installed}, not {previous}",
        )
        request(port, "memory.set", {"memory_key": "rehearsal/upgrade", "value": "Kept across"})
        secret = read_env(home)["LOOM_JWT_SECRET"]
        output = self.one_liner(image, home, port, installer)
        # Between releases `previous` is `version`, so only this line shows the one-liner ran.
        check(
            "Preserved existing" in output, f"{distro}: the upgrade printed no 'Preserved existing'"
        )
        installed = installed_version(home, distro)
        check(installed == version, f"{distro}: the upgrade left {installed}, not {version}")
        pinned = read_env(home)["LOOM_VERSION"]
        check(pinned == version, f"{distro}: .env names {pinned}, not {version}")
        check(
            read_env(home)["LOOM_JWT_SECRET"] == secret,
            f"{distro}: the upgrade replaced the secret",
        )
        found = request(port, "memory.get", {"memory_key": "rehearsal/upgrade"})
        check(
            found["found"] and found["value"] == "Kept across",
            f"{distro}: the upgrade lost rehearsal/upgrade: {found}",
        )
        if previous == version:
            print(f"PASS {distro}: the one-liner over {previous}'s own install", flush=True)
        else:
            print(f"PASS {distro}: upgrade from {previous} to {version}", flush=True)


def release_order(version: str) -> tuple[int, ...]:
    """A version's place in release order: a candidate below its final,
    and candidates by their number, as install.sh's version_below orders them."""
    final, _, candidate = version.partition("-rc.")
    major, minor, patch = (int(part) for part in final.split("."))
    return (major, minor, patch, 0, int(candidate)) if candidate else (major, minor, patch, 1, 0)


def next_release(version: str) -> str:
    """A release just after `version`: a candidate's final, or a final's next
    patch's first candidate."""
    final, _, candidate = version.partition("-rc.")
    if candidate:
        return final
    major, minor, patch = final.split(".")
    return f"{major}.{minor}.{int(patch) + 1}-rc.1"


def release_version(text: str) -> str:
    """An argparse type: a version as the release URLs name it, without the v."""
    if not VERSION_PATTERN.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not X.Y.Z or X.Y.Z-rc.N, without the v")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--release-dir", type=Path, help="install.sh, loom-stack.tar.gz and .sha256"
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="also upgrade from the previous release (previous_release_files.sh)",
    )
    parser.add_argument("--image", help="the server image, built locally")
    parser.add_argument("--extractor-image", help="the extractor image, built locally")
    parser.add_argument("--published", action="store_true", help="the real one-liner")
    parser.add_argument(
        "--version",
        type=release_version,
        help="with --published: the published release to install",
    )
    parser.add_argument(
        "--previous-version",
        type=release_version,
        help="with --published: the release to upgrade from (a local run takes --upgrade)",
    )
    parser.add_argument(
        "--latest",
        choices=("true", "false"),
        help="with --published: whether the version is Latest (scripts/release/make_latest.py)",
    )
    parser.add_argument(
        "--distro", action="append", choices=DISTROS, help=f"default: {' '.join(DISTROS)}"
    )
    args = parser.parse_args()
    local_flags = (args.release_dir, args.upgrade, args.image, args.extractor_image)
    published_flags = (args.version, args.latest, args.previous_version)
    if args.published:
        if not (args.version and args.latest):
            parser.error("--published needs --version and --latest")
        if any(local_flags):
            parser.error("--published takes no local release files or images")
        if args.previous_version and not (
            release_order(args.previous_version) < release_order(args.version)
        ):
            parser.error("--previous-version must be an earlier release than --version")
    else:
        if not (args.release_dir and args.image and args.extractor_image):
            parser.error("--release-dir, --image and --extractor-image, or --published")
        if any(published_flags):
            parser.error("--version, --latest and --previous-version go with --published")

    with ExitStack() as cleanup:
        work = Path(tempfile.mkdtemp(prefix="loom-installer-rehearsal-"))
        cleanup.callback(shutil.rmtree, work, ignore_errors=True)
        docker = host_docker()  # before any build or push
        run_id = secrets.token_hex(3)
        if args.published:
            release = published_release(args)
        else:
            try:
                release = serve_local_release(args, cleanup, work, run_id)
            except ValueError as e:  # a release bundle that cannot be read
                raise SystemExit(f"FAIL {e}") from None
        rehearsal = Rehearsal(release, cleanup, work, run_id, docker)
        for index, distro in enumerate(dict.fromkeys(args.distro or DISTROS)):
            with ExitStack() as stacks:
                rehearsal.rehearse(distro, stacks)
            if index == 0 and release.previous:  # the upgrade on the first distribution only
                with ExitStack() as stacks:
                    rehearsal.upgrade(distro, stacks)
    print(f"PASS installer rehearsal of {release.version}", flush=True)


if __name__ == "__main__":
    main()
