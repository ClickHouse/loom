"""The installer rehearsal's own pieces, and the release-check workflow that
runs it against a published release. The rehearsal itself runs in docker-smoke
(scripts/gates/installer_rehearsal.py)."""

from __future__ import annotations

import argparse
import re
import subprocess
import tarfile
import threading
import urllib.error
import urllib.request
from contextlib import ExitStack, nullcontext
from pathlib import Path

import pytest
import yaml

from scripts.gates import installer_rehearsal as rehearsal
from scripts.release import build_stack_bundle
from tests.unit import git_repo
from tests.unit.install_versions import GUARD_CASES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def releases(tmp_path_factory: pytest.TempPathFactory):
    """A release 9.9.9 served as GitHub serves it, Latest, once for the module's
    tests, which only read it; yields the bundle's files and the releases URL."""
    tmp_path = tmp_path_factory.mktemp("releases")
    files = tmp_path / "files"
    build_stack_bundle.build("9.9.9", files, "clickhouse/clickhouse-server:26.9.18.11")
    root = tmp_path / "root"
    rehearsal.publish(root, "9.9.9", files)
    server = rehearsal.release_server(root, "9.9.9")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield files, f"http://127.0.0.1:{server.server_address[1]}{rehearsal.RELEASES_PATH}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_latest_redirects_to_the_release_as_github_does(releases) -> None:
    """`curl -L` follows `releases/latest/download/F` to `releases/download/vX.Y.Z/F`."""
    _, base = releases
    opener = urllib.request.build_opener(rehearsal._NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as redirect:
        opener.open(f"{base}/latest/download/install.sh")
    assert redirect.value.code == 302
    assert redirect.value.headers["Location"] == (
        f"{rehearsal.RELEASES_PATH}/download/v9.9.9/install.sh"
    )
    with urllib.request.urlopen(f"{base}/latest/download/install.sh") as followed:
        assert b"\n  RELEASE_VERSION=9.9.9\n" in followed.read()


def test_the_published_check_uses_the_one_liner_only_for_latest(releases) -> None:
    """And then `latest` must serve that version."""
    _, base = releases

    def url(version: str, latest: bool) -> str:
        return rehearsal.published_installer_url(base, version, latest=latest)

    assert url("9.9.9", latest=True) == f"{base}/latest/download/install.sh"
    assert url("9.8.1", latest=False) == f"{base}/download/v9.8.1/install.sh"
    with pytest.raises(SystemExit, match="not a redirect to v9.10.0"):
        url("9.10.0", latest=True)


@pytest.mark.parametrize("version_is_a_directory", [False, True])
def test_a_bundle_without_its_version_file_is_named(
    tmp_path: Path, version_is_a_directory: bool
) -> None:
    with tarfile.open(tmp_path / "loom-stack.tar.gz", "w:gz") as archive:
        archive.add(tmp_path, arcname="loom-stack", recursive=False)
        if version_is_a_directory:
            archive.add(tmp_path, arcname="loom-stack/VERSION", recursive=False)
    with pytest.raises(ValueError, match="loom-stack.tar.gz has no loom-stack/VERSION$"):
        rehearsal.bundle_version(tmp_path)


def test_an_installer_that_wrote_nothing_fails_by_name(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="^FAIL alpine:3.22: the installer wrote no "):
        rehearsal.installed_version(tmp_path, "alpine:3.22")


def test_the_version_is_read_from_the_bundle(releases) -> None:
    files, _ = releases
    assert rehearsal.bundle_version(files) == "9.9.9"


WRITES_ITS_VERSION = (
    "import argparse, pathlib\n"
    "p = argparse.ArgumentParser(); p.add_argument('cmd'); p.add_argument('--version')\n"
    "p.add_argument('--clickhouse-image'); p.add_argument('--output-dir')\n"
    "a = p.parse_args(); out = pathlib.Path(a.output_dir)\n"
    "(out / 'loom-stack.tar.gz').write_text(a.version)\n"
)


def _commit(repo: Path, message: str) -> None:
    git_repo.git(repo, "commit", "-q", "--allow-empty", "-m", message)


def _previous(repo: Path, out: Path, version: str = "1.2.0") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/gates/previous_release_files.sh"), version,
         "clickhouse/clickhouse-server:26.9.18.11", str(out)],
        cwd=repo, capture_output=True, text=True,
    )  # fmt: skip


def _repo_with_releases(tmp_path: Path, tags: list[str], builder: str = WRITES_ITS_VERSION) -> Path:
    """A repository whose every tag has `builder`, by default one that writes its version."""
    repo = tmp_path / "repo"
    (repo / "scripts/release").mkdir(parents=True)
    (repo / "scripts/release/build_stack_bundle.py").write_text(builder)
    (repo / "install.sh").write_text("#!/bin/sh\n")

    git_repo.git(repo, "init", "-q")
    git_repo.git(repo, "add", ".")
    _commit(repo, "x")
    for tag in tags:
        git_repo.git(repo, "tag", tag)
    return repo


