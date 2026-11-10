"""install.sh with a local bundle and LOOM_SKIP_START=1: the files and the
.env it writes for each ClickHouse target. No network, no containers; a stub
`docker` on PATH answers the version check."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.release import build_stack_bundle
from tests.unit.install_versions import GUARD_CASES
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]


def _build(tmp: Path, version: str = "9.9.9") -> tuple[Path, Path]:
    return build_stack_bundle.build(
        version, tmp / "bundle", "clickhouse/clickhouse-server:26.9.18.11"
    )


@pytest.fixture
def installer_env(tmp_path: Path) -> dict[str, str]:
    """The environment install.sh runs in: a local bundle, a stub docker, no start."""
    archive, checksum = _build(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1\n')
    docker.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LOOM_INSTALL_DIR": str(tmp_path / "loom"),
        "LOOM_INSTALL_BUNDLE": str(archive),
        "LOOM_INSTALL_CHECKSUM": str(checksum),
        "LOOM_SKIP_START": "1",
    }


@pytest.fixture
def download_env(installer_env: dict[str, str]) -> dict[str, str]:
    """The installer's environment without a local bundle: it downloads one."""
    return {k: v for k, v in installer_env.items() if not k.startswith("LOOM_INSTALL_")}


@pytest.fixture
def install(installer_env: dict[str, str]):
    def run(
        *args: str, start_new_session: bool = False, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(ROOT / "install.sh"), *args],
            env={**installer_env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=start_new_session,  # no controlling terminal
            check=False,
        )

    return run, Path(installer_env["LOOM_INSTALL_DIR"])


def _env(target: Path) -> dict[str, str]:
    lines = (target / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_a_default_install_runs_the_bundled_clickhouse(install) -> None:
    run, target = install
    result = run()
    assert result.returncode == 0, result.stderr
    assert (target / "compose.yaml").is_file()
    assert (target / "deploy" / "clickhouse-config" / "nlp_user_settings.xml").is_file()
    assert (target / "deploy" / "lemmagen3" / "en.bin").is_file()
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled"
    assert "LOOM_CH_HOST" not in env
    assert "ch-setup" not in result.stdout  # the bundled server needs none
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600
    # The extractor service's token, its own on each install; the server reaches it by name.
    assert env["LOOM_DOCUMENT_EXTRACTOR_URL"] == "http://extractor:8790"
    assert len(env["LOOM_DOCUMENT_EXTRACTOR_TOKEN"]) == 48
    assert "__" not in (target / ".env").read_text()


def test_an_outside_clickhouse_is_written_to_env_and_never_printed(install) -> None:
    run, target = install
    url = "https://loom%40acme:p%40ss$w0rd%3A%2F@abc.clickhouse.cloud:9440/ignored"
    result = run("--clickhouse", url)
    assert result.returncode == 0, result.stderr
    assert "p@ss" not in result.stdout + result.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == ""
    assert env["LOOM_CH_HOST"] == "abc.clickhouse.cloud"
    assert env["LOOM_CH_PORT"] == "9440"
    assert env["LOOM_CH_SECURE"] == "true"
    assert env["LOOM_CH_USER"] == "loom@acme"
    # Single quotes: Compose takes the value literally, `$` included.
    assert env["LOOM_CH_PASSWORD"] == '"p@ss\\$w0rd:/"'
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600


def test_a_raw_slash_or_colon_in_the_password_is_never_printed(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://loom:Secr3t/xy:12@h.example:8443")
    assert result.returncode == 0, result.stderr
    assert "Secr3t" not in result.stdout + result.stderr
    env = _env(target)
    assert (env["LOOM_CH_HOST"], env["LOOM_CH_PORT"]) == ("h.example", "8443")
    assert env["LOOM_CH_PASSWORD"] == '"Secr3t/xy:12"'


def test_a_query_string_after_the_port_is_ignored(install) -> None:
    run, target = install
    assert run("--clickhouse", "https://loom:pw@h.example:8443?secure=1#x").returncode == 0
    assert (_env(target)["LOOM_CH_HOST"], _env(target)["LOOM_CH_PORT"]) == ("h.example", "8443")


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("https://loom:pw@[2001:db8::1]", "[2001:db8::1]", "8443"),
        ("http://loom:pw@[2001:db8::1]:9000", "[2001:db8::1]", "9000"),
    ],
)
def test_an_ipv6_host_is_parsed_with_or_without_a_port(install, url, host, port) -> None:
    run, target = install
    assert run("--clickhouse", url).returncode == 0
    assert (_env(target)["LOOM_CH_HOST"], _env(target)["LOOM_CH_PORT"]) == (host, port)


def test_the_password_can_come_from_the_environment(install) -> None:
    run, target = install
    result = run(LOOM_CH_URL="http://loom@ch.internal", LOOM_CH_PASSWORD="from-env")
    assert result.returncode == 0, result.stderr
    env = _env(target)
    assert (env["LOOM_CH_HOST"], env["LOOM_CH_PORT"], env["LOOM_CH_SECURE"]) == (
        "ch.internal",
        "8123",
        "false",
    )
    assert env["LOOM_CH_USER"] == "loom" and env["LOOM_CH_PASSWORD"] == '"from-env"'


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://h.example:8443", "names no user"),
        ("http://loom:pw@localhost:8123", "is the container itself"),
        ("http://loom:pw@127.0.0.1:8123", "is the container itself"),
    ],
)
def test_a_url_without_a_user_or_on_loopback_is_refused(install, url: str, message: str) -> None:
    run, target = install
    result = run("--clickhouse", url)
    assert result.returncode == 1 and message in result.stderr
    assert not (target / ".env").exists()


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--clickhouse", "http://loom:@h.example"), {}),
        (("--clickhouse", "http://loom@h.example"), {"LOOM_CH_PASSWORD": ""}),
    ],
)
def test_an_empty_password_needs_no_terminal(install, args, env) -> None:
    run, target = install
    result = run(*args, start_new_session=True, **env)
    assert result.returncode == 0, result.stderr
    assert _env(target)["LOOM_CH_PASSWORD"] == '""'


def test_a_rerun_for_the_same_server_keeps_the_saved_password(install) -> None:
    run, target = install
    assert run("--clickhouse", "https://loom:first@h.example").returncode == 0
    rerun = run("--clickhouse", "https://loom@h.example", start_new_session=True)
    assert rerun.returncode == 0, rerun.stderr
    assert "Using the ClickHouse password saved in .env" in rerun.stdout
    assert _env(target)["LOOM_CH_PASSWORD"] == '"first"'
    other_port = run("--clickhouse", "https://loom@h.example:9440", start_new_session=True)
    assert other_port.returncode == 1 and "no ClickHouse password" in other_port.stderr
    other = run("--clickhouse", "https://someone@h.example", start_new_session=True)
    assert other.returncode == 1 and "no ClickHouse password" in other.stderr