@pytest.mark.parametrize(
    ("version", "previous"),
    [
        ("1.2.0", "1.1.0"),
        ("1.10.0", "1.9.1"),  # its own tag on HEAD: a release run
        ("1.2.0-rc.1", "1.1.0"),
        ("1.1.0-rc.1", "1.0.0"),  # its own final already tagged
        ("1.0.0", ""),
    ],
)
def test_the_previous_release_is_the_highest_final_tag_below(
    tmp_path: Path, version: str, previous: str
) -> None:
    repo = _repo_with_releases(tmp_path, ["v1.0.0", "v1.1.0", "v1.2.0-rc.1", "v1.9.1", "v1.10.0"])
    out = tmp_path / "previous"
    result = _previous(repo, out, version)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == previous
    if previous:
        # Built by that tag's own builder, with that tag's install.sh beside it.
        assert (out / "loom-stack.tar.gz").read_text() == previous
        assert (out / "install.sh").is_file()
    else:
        assert not out.exists()


def test_a_released_version_built_on_another_commit_upgrades_from_its_own_tag(
    tmp_path: Path,
) -> None:
    """A pull request on main still names the release that is out: the upgrade
    starts from that release, not the one before it."""
    repo = _repo_with_releases(tmp_path, ["v1.0.0", "v1.1.0"])
    _commit(repo, "after the release")
    result = _previous(repo, tmp_path / "previous", "1.1.0")
    assert result.stdout.strip() == "1.1.0", result.stderr


def test_a_checkout_without_tags_fails_rather_than_skip_the_upgrade(tmp_path: Path) -> None:
    result = _previous(_repo_with_releases(tmp_path, []), tmp_path / "previous")
    assert result.returncode != 0 and "no v* tags" in result.stderr


def test_only_candidate_tags_are_no_previous_release(tmp_path: Path) -> None:
    result = _previous(_repo_with_releases(tmp_path, ["v1.2.0-rc.1"]), tmp_path / "previous")
    assert result.returncode == 0 and result.stdout == "", result.stderr


def test_a_previous_release_that_fails_to_build_fails(tmp_path: Path) -> None:
    repo = _repo_with_releases(tmp_path, ["v1.1.0"], builder="raise SystemExit(3)\n")
    result = _previous(repo, tmp_path / "previous")
    assert result.returncode != 0 and not result.stdout.strip()