def test_no_password_and_no_terminal_says_where_to_put_it(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://loom@h.example", start_new_session=True)
    assert result.returncode == 1
    assert "no ClickHouse password: set LOOM_CH_PASSWORD" in result.stderr


@pytest.mark.parametrize("password", ["it's", "ends\\", "a$b", 'q"x', "t\\tab", "$HOME"])
def test_compose_reads_back_the_password_exactly(install, tmp_path: Path, password: str) -> None:
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode:
        pytest.skip("needs Docker Compose to parse .env")
    run, target = install
    assert run("--clickhouse", "https://loom@h.example", LOOM_CH_PASSWORD=password).returncode == 0
    probe = tmp_path / "probe.yaml"
    # Both ways compose.yaml passes it: env_file, and `${LOOM_CH_PASSWORD-…}`.
    probe.write_text(
        "services:\n  t:\n    image: busybox\n    env_file: [.env]\n"
        '    environment:\n      INTERPOLATED: "${LOOM_CH_PASSWORD-unset}"\n'
    )
    config = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(target),
            "--env-file",
            str(target / ".env"),
            "-f",
            str(probe),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    environment = json.loads(config.stdout)["services"]["t"]["environment"]
    # `config` writes a literal `$` as `$$`, its own escape.
    assert environment["LOOM_CH_PASSWORD"].replace("$$", "$") == password
    assert environment["INTERPOLATED"].replace("$$", "$") == password


def test_an_upgrade_keeps_env_and_an_older_install_stays_bundled(install) -> None:
    run, target = install
    assert run().returncode == 0
    env_path = target / ".env"
    lines = [line for line in env_path.read_text().splitlines() if "COMPOSE_PROFILES" not in line]
    env_path.write_text(
        "\n".join(
            lines + ["LOOM_JWT_SECRET=kept", "LOOM_CH_HOST=localhost", "LOOM_CH_TIMEOUT_READ_S=45"]
        )
        + "\n"
    )
    (target / "clickhouse-config").mkdir()  # the layout before deploy/
    result = run()
    assert result.returncode == 0, result.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled"
    assert env["LOOM_JWT_SECRET"] == "kept" and env["LOOM_VERSION"] == "9.9.9"
    # The old compose file pinned the connection, so a stray line stays inert.
    assert "LOOM_CH_HOST" not in env
    assert env["LOOM_CH_TIMEOUT_READ_S"] == "45"  # the old stack honoured this one
    assert "# ignored before profiles: LOOM_CH_HOST=localhost" in (target / ".env").read_text()
    assert not (target / "clickhouse-config").exists()


def test_switching_an_install_to_an_outside_clickhouse(install) -> None:
    run, target = install
    assert run().returncode == 0
    switched = run("--clickhouse", "https://loom:pw@h.example")
    assert switched.returncode == 0
    assert "--profile bundled stop clickhouse" in switched.stdout
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "" and env["LOOM_CH_HOST"] == "h.example"
    assert list(env).count("LOOM_CH_HOST") == 1


def _answer_prompt(env: dict[str, str], url: str, answer: bytes) -> None:
    """Run the installer on a pseudo-terminal, type `answer` at the prompt and
    wait for it to finish, all within a deadline."""
    import pty
    import select
    import signal
    import time

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("sh", ["sh", str(ROOT / "install.sh"), "--clickhouse", url], env)
    deadline = time.monotonic() + 30
    seen, answered, status = b"", False, None
    try:
        while status is None and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    seen += os.read(fd, 1024)
                except OSError:  # the child closed the terminal
                    pass
            if not answered and b"password for" in seen:
                assert b"(Enter keeps the one saved in .env)" in seen, seen
                os.write(fd, answer)
                answered = True
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    assert answered, seen
    assert status is not None and os.waitstatus_to_exitcode(status) == 0, seen


def test_enter_at_a_rerun_prompt_keeps_the_saved_password(install, installer_env) -> None:
    run, target = install
    assert run("--clickhouse", "https://loom:first@h.example").returncode == 0
    _answer_prompt(installer_env, "https://loom@h.example", b"\n")
    assert _env(target)["LOOM_CH_PASSWORD"] == '"first"'
    _answer_prompt(installer_env, "https://loom@h.example", b"second\n")
    assert _env(target)["LOOM_CH_PASSWORD"] == '"second"'


def test_the_port_and_image_given_to_a_run_stay_in_env(install) -> None:
    run, target = install
    given = {
        "LOOM_PORT": "28977",
        "LOOM_IMAGE_REPOSITORY": "127.0.0.1:5000/loom",
        # A later bare `up` must not move the bundled server to another release.
        "LOOM_CLICKHOUSE_IMAGE": "clickhouse/clickhouse-server:head",
    }
    assert run(**given).returncode == 0
    assert {k: _env(target)[k] for k in given} == given
    # A later run without them keeps them: a bare `docker compose` there must
    # not fall back to port 7777 or the public image.
    assert run().returncode == 0
    assert {k: _env(target)[k] for k in given} == given


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LOOM_PORT", "77x"),
        ("LOOM_PORT", "0"),
        ("LOOM_PORT", "99999"),
        ("LOOM_IMAGE_REPOSITORY", "repo name"),
        ("LOOM_BIND_HOST", "$(id)"),
    ],
)
def test_a_setting_env_cannot_keep_is_refused(install, key: str, value: str) -> None:
    run, target = install
    result = run(**{key: value})
    assert result.returncode == 1 and key in result.stderr
    assert not (target / ".env").exists()


@pytest.mark.parametrize(
    ("bind_host", "polled"),
    [
        ("", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        ("::", "[::1]"),
        ("::1", "[::1]"),
        ("172.17.0.1", "172.17.0.1"),
    ],
)
def test_the_health_check_polls_where_loom_is_published(
    install, installer_env: dict[str, str], bind_host: str, polled: str
) -> None:
    run, target = install
    seen = Path(installer_env["HOME"]) / "curl.log"
    curl = Path(installer_env["PATH"].split(":")[0]) / "curl"
    curl.write_text(f'#!/bin/sh\necho "$@" >> {seen}\necho \'{{"status":"ok"}}\'\n')
    curl.chmod(0o755)
    # Every compose call (pull, up) succeeds; only the version is asked for its output.
    docker = curl.with_name("docker")
    docker.write_text(
        '#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1\nexit 0\n'
    )
    result = run(LOOM_SKIP_START="0", LOOM_PORT="28977", LOOM_BIND_HOST=bind_host)
    assert result.returncode == 0, result.stderr
    assert f"http://{polled}:28977/v1/health" in seen.read_text()
    assert f"Dashboard: http://{polled}:28977/\n" in result.stdout
    assert "--profile bundled down" in result.stdout


def test_a_prepared_install_on_an_outside_clickhouse_names_ch_setup(install) -> None:
    run, target = install
    result = run("--clickhouse", "https://loom:pw@h.example")
    assert result.returncode == 0, result.stderr
    assert "run --rm loom loom ch-setup" in result.stdout


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://lo%24om%20x@h.example:8123", "the ClickHouse user may hold only"),
        ("http://loom@h%24x.example:8123", "invalid host"),
        ("http://loom@a]b:8123", "invalid host"),
    ],
)
def test_a_user_or_host_env_cannot_keep_is_refused(install, url: str, message: str) -> None:
    run, target = install
    result = run("--clickhouse", url, LOOM_CH_PASSWORD="pw")
    assert result.returncode == 1 and message in result.stderr
    assert not (target / ".env").exists()


def test_a_quoted_or_commented_profile_in_env_still_reads_as_bundled(install) -> None:
    run, target = install
    assert run().returncode == 0
    env = target / ".env"
    for written in ('COMPOSE_PROFILES="bundled"', "COMPOSE_PROFILES=bundled  # default"):
        text = env.read_text()
        start = text.index("COMPOSE_PROFILES=")
        env.write_text(text[:start] + written + text[text.index("\n", start) :])
        # A prepared rerun names ch-setup only for an outside ClickHouse.
        assert "ch-setup" not in run().stdout, written


@pytest.mark.parametrize(("status", "says"), [(3, "has no Keeper"), (1, "is not ready for Loom")])
def test_an_outside_clickhouse_ch_setup_refuses_stops_the_install(
    install, installer_env: dict[str, str], status: int, says: str
) -> None:
    """`loom ch-setup` exits 3 when that server has no Keeper: the installer names
    the two ways on (give it one, or --bundled) and starts nothing."""
    run, _ = install
    calls = _recording_docker(installer_env, ch_setup_status=status)
    result = run("--clickhouse", "https://loom:pw@ch.example:8443", LOOM_SKIP_START="0")
    assert result.returncode == 1 and says in result.stderr
    assert not any(" up " in c for c in calls.read_text().splitlines())
    if status == 3:
        assert "with --bundled" in result.stderr


def test_bundled_recovers_an_install_whose_clickhouse_has_no_keeper(
    install, installer_env: dict[str, str]
) -> None:
    """The recovery the refusal names: the second run uses the bundled ClickHouse,
    with the outside server's settings gone, and runs no ch-setup."""
    run, target = install
    calls = _recording_docker(installer_env, ch_setup_status=3)
    first = run("--clickhouse", "https://loom:pw@ch.example:8443", LOOM_SKIP_START="0")
    assert first.returncode == 1 and "with --bundled" in first.stderr
    assert _env(target)["LOOM_CH_HOST"] == "ch.example"
    calls.write_text("")
    second = run("--bundled")
    assert second.returncode == 0, second.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled"
    assert not any(k.startswith("LOOM_CH_") for k in env), env
    assert "ch-setup" not in calls.read_text() and "ch-setup" not in second.stdout
    assert run("--bundled", "--clickhouse", "https://x:y@h:1").returncode == 1