def test_release_check_runs_the_published_one_liner_on_both_architectures() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-check.yml").read_text())
    job = workflow["jobs"]["installer"]
    runners = {i["arch"]: i["runner"] for i in job["strategy"]["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-latest", "arm64": "ubuntu-24.04-arm"}
    (step,) = [s for s in job["steps"] if "installer_rehearsal.py" in s.get("run", "")]
    assert "--published" in step["run"]
    # Signed in to nothing: the token stays with the Latest lookup.
    assert "GH_TOKEN" not in step["env"]
    assert step["env"]["LATEST"] == "${{ steps.latest.outputs.latest }}"
    # A rehearsal's own release URL never reaches the published check.
    assert "LOOM_RELEASES_URL" not in yaml.safe_dump(workflow)


def test_the_published_previous_release_is_installed_by_its_own_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No LOOM_VERSION: the release's install.sh installs its own release."""
    release = rehearsal.Release("1.2.0", "1.1.0", "unused", env={})
    docker = rehearsal.HostDocker("/compose", 0)
    rehearsing = rehearsal.Rehearsal(release, ExitStack(), tmp_path, "id", docker)
    scripts: list[str] = []
    monkeypatch.setattr(
        rehearsing, "container", lambda image, home, port, script: scripts.append(script)
    )
    rehearsing.install_previous("image", tmp_path, 1)
    assert scripts == [f'curl -fsSL "{rehearsal.GITHUB_RELEASES}/download/v1.1.0/install.sh" | sh']


@pytest.mark.parametrize("flag", ["--version", "--previous-version"])
def test_a_version_flag_refuses_a_leading_v(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.setattr("sys.argv", ["installer_rehearsal.py", "--published", flag, "v1.2.0"])
    with pytest.raises(SystemExit):
        rehearsal.main()
    assert "'v1.2.0' is not X.Y.Z or X.Y.Z-rc.N, without the v" in capsys.readouterr().err


def test_release_order_puts_a_candidate_below_its_final_and_by_number() -> None:
    order = ["1.1.0", "1.2.0-rc.1", "1.2.0-rc.2", "1.2.0-rc.10", "1.2.0", "1.10.0"]
    assert sorted(reversed(order), key=rehearsal.release_order) == order


@pytest.mark.parametrize(
    ("version", "newer"),
    [("1.2.0-rc.3", "1.2.0"), ("1.2.0", "1.2.1-rc.1"), ("1.2.9", "1.2.10-rc.1")],
)
def test_the_downgrade_check_writes_a_release_just_after_this_one(version: str, newer: str) -> None:
    assert rehearsal.next_release(version) == newer
    assert rehearsal.release_order(version) < rehearsal.release_order(newer)


@pytest.mark.parametrize(("installed", "bundle", "refused"), GUARD_CASES)
def test_release_order_agrees_with_the_installers_downgrade_guard(
    installed: str, bundle: str, refused: bool
) -> None:
    assert (rehearsal.release_order(bundle) < rehearsal.release_order(installed)) is refused


@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-rc.1"])
def test_a_release_version_is_taken_as_given(version: str) -> None:
    assert rehearsal.release_version(version) == version


@pytest.mark.parametrize("version", ["v1.2.0", "1.2", "1.2.0-beta.1", "1.2.0-rc1"])
def test_anything_but_a_final_or_a_candidate_is_refused(version: str) -> None:
    """Finals and candidates only: the versions a release tag names."""
    with pytest.raises(argparse.ArgumentTypeError):
        rehearsal.release_version(version)


def _host_docker(
    monkeypatch: pytest.MonkeyPatch, socket: Path, endpoint: str
) -> rehearsal.HostDocker:
    """host_docker() with `socket` as the mounted socket and the CLI resolving `endpoint`."""

    def docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = endpoint if args[1] == "context" else "/compose"
        return subprocess.CompletedProcess(args, 0, out + "\n", "")

    monkeypatch.setattr(rehearsal, "run", docker)
    monkeypatch.setattr(rehearsal, "DOCKER_SOCKET", str(socket))
    return rehearsal.host_docker()


@pytest.mark.parametrize("through_a_link", [False, True])
def test_the_mounted_socket_is_taken_by_its_path_or_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through_a_link: bool
) -> None:
    socket = tmp_path / "docker.sock"  # a file stands in: only its path and group are read
    socket.touch()
    mounted = socket
    if through_a_link:  # DOCKER_SOCKET is /var/run/docker.sock, where /var/run links to /run
        mounted = tmp_path / "link.sock"
        mounted.symlink_to(socket)
    docker = _host_docker(monkeypatch, mounted, f"unix://{socket}")
    assert docker == rehearsal.HostDocker("/compose", socket.stat().st_gid)


def test_a_missing_socket_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket = tmp_path / "docker.sock"  # never created
    with pytest.raises(SystemExit, match="which is missing$"):
        _host_docker(monkeypatch, socket, f"unix://{socket}")


@pytest.mark.parametrize(
    "endpoint",
    ["unix:///run/user/1000/docker.sock", "tcp://10.0.0.5:2376"],  # rootless, remote
)
def test_a_daemon_other_than_the_mounted_socket_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    socket = tmp_path / "docker.sock"
    socket.touch()
    with pytest.raises(SystemExit, match=f"^Docker talks to {re.escape(endpoint)};"):
        _host_docker(monkeypatch, socket, endpoint)


def test_a_failed_command_prints_what_it_wrote(capsys) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        rehearsal.run("sh", "-c", "echo out; echo err >&2; exit 3", capture_output=True)
    printed = capsys.readouterr().out
    assert "out\n" in printed and "err\n" in printed


@pytest.mark.parametrize("failed", [False, True])
def test_a_stack_prints_its_logs_before_it_is_removed_only_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    (tmp_path / ".loom").mkdir()
    (tmp_path / ".loom" / ".env").write_text("COMPOSE_PROJECT_NAME=loom-x\n")
    ran: list[str] = []
    monkeypatch.setattr(
        rehearsal.subprocess,
        "run",
        lambda cmd, **_: ran.append(next(a for a in cmd if a in ("logs", "down"))),
    )
    expected = pytest.raises(RuntimeError) if failed else nullcontext()
    with expected, ExitStack() as stacks:
        rehearsal.remove_stack_on_exit(tmp_path, stacks)
        if failed:
            raise RuntimeError("the round trip failed")
    assert ran == (["logs", "down"] if failed else ["down"])


def test_waiting_returns_once_a_url_answers_and_raises_after_the_deadline(releases) -> None:
    _, base = releases
    rehearsal.wait_until_listening(f"{base}/latest/download/install.sh", seconds=1)
    with pytest.raises(OSError):
        rehearsal.wait_until_listening("http://127.0.0.1:9/v2/", seconds=0.2)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--published", "--version", "1.2.3", "--latest", "true", "--release-dir", "x"],
         "--published takes no local release files or images"),
        (["--release-dir", "x", "--image", "i", "--extractor-image", "e", "--version", "1.2.3"],
         "--version, --latest and --previous-version go with --published"),
        (["--published", "--version", "1.2.3", "--latest", "true", "--previous-version", "1.2.3"],
         "--previous-version must be an earlier release than --version"),
        (["--published", "--version", "1.2.3", "--latest", "true", "--previous-version", "1.3.0"],
         "--previous-version must be an earlier release than --version"),
    ],
)  # fmt: skip
def test_a_mode_refuses_the_other_modes_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.setattr("sys.argv", ["installer_rehearsal.py", *argv])
    with pytest.raises(SystemExit) as exited:
        rehearsal.main()
    assert exited.value.code == 2 and message in capsys.readouterr().err