def test_bundled_recovers_an_install_named_by_loom_ch_url(
    install, installer_env: dict[str, str]
) -> None:
    """The same recovery when the outside server came from LOOM_CH_URL, still set in
    the shell: --bundled wins over it."""
    run, target = install
    _recording_docker(installer_env, ch_setup_status=3)
    url = {"LOOM_CH_URL": "https://loom:pw@ch.example:8443"}
    assert run(LOOM_SKIP_START="0", **url).returncode == 1
    second = run("--bundled", **url)
    assert second.returncode == 0, second.stderr
    env = _env(target)
    assert env["COMPOSE_PROFILES"] == "bundled" and "LOOM_CH_HOST" not in env


def _recording_docker(installer_env: dict[str, str], *, ch_setup_status: int) -> Path:
    """A docker stub that logs every call and fails ch-setup with `ch_setup_status`."""
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    calls = bin_dir / "docker-calls.log"
    calls.write_text("")
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {calls}\n'
        '[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1 && exit 0\n'
        f'case "$*" in *ch-setup*) exit {ch_setup_status} ;; esac\nexit 0\n'
    )
    return calls


def test_an_upgrade_from_before_the_extractor_gives_it_a_token_and_keeps_one_it_has(
    install,
) -> None:
    run, target = install
    assert run().returncode == 0
    env_path = target / ".env"
    kept = _env(target)["LOOM_DOCUMENT_EXTRACTOR_TOKEN"]
    assert run().returncode == 0
    assert _env(target)["LOOM_DOCUMENT_EXTRACTOR_TOKEN"] == kept  # a rerun keeps it
    lines = env_path.read_text().splitlines()
    kept_lines = [x for x in lines if not x.startswith("LOOM_DOCUMENT_")]
    # no line, an empty one, a quoted empty one (with an empty URL line too)
    for extra in (
        [],
        ["LOOM_DOCUMENT_EXTRACTOR_TOKEN="],
        ['LOOM_DOCUMENT_EXTRACTOR_TOKEN=""', "LOOM_DOCUMENT_EXTRACTOR_URL="],
    ):
        env_path.write_text("\n".join(kept_lines + extra) + "\n")
        result = run()
        assert result.returncode == 0, result.stderr
        env = _env(target)
        assert env["LOOM_DOCUMENT_EXTRACTOR_URL"] == "http://extractor:8790"
        token = env["LOOM_DOCUMENT_EXTRACTOR_TOKEN"]
        assert len(token) == 48 and token != kept, extra
        assert env_path.read_text().count("LOOM_DOCUMENT_EXTRACTOR_TOKEN=") == 1


def test_an_upgrade_keeps_an_operators_own_extractor_url(install) -> None:
    run, target = install
    assert run().returncode == 0
    env_path = target / ".env"
    lines = [x for x in env_path.read_text().splitlines() if not x.startswith("LOOM_DOCUMENT_")]
    env_path.write_text("\n".join([*lines, "LOOM_DOCUMENT_EXTRACTOR_URL=https://mine:8790"]) + "\n")
    result = run()
    assert result.returncode == 0, result.stderr
    env = _env(target)
    assert env["LOOM_DOCUMENT_EXTRACTOR_URL"] == "https://mine:8790"
    assert len(env["LOOM_DOCUMENT_EXTRACTOR_TOKEN"]) == 48


def _stub_curl(bin_dir: Path, *, fail: bool = False) -> Path:
    """A curl that logs each URL and writes the requested -o file (or fails, as
    a 404 does with -f)."""
    log = bin_dir / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'out=; for a; do [ "$prev" = -o ] && out=$a; prev=$a; done\n'
        f'for a; do case $a in https://*|http://*) echo "$a" >> "{log}" ;; esac; done\n'
        + ("exit 22\n" if fail else '[ -z "$out" ] || : > "$out"\n')
    )
    (bin_dir / "curl").chmod(0o755)
    return log


@pytest.mark.parametrize(
    ("version", "base"),
    [
        ("latest", "https://github.com/ClickHouse/loom/releases/latest/download"),
        ("1.2.3", "https://github.com/ClickHouse/loom/releases/download/v1.2.3"),
    ],
)
def test_the_release_files_come_from_github_by_default(
    download_env: dict[str, str], tmp_path: Path, version: str, base: str
) -> None:
    log = _stub_curl(tmp_path / "bin")
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        env={**download_env, "LOOM_VERSION": version},
        capture_output=True,
        text=True,
        check=False,
    )
    # The stub's empty files fail the checksum: only the URLs are under test.
    assert result.returncode != 0
    assert log.read_text().splitlines() == [
        f"{base}/loom-stack.tar.gz",
        f"{base}/loom-stack.tar.gz.sha256",
    ]


def test_a_releases_install_sh_downloads_that_release(
    download_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "install.sh").write_text(build_stack_bundle.stamp_installer("1.2.3"))
    log = _stub_curl(tmp_path / "bin")
    result = subprocess.run(
        ["sh", str(tmp_path / "install.sh")],
        env=download_env,
        capture_output=True,
        text=True,
        check=False,
    )
    base = "https://github.com/ClickHouse/loom/releases/download/v1.2.3"
    assert log.exists(), result.stderr
    assert log.read_text().splitlines() == [
        f"{base}/loom-stack.tar.gz",
        f"{base}/loom-stack.tar.gz.sha256",
    ]


def test_the_release_files_come_from_loom_releases_url_when_set(
    download_env: dict[str, str], tmp_path: Path
) -> None:
    log = _stub_curl(tmp_path / "bin")
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        env={
            **download_env,
            "LOOM_RELEASES_URL": "https://127.0.0.1:8443/ClickHouse/loom/releases",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert log.exists(), result.stderr
    assert log.read_text().splitlines()[0] == (
        "https://127.0.0.1:8443/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz"
    )


def test_a_release_without_the_bundle_says_where_the_releases_are(
    download_env: dict[str, str], tmp_path: Path
) -> None:
    _stub_curl(tmp_path / "bin", fail=True)
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        env={**download_env, "LOOM_VERSION": "9.9.9"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert (
        "could not download https://github.com/ClickHouse/loom/releases/download/v9.9.9/"
        "loom-stack.tar.gz: the releases with an installer are listed at "
        "https://github.com/ClickHouse/loom/releases"
    ) in result.stderr


def test_an_older_installer_refuses_to_downgrade_an_install(install) -> None:
    """A saved copy of an older release's installer, run over a newer install,
    refuses: it would start an older Loom and ClickHouse on newer data."""
    run, target = install
    target.mkdir()
    (target / "VERSION").write_text("10.0.0\n")
    result = run()
    assert result.returncode == 1
    assert "Loom 10.0.0 is installed and this installer installs the older 9.9.9" in result.stderr
    assert (target / "VERSION").read_text() == "10.0.0\n"
    assert run(LOOM_VERSION="latest").returncode == 1  # latest names no version: the guard holds
    # A name that is not the bundle's does not lift the guard either.
    named_other = run(LOOM_VERSION="10.1.0")
    assert named_other.returncode == 1 and "installs the older 9.9.9" in named_other.stderr
    for named in ("9.9.9", "v9.9.9"):  # asked for by name, with or without the v
        (target / "VERSION").write_text("10.0.0\n")
        assert run(LOOM_VERSION=named).returncode == 0, named
    assert (target / "VERSION").read_text().strip() == "9.9.9"


@pytest.mark.parametrize("installed", ["", "dev"])
def test_a_version_file_that_names_no_release_does_not_block_the_install(
    install, installed: str
) -> None:
    """The guard compares releases: a VERSION that names no release is no newer install."""
    run, target = install
    target.mkdir()
    (target / "VERSION").write_text(f"{installed}\n")
    result = run()
    assert result.returncode == 0, result.stderr
    assert (target / "VERSION").read_text().strip() == "9.9.9"


def test_an_awk_that_fails_refuses_rather_than_lift_the_guard(
    install, installer_env: dict[str, str]
) -> None:
    run, target = install
    target.mkdir()
    (target / "VERSION").write_text("9.9.8\n")  # older: a working guard would let it through
    awk = Path(installer_env["PATH"].split(":")[0]) / "awk"
    awk.write_text("#!/bin/sh\nexit 2\n")
    awk.chmod(0o755)
    result = run()
    assert result.returncode == 1
    assert "Loom installer: could not compare the installed Loom 9.9.8 with 9.9.9" in result.stderr
    assert (target / "VERSION").read_text() == "9.9.8\n"


@pytest.mark.parametrize(("installed", "bundle", "refused"), GUARD_CASES)
def test_only_an_older_bundle_than_the_installed_one_is_refused(
    install, tmp_path: Path, installed: str, bundle: str, refused: bool
) -> None:
    run, target = install
    target.mkdir()
    (target / "VERSION").write_text(f"{installed}\n")
    archive, checksum = _build(tmp_path / "bundle", bundle)
    result = run(LOOM_INSTALL_BUNDLE=str(archive), LOOM_INSTALL_CHECKSUM=str(checksum))
    assert result.returncode == (1 if refused else 0), result.stderr
    assert (target / "VERSION").read_text().strip() == (installed if refused else bundle)
    if refused:
        assert f"installs the older {bundle}" in result.stderr


LAST_LINE = '{ main "$@"; }\n'


def test_the_whole_script_piped_to_sh_installs(install, installer_env: dict[str, str]) -> None:
    """The control for the cut-off cases below: the same environment, the whole script."""
    _, target = install
    text = (ROOT / "install.sh").read_text()
    result = subprocess.run(
        ["sh"], input=text, env=installer_env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and target.exists(), result.stderr


def _cut_runs_nothing(install, installer_env: dict[str, str], end: int) -> None:
    """The first `end` characters of the script, piped to `sh` as `curl | sh`
    does, run nothing, in an environment where the whole script installs."""
    _, target = install
    text = (ROOT / "install.sh").read_text()
    result = subprocess.run(
        ["sh"], input=text[:end], env=installer_env, capture_output=True, text=True, check=False
    )
    assert not target.exists(), result.stderr
    assert result.stdout == ""
    assert "Loom installer:" not in result.stderr


@pytest.mark.parametrize("fraction", [0.1, 0.5, 0.9, 0.99])
def test_a_download_cut_short_runs_nothing(
    install, installer_env: dict[str, str], fraction: float
) -> None:
    """`curl | sh` runs what has arrived: the script is one function called on its
    last line, so a cut-off copy is a syntax error or defines it without calling it."""
    text = (ROOT / "install.sh").read_text()
    _cut_runs_nothing(install, installer_env, int(len(text) * fraction))


@pytest.mark.parametrize("into_last_line", range(len(LAST_LINE) - 1))
def test_a_download_cut_inside_the_last_line_runs_nothing(
    install, installer_env: dict[str, str], into_last_line: int
) -> None:
    """The call is inside braces, so a cut right after `main` or before the
    closing brace leaves the brace open, and `sh` runs nothing."""
    text = (ROOT / "install.sh").read_text()
    assert text.endswith("\n" + LAST_LINE)
    _cut_runs_nothing(install, installer_env, len(text) - len(LAST_LINE) + into_last_line)


def test_an_unreachable_docker_daemon_is_named_before_any_pull(
    installer_env: dict[str, str], tmp_path: Path
) -> None:
    docker = tmp_path / "bin" / "docker"
    docker.write_text(
        '#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1 && exit 0\n'
        'echo "$*" >> "$0.log"; exit 1\n'
    )
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        env={**installer_env, "LOOM_SKIP_START": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "cannot reach the Docker daemon" in result.stderr
    assert Path(f"{docker}.log").read_text().splitlines() == ["info"]  # no pull
    assert not Path(installer_env["LOOM_INSTALL_DIR"]).exists()  # nor any file written


# curl's own save flags: `-o FILE`, `--output FILE`, or `-O` / a short-flag group
# ending in `o FILE` or holding `O`. A FILE of `-` or under /dev/ is a stream, not a save.
_SAVES = re.compile(
    r"\s(?:-[A-Za-z]*o|--output)\s*(?!-(?![\w.])|/dev/)[\w.~/$\"'{}-]|\s-[A-Za-z]*O\b"
)
# Where a shell command ends and the next one, or a substitution, begins.
_COMMAND_BREAK = re.compile(r"\\?\||&&|;|\$\(|<\(")
# Installed dependencies, which hold other projects' install lines.
_NOT_OURS = {"node_modules", ".venv", ".git"}


def _unsaved_download(line: str) -> bool:
    """A line whose curl of `install.sh` does not save it to a file."""
    curls = [part for part in _COMMAND_BREAK.split(line) if "curl" in part]
    return "install.sh" in line and any(not _SAVES.search(part) for part in curls)


def test_every_installer_download_is_saved_first() -> None:
    """A downloaded installer handed straight to a shell (`curl … | sh`,
    `sh -c "$(curl …)"`, `bash <(curl …)`) runs as an empty script and exits 0 when
    the download fails; on every tracked line that names `install.sh`, each curl
    command saves its download to a file first (the docs fetch with curl only). A `\\` line break joins two lines into one."""
    own = Path(__file__).resolve().relative_to(ROOT).as_posix()  # holds the examples below
    unsaved = []
    for name in tracked():
        if name == own or _NOT_OURS & set(Path(name).parts):
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue  # a binary file, a submodule, a file deleted in the work tree
        if any(_unsaved_download(line) for line in re.sub(r"\\\n\s*", " ", text).splitlines()):
            unsaved.append(name)
    assert unsaved == []


@pytest.mark.parametrize(
    "line",
    [
        "curl -fsSL https://x/install.sh | sh",
        "curl -fsSL 'https://x/install.sh' | sudo -E bash",
        'curl -fsSL "https://x/install.sh" | LOOM_SKIP_START=1 sh -s -- --bundled',
        "`curl -fsSL …/install.sh \\| sh`",
        'sh -c "$(curl -fsSL https://x/install.sh)"',
        "bash <(curl -fsSL https://x/install.sh)",
        "curl https://x/install.sh -o - | sh",
        "curl https://x/install.sh -o -|sh",
        "curl -fsSL https://x/install.sh | sh -o errexit",
        "curl -fsSL https://x/install.sh -o /dev/stdout | sh",
    ],
)
def test_an_unsaved_download_is_flagged(line: str) -> None:
    assert _unsaved_download(line)


@pytest.mark.parametrize(
    "line",
    [
        "curl -fsSL https://x/install.sh -o loom-install.sh && sh loom-install.sh",
        "curl -fL https://x/install.sh -o loom-install.sh",
        "curl -fsSLO https://x/install.sh && sh install.sh",
        "| `install.sh` | bash-compatible installer |",
        "curl -fsSLo loom-install.sh https://x/install.sh && sh loom-install.sh",
        "curl https://x/install.sh --output loom-install.sh && sh loom-install.sh",
        'curl -fsSL "https://x/install.sh" -o ~/loom-agent-setup/loom-install.sh',
    ],
)
def test_a_saved_download_passes(line: str) -> None:
    assert not _unsaved_download(line)


def test_the_documented_one_liner_fails_on_a_failed_download(tmp_path: Path) -> None:
    """The README's command, with a curl that writes an installer to its `-o` path
    and still fails: the line exits with curl's code and the installer never runs."""
    (line,) = [
        line
        for line in (ROOT / "README.md").read_text().splitlines()
        if line.startswith("curl ") and "install.sh" in line
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    # Writes a script that would create ~/.loom to its -o path, then fails.
    curl.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && printf \'mkdir "$HOME/.loom"\\n\' >"$2"; shift; done\n'
        "echo 'curl: (22) The requested URL returned error: 404' >&2\n"
        "exit 22\n"
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", line],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 22, result
    assert "404" in result.stderr
    # The fake wrote it: only `&&` kept it from running.
    assert (tmp_path / "loom-install.sh").is_file()
    assert not (tmp_path / ".loom").exists()
