"""install.sh with a local bundle and LOOM_SKIP_START=1: the files and the
.env it writes for each ClickHouse target. No network, no containers; a stub
`docker` on PATH answers the version check."""

from __future__ import annotations

import json
import os
import platform
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable
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
    # Nothing listens anywhere: the port check's probe is refused (curl's exit 7).
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 7\n")
    curl.chmod(0o755)
    # Agent CLIs that do nothing, ahead of any the machine has: a test that runs
    # the agent step never reaches a real one.
    for name in ("claude", "codex", "code"):
        cli = bin_dir / name
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o755)
    # Node.js 20, whatever the machine has.
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\necho 20\n")
    node.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LOOM_INSTALL_DIR": str(tmp_path / "loom"),
        "LOOM_INSTALL_BUNDLE": str(archive),
        "LOOM_INSTALL_CHECKSUM": str(checksum),
        "LOOM_SKIP_START": "1",
        # No agent on the machine running the tests is touched, nor a browser opened.
        "LOOM_CONNECT": "none",
        "LOOM_NO_OPEN": "1",
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


def _on_terminal(
    env: dict[str, str],
    args: list[str],
    prompt: bytes | None,
    answer: bytes = b"",
    *,
    ready: Callable[[], bool] | None = None,
    exit_code: int = 0,
) -> bytes:
    """Run the installer on a pseudo-terminal and wait for it to finish, within a
    deadline, with `exit_code`. `answer` is typed at each `prompt`, or once when
    `ready()` turns true. Returns what the terminal showed."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("sh", ["sh", str(ROOT / "install.sh"), *args], env)
    deadline = time.monotonic() + 180  # a loaded machine runs the installer slowly
    seen, answered, status = b"", 0, None
    try:
        while status is None and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    seen += os.read(fd, 1024)
                except OSError:  # the child closed the terminal
                    pass
            due = (
                seen.count(prompt) > answered
                if prompt
                else bool(ready and not answered and ready())
            )
            if due:
                os.write(fd, answer)
                answered += 1
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    assert answered or (prompt is None and ready is None), seen
    assert status is not None and os.waitstatus_to_exitcode(status) == exit_code, seen
    return seen


def test_ctrl_c_stops_the_agent_cli_and_cleans_up(
    installer_env: dict[str, str], tmp_path: Path
) -> None:
    """^C while a client CLI runs under `bounded`: the CLI is stopped, the run exits
    130 and its temporary directory is removed."""
    _start_stubs(installer_env)
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    pids = bin_dir / "claude.pid"
    claude = bin_dir / "claude"
    # Deaf to the terminal's ^C and hangup: only the installer's own kill stops it.
    claude.write_text(f'#!/bin/sh\ntrap "" HUP INT\necho $$ > "{pids}"\nexec sleep 60\n')
    claude.chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {
        **installer_env,
        "LOOM_SKIP_START": "0",
        "LOOM_CONNECT": "claude-code",
        "TMPDIR": str(tmp),
    }
    _on_terminal(
        env,
        [],
        None,
        b"\x03",
        ready=lambda: pids.exists() and bool(pids.read_text().strip()),
        exit_code=130,
    )
    cli = int(pids.read_text())
    gone = time.monotonic() + 10
    while _running(cli) and time.monotonic() < gone:
        time.sleep(0.05)
    if _running(cli):
        os.kill(cli, signal.SIGKILL)
        raise AssertionError(f"claude ({cli}) still runs")
    assert list(tmp.iterdir()) == []


def _running(pid: int) -> bool:
    """Alive and not a zombie an init that does not reap has kept."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        # Reaped between the two reads, or no /proc (macOS), where kill said alive.
        return not Path("/proc").exists()
    except OSError:  # exiting
        return False
    return state != "Z"


def _answer_prompt(env: dict[str, str], url: str, answer: bytes) -> None:
    seen = _on_terminal(env, ["--clickhouse", url], b"password for", answer)
    assert b"(Enter keeps the one saved in .env)" in seen, seen


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
    # The port probe before the pull is refused; the health poll answers ok.
    curl.write_text(
        f'#!/bin/sh\necho "$@" >> {seen}\ncase "$*" in *health*) ;; *) exit 7 ;; esac\n'
        'echo \'{"status":"ok"}\'\n'
    )
    curl.chmod(0o755)
    # Every docker call succeeds and prints nothing, so no own server is found; only the
    # version is asked for its output.
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


# Fake curls: the working one serves a file from $SERVES when it holds one by that name,
# else writes an installer creating ~/.loom, to its -o/-O file or to stdout. The failing
# one does the same, then answers a 404 for install.sh: the file written anyway stands for a
# copy an earlier attempt left, the text on stdout for what a piped download printed.
_WORKING_CURL = """#!/bin/sh
out=
url=
save=
while [ $# -gt 0 ]; do
  case $1 in -o) out=$2 ;; -*O) save=1 ;; http*|…*) url=$1 ;; esac
  shift
done
[ -n "$save" ] && [ -z "$out" ] && out=${url##*/}
if [ -n "$SERVES" ] && [ -f "$SERVES/${url##*/}" ]; then
  cp "$SERVES/${url##*/}" "${out:-/dev/stdout}"
  exit 0
fi
installer='mkdir -p "$HOME/.loom"; echo "$@" >"$HOME/.loom/args"; env | grep ^LOOM_ >"$HOME/.loom/env" || :'
if [ -z "$out" ] || [ "$out" = - ]; then echo "$installer"; else echo "$installer" >"$out"; fi
"""
_FAILING_CURL = (
    _WORKING_CURL
    + """case $url in *install.sh) ;; *) exit 0 ;; esac
echo 'curl: (22) The requested URL returned error: 404' >&2
exit 22
"""
)
_CODE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```|`([^`\n]+)`", re.MULTILINE | re.DOTALL)


def _joined(text: str) -> str:
    """`text` with each `\\` line continuation joined."""
    return re.sub(r"\\\n\s*", " ", text)


def _documented_installs() -> list[tuple[str, str]]:
    """(page, example) for every code block or inline command a tracked page shows
    that downloads `install.sh`, as a copy button pastes it: each `\\` continuation
    joined and a `<placeholder>` filled."""
    found = []
    for name in tracked():
        # i18n/ copies the English pages' code byte for byte (scripts/i18n/translate.py).
        if not name.endswith(".md") or name.startswith("i18n/"):
            continue
        for block, span in _CODE.findall((ROOT / name).read_text(encoding="utf-8")):
            lines = [
                re.sub(r"<[^>]*>", "x", line.strip().replace("\\|", "|"))
                for line in _joined(block or span).splitlines()
            ]
            if any("curl" in line and "install.sh" in line for line in lines):
                found.append((name, "\n".join(lines)))
    return found


def _run_install(
    script: str, tmp_path: Path, curl_script: str, serves: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """`script` as a terminal runs pasted lines, each after the last whatever it
    returned, in an empty home with `curl_script` as curl."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(curl_script)
    curl.chmod(0o755)
    return subprocess.run(
        ["sh", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "SERVES": str(serves or ""),
        },
        capture_output=True,
        text=True,
    )


# `[LOOM_VAR=value …] sh FILE [ARG …]` up to the next `&&`: the step that runs the installer.
_RUN_STEP = re.compile(
    r"&&\s*(?P<env>(?:LOOM_\w+=\S*\s+)*)sh\s+\S*loom-install\.sh(?P<args>(?:\s+(?!&&)\S+)*)"
)
_INSTALLS = _documented_installs()
# The inspect path's downloads and checks, which the reader runs before installing.
_INSPECT = "sha256sum -c"


def _inspect_then_install() -> tuple[str, str, str]:
    """The inspect path's checks followed by its install step, the next code block on the
    same page, as a reader runs them once the checks look right."""
    ((page, checks),) = [(p, e) for p, e in _INSTALLS if _INSPECT in e]
    text = (ROOT / page).read_text(encoding="utf-8")
    blocks = [m[1] for m in re.finditer(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)]
    at = next(i for i, block in enumerate(blocks) if _INSPECT in block)
    assert at + 1 < len(blocks), page
    return page, checks, _joined(blocks[at + 1]).strip()


def test_the_pages_show_install_commands() -> None:
    pages = {page for page, _ in _INSTALLS}
    assert {"README.md", "docs/getting-started/quickstart.md"} <= pages
    assert any(_INSPECT in example for _, example in _INSTALLS)


@pytest.mark.parametrize(("page", "example"), _INSTALLS)
def test_a_documented_install_stops_on_a_failed_download(
    page: str, example: str, tmp_path: Path
) -> None:
    """Piped into a shell, a download that fails runs what curl wrote and exits 0;
    every install example a page shows exits with curl's error and runs nothing. The
    inspect block installs nothing itself, so for it this checks only that `install.sh`
    is not piped."""
    result = _run_install(example, tmp_path, _FAILING_CURL)
    if _INSPECT not in example:  # the inspect block's last line is a check of its own
        assert result.returncode != 0, (page, example, result)
    assert "404" in result.stderr, (page, example, result.stderr)
    assert not (tmp_path / ".loom").exists(), (page, example)


@pytest.mark.parametrize(
    ("page", "example"), [case for case in _INSTALLS if _INSPECT not in case[1]]
)
def test_a_documented_install_runs_the_installer_and_cleans_up(
    page: str, example: str, tmp_path: Path
) -> None:
    """With a download that works, every install example runs the installer and leaves
    no saved copy behind."""
    result = _run_install(example, tmp_path, _WORKING_CURL)
    assert result.returncode == 0, (page, example, result)
    assert (tmp_path / ".loom").exists(), (page, example)
    assert list(tmp_path.rglob("loom-install.sh")) == [], (page, example)
    # What the command gives the installer reaches it: the words after the saved file,
    # and the LOOM_* assignments before `sh`.
    run = _RUN_STEP.search(example.splitlines()[-1])
    assert run, (page, example)
    assert (tmp_path / ".loom" / "args").read_text().split() == run["args"].split(), example
    passed = (tmp_path / ".loom" / "env").read_text().splitlines()
    assert all(v in passed for v in run["env"].split()), (example, passed)


def test_the_inspect_path_installs_from_the_checked_bundle(tmp_path: Path) -> None:
    """With a real bundle and checksum served, the inspect checks pass, and the install
    step runs the saved installer with that bundle and its checksum."""
    archive, checksum = _build(tmp_path / "release")
    serves = tmp_path / "serves"
    serves.mkdir()
    shutil.copy(archive, serves / "loom-stack.tar.gz")
    shutil.copy(checksum, serves / "loom-stack.tar.gz.sha256")
    home = tmp_path / "home"
    home.mkdir()
    page, checks, install = _inspect_then_install()
    script = checks + "\n" + install
    result = _run_install(script, home, _WORKING_CURL, serves)
    assert result.returncode == 0, (page, script, result)
    assert "loom-stack.tar.gz: OK" in result.stdout, (page, script, result)
    passed = (home / ".loom" / "env").read_text().splitlines()
    assert f"LOOM_INSTALL_BUNDLE={home}/loom-stack.tar.gz" in passed, passed
    assert f"LOOM_INSTALL_CHECKSUM={home}/loom-stack.tar.gz.sha256" in passed, passed
    assert not (home / "loom-install.sh").exists(), (page, install)


def _start_stubs(
    installer_env: dict[str, str],
    *,
    docker_body: str = "exit 0",
    probe_exit: int = 7,
    connect_time: str = "0.000000",
    health: str = '{"status":"ok"}',
    memory_status: int = 200,
    set_saves: bool = True,
    delete_status: int = 200,
    set_status: int | None = None,
    misses: int = 0,
) -> tuple[Path, Path]:
    """Stubs for a started install: a curl whose port probe prints `connect_time` and
    exits `probe_exit`, whose /v1/health calls print `health`, and whose memory calls
    answer `memory_status`: memory.get finds the memory once a
    memory.set saved it (`set_saves`), the body to its -o file; a docker that runs
    `docker_body`. Both log their arguments; returns (curl log, docker log)."""
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    curl_log, docker_log = bin_dir / "curl.log", bin_dir / "docker.log"
    saved = bin_dir / "memory.saved"
    saved.unlink(missing_ok=True)
    # `misses` reads after the save answer not found, as a replica that lags would.
    lag = bin_dir / "memory.lag"
    lag.write_text("x" * misses)
    on_set = f"touch {saved}; " if set_saves else ""
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\necho "$*" >> {curl_log}\n'
        'out=; prev=\nfor a in "$@"; do [ "$prev" = -o ] && out=$a; prev=$a; done\n'
        f"case \"$*\" in *v1/health*) echo '{health}'; exit 0 ;;\n"
        f"  *v1/memory.get*) if [ -f {saved} ] && [ ! -s {lag} ]; then "
        f"echo '{{\"found\":true}}'; elif [ -f {saved} ]; then "
        f'l=$(cat {lag}); printf %s "${{l#x}}" > {lag}; echo \'{{"found":false}}\'; '
        f'else echo \'{{"found":false}}\'; fi > "$out"; printf {memory_status}; exit 0 ;;\n'
        f"  *v1/memory.set*) {on_set}echo '{{}}' > \"$out\"; "
        f"printf {set_status if set_status is not None else memory_status}; exit 0 ;;\n"
        f"  *v1/memory.delete*) rm -f {saved}; echo '{{}}' > \"$out\"; printf {delete_status}; exit 0 ;;\n"
        "esac\n"
        f"printf %s {connect_time}\nexit {probe_exit}\n"
    )
    (bin_dir / "docker").write_text(
        '#!/bin/sh\n[ "$1 $2 $3" = "compose version --short" ] && echo 2.29.1 && exit 0\n'
        f'echo "$*" >> {docker_log}\n{docker_body}\n'
    )
    return curl_log, docker_log


def _started(docker_log: Path) -> bool:
    return any(" pull" in c or " up " in c for c in docker_log.read_text().splitlines())


# This install's server runs and Docker publishes it on 7777.
_OWN_SERVER = """case "$*" in
  *" ps -q --status running --status restarting loom") echo 3f2a ;;
  inspect*) echo " 7777" ;;
esac
exit 0"""


def test_a_port_another_program_holds_stops_before_any_pull(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _, docker_log = _start_stubs(installer_env, probe_exit=0, connect_time="0.000210")
    result = run(LOOM_SKIP_START="0", LOOM_PORT="28977")
    assert result.returncode == 1
    assert "port 28977 on 127.0.0.1 is in use by another program:" in result.stderr
    assert "again with LOOM_PORT=28978 in front of sh" in result.stderr
    assert not _started(docker_log)


def test_a_listener_that_never_answers_holds_the_port(
    install, installer_env: dict[str, str]
) -> None:
    """A timeout after the connection was made: something accepted it."""
    run, _ = install
    _start_stubs(installer_env, probe_exit=28, connect_time="0.000412")
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1
    assert "port 7777 on 127.0.0.1 is in use by another program" in result.stderr


def test_any_exit_after_a_connect_is_a_listener(install, installer_env: dict[str, str]) -> None:
    """An exit not named as a listener's (6 here) still is one when curl connected."""
    run, _ = install
    _start_stubs(installer_env, probe_exit=6, connect_time="0.000150")
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1
    assert "port 7777 on 127.0.0.1 is in use by another program" in result.stderr


@pytest.mark.parametrize("connect_time", ["0.000000", "0.000"])
def test_a_timeout_with_no_connect_time_names_both_causes(
    install, installer_env: dict[str, str], connect_time: str
) -> None:
    """Zero as curl prints it now and as old releases print it, also for a fast
    connect: a silent listener and an unreachable address are both named."""
    run, _ = install
    _, docker_log = _start_stubs(installer_env, probe_exit=28, connect_time=connect_time)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1 and not _started(docker_log)
    assert "port 7777 on 127.0.0.1 does not answer:" in result.stderr
    assert "a program may hold it" in result.stderr and "firewall" in result.stderr


@pytest.mark.parametrize("code", [0, 1, 8, 18, 52, 55, 56])
def test_a_listener_is_named_by_curls_exit_alone(
    install, installer_env: dict[str, str], code: int
) -> None:
    """Old curls print a fast loopback connect as 0.000: the exit code still says a
    listener answered."""
    run, _ = install
    _start_stubs(installer_env, probe_exit=code, connect_time="0.000")
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1
    assert "port 7777 on 127.0.0.1 is in use by another program" in result.stderr


@pytest.mark.parametrize(("port", "suggested"), [("7779", "7780"), ("65535", "7778")])
def test_the_suggested_port_is_the_next_one(
    install, installer_env: dict[str, str], port: str, suggested: str
) -> None:
    run, _ = install
    _start_stubs(installer_env, probe_exit=0, connect_time="0.000100")
    result = run(LOOM_SKIP_START="0", LOOM_PORT=port)
    assert f"LOOM_PORT={suggested} in front of sh" in result.stderr


def test_a_probe_curl_cannot_make_is_not_called_in_use(
    install, installer_env: dict[str, str]
) -> None:
    """Curl's 6 (the host does not resolve) says nothing about a listener."""
    run, _ = install
    _, docker_log = _start_stubs(installer_env, probe_exit=6)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1 and not _started(docker_log)
    assert "could not check port 7777 on 127.0.0.1 (curl exit 6)" in result.stderr
    assert "in use" not in result.stderr


@pytest.mark.parametrize("status", ["ok", "degraded"])
def test_a_port_another_loom_holds_is_named_as_loom(
    install, installer_env: dict[str, str], status: str
) -> None:
    """Named from its health body, also the `degraded` one a Loom whose ClickHouse
    is down answers with a 503, which curl without `-f` passes through."""
    run, _ = install
    health = f'{{"status":"{status}","schema_version":"1"}}'
    _start_stubs(installer_env, probe_exit=0, connect_time="0.000120", health=health)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1
    assert "port 7777 on 127.0.0.1 is in use by another Loom server" in result.stderr


def test_a_rerun_over_its_own_running_server_skips_the_port_check(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    curl_log, docker_log = _start_stubs(
        installer_env, docker_body=_OWN_SERVER, probe_exit=0, connect_time="0.000100"
    )
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert not any(c.endswith(":7777/") for c in curl_log.read_text().splitlines())
    assert any(" up -d" in c for c in docker_log.read_text().splitlines())


def test_a_rerun_onto_another_port_probes_that_port(install, installer_env: dict[str, str]) -> None:
    """Its own server publishes 7777, not the 7778 asked for: 7778 is probed."""
    run, _ = install
    _, docker_log = _start_stubs(
        installer_env, docker_body=_OWN_SERVER, probe_exit=0, connect_time="0.000100"
    )
    result = run(LOOM_SKIP_START="0", LOOM_PORT="7778")
    assert result.returncode == 1
    assert "port 7778 on 127.0.0.1 is in use" in result.stderr
    assert "LOOM_PORT=7779" in result.stderr
    assert not _started(docker_log)


def test_a_rerun_onto_a_free_port_installs(install, installer_env: dict[str, str]) -> None:
    """Its own server publishes 7777; 7778, asked for, is probed and free."""
    run, _ = install
    curl_log, docker_log = _start_stubs(installer_env, docker_body=_OWN_SERVER)
    result = run(LOOM_SKIP_START="0", LOOM_PORT="7778")
    assert result.returncode == 0, result.stderr
    assert curl_log.read_text().splitlines()[0].endswith(" http://127.0.0.1:7778/")
    assert any(" up -d" in c for c in docker_log.read_text().splitlines())


def test_the_port_probe_goes_where_loom_is_published(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    curl_log, _ = _start_stubs(installer_env)
    assert run(LOOM_SKIP_START="0", LOOM_PORT="28977", LOOM_BIND_HOST="::").returncode == 0
    assert curl_log.read_text().splitlines()[0].endswith(" http://[::1]:28977/")


@pytest.mark.parametrize(("probe_exit", "installs"), [(7, True), (0, False)])
def test_every_call_to_the_local_server_bypasses_a_proxy(
    install, installer_env: dict[str, str], probe_exit: int, installs: bool
) -> None:
    """A proxy in the shell would answer for 127.0.0.1 and make the port look held:
    the probe, the health poll and the call that names a holder all go direct."""
    run, _ = install
    curl_log, _ = _start_stubs(installer_env, probe_exit=probe_exit, connect_time="0.000100")
    result = run(LOOM_SKIP_START="0", http_proxy="http://proxy.invalid:3128")
    assert (result.returncode == 0) == installs, result.stderr
    calls = curl_log.read_text().splitlines()
    assert any("v1/health" in c for c in calls), calls
    assert all("--noproxy *" in c for c in calls), calls


def test_a_failed_pull_names_dockers_proxy_settings(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    body = 'case "$*" in *" pull"*) exit 1 ;; esac\nexit 0'
    _, docker_log = _start_stubs(installer_env, docker_body=body)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 1
    assert "could not pull the Loom images" in result.stderr
    assert "docs.docker.com/engine/daemon/proxy" in result.stderr
    assert not any(" up " in c for c in docker_log.read_text().splitlines())


def _agent_cli(installer_env: dict[str, str], name: str, plugins: str = "", code: int = 0) -> Path:
    """A stub agent CLI that logs its arguments, prints `plugins` for `plugin list`
    (escapes read by printf %b) and exits `code`; returns its log."""
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    log = bin_dir / f"{name}.log"
    cli = bin_dir / name
    cli.write_text(
        f'#!/bin/sh\necho "$*" >> {log}\n'
        f'case "$*" in "plugin list") printf \'%b\\n\' "{plugins}" ;; esac\nexit {code}\n'
    )
    cli.chmod(0o755)
    log.write_text("")
    return log


def test_a_new_install_saves_reads_back_and_deletes_a_test_memory(
    install, installer_env: dict[str, str]
) -> None:
    """Proof that writes and reads work, which leaves no memory behind: a kept one
    would read as the user's first (the dashboard's first-run checklist)."""
    run, target = install
    curl_log, _ = _start_stubs(installer_env)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "Saved, read back and deleted a test memory: writes and reads work." in result.stdout
    assert not (target / ".memory-check-pending").exists()
    calls = curl_log.read_text().splitlines()
    ops = [op for c in calls for op in ("memory.get", "memory.set", "memory.delete") if op in c]
    assert ops == ["memory.get", "memory.set", "memory.get", "memory.delete"]
    sets = [c for c in calls if "memory.set" in c]
    # Agent "-": the dashboard lists no connected agent for the check.
    assert '"org":"dev","namespace":"local","agent":"-"' in sets[0]
    assert '"memory_key":"loom/installer-check"' in sets[0]
    assert all("--noproxy *" in c for c in calls)


def test_a_failed_run_checks_on_the_next_one(install, installer_env: dict[str, str]) -> None:
    """The marker a new .env comes with stays until the check passes."""
    run, target = install
    assert run().returncode == 0  # files only, as a run that stopped early
    assert (target / ".memory-check-pending").exists()
    curl_log, _ = _start_stubs(installer_env)
    assert run(LOOM_SKIP_START="0").returncode == 0
    assert any("memory.set" in c for c in curl_log.read_text().splitlines())


def test_a_rerun_writes_no_memory_again(install, installer_env: dict[str, str]) -> None:
    """The marker is gone once the check passed: a rerun saves no test memory again."""
    run, _ = install
    curl_log, _ = _start_stubs(installer_env)
    assert run(LOOM_SKIP_START="0").returncode == 0
    curl_log.write_text("")
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "test memory" not in result.stdout
    assert not any("memory.set" in c for c in curl_log.read_text().splitlines())


def test_an_upgrade_from_before_writes_no_memory(install, installer_env: dict[str, str]) -> None:
    """An install whose .env came before this release has data of its own."""
    run, target = install
    assert run().returncode == 0  # files only
    (target / ".memory-check-pending").unlink()
    curl_log, _ = _start_stubs(installer_env)
    assert run(LOOM_SKIP_START="0").returncode == 0
    assert not any("memory.set" in c for c in curl_log.read_text().splitlines())


@pytest.mark.parametrize("status", [401, 403])
def test_sign_in_on_connects_no_agent(install, installer_env: dict[str, str], status: int) -> None:
    run, _ = install
    _start_stubs(installer_env, memory_status=status)
    claude = _agent_cli(installer_env, "claude")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code")
    assert result.returncode == 0, result.stderr
    assert "Sign-in is on" in result.stdout
    assert claude.read_text() == ""


def test_loom_connect_installs_the_claude_code_plugin(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code", LOOM_PORT="28977")
    assert result.returncode == 0, result.stderr
    calls = claude.read_text().splitlines()
    assert "plugin marketplace add ClickHouse/loom" in calls
    assert "plugin install loom@loom --config server_url=http://localhost:28977" in calls
    assert "mcp list" not in calls
    assert "Claude Code: connected to http://localhost:28977" in result.stdout
    assert "Connected: Claude Code." in result.stdout


@pytest.mark.parametrize("port", ["7777", "28977"])
def test_a_claude_plugin_from_before_is_left_as_it_is(
    install, installer_env: dict[str, str], port: str
) -> None:
    """It keeps the server it was set up for: the run says how to point it here and
    neither reinstalls it nor counts it as connected."""
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude", "  ❯ loom@loom\\n    Status: ✔ enabled")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code", LOOM_PORT=port)
    assert result.returncode == 0, result.stderr
    assert claude.read_text().splitlines() == ["plugin list"]
    assert f"--config server_url=http://localhost:{port}` points it here" in result.stdout
    assert "Connected:" not in result.stdout
    assert "Your agents keep the Loom setup they had." in result.stdout


def test_without_a_terminal_or_loom_connect_no_agent_is_changed(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    codex = _agent_cli(installer_env, "codex")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="")
    assert result.returncode == 0, result.stderr
    # Nothing could be connected, so no client is even looked at.
    assert claude.read_text() == "" and codex.read_text() == ""
    assert "Connect an agent:" in result.stdout


def test_loom_connect_adds_the_codex_plugin(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex")
    assert result.returncode == 0, result.stderr
    assert "plugin add loom@loom" in codex.read_text().splitlines()
    assert "approve the plugin's hooks" in result.stdout


def test_a_first_memory_that_does_not_read_back_is_named(
    install, installer_env: dict[str, str]
) -> None:
    run, target = install
    _start_stubs(installer_env, set_saves=False)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "Saved a test memory but could not read it back" in result.stdout
    assert (target / ".memory-check-pending").exists()


def test_a_namespace_a_json_body_cannot_hold_skips_the_memory_check(
    install, installer_env: dict[str, str]
) -> None:
    run, target = install
    assert run().returncode == 0
    with (target / ".env").open("a") as env:
        env.write('LOOM_DEFAULT_NAMESPACE=my"space\n')
    curl_log, _ = _start_stubs(installer_env)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "Skipped the memory check" in result.stdout
    assert not any("memory." in c for c in curl_log.read_text().splitlines())


def test_codex_on_another_port_is_pointed_at_its_docs(
    install, installer_env: dict[str, str]
) -> None:
    """Codex plugins take no options: its fixed URL cannot reach another port."""
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex", LOOM_PORT="28977")
    assert result.returncode == 0, result.stderr
    assert "plugin add loom@loom" not in codex.read_text().splitlines()
    assert "Codex: its Loom plugin always uses http://localhost:7777" in result.stdout
    assert "Connected:" not in result.stdout


def test_loom_connect_vscode_adds_the_mcp_server(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    _start_stubs(installer_env)
    code = _agent_cli(installer_env, "code")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode")
    assert result.returncode == 0, result.stderr
    assert code.read_text().splitlines() == [
        '--add-mcp {"name":"loom","type":"http","url":"http://localhost:7777/mcp?toolset=coding"}'
    ]
    assert "Connected: VS Code." in result.stdout


@pytest.mark.parametrize("remote", ["CODESPACES", "REMOTE_CONTAINERS", "SSH_CONNECTION"])
def test_vscode_inside_a_remote_is_not_offered(
    install, installer_env: dict[str, str], remote: str
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    code = _agent_cli(installer_env, "code")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode", **{remote: "1"})
    assert result.returncode == 0, result.stderr
    assert code.read_text() == ""
    assert "VS Code: not offered in a remote session or WSL" in result.stdout


def test_loom_connect_all_connects_every_client(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    codex = _agent_cli(installer_env, "codex")
    code = _agent_cli(installer_env, "code")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="all")
    assert result.returncode == 0, result.stderr
    assert "plugin install loom@loom --config server_url=http://localhost:7777" in (
        claude.read_text().splitlines()
    )
    assert "plugin add loom@loom" in codex.read_text().splitlines()
    assert any(c.startswith("--add-mcp ") for c in code.read_text().splitlines())
    assert "Connected: Claude Code, Codex, VS Code." in result.stdout


def test_a_server_error_connects_no_agent(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    _start_stubs(installer_env, memory_status=500)
    claude = _agent_cli(installer_env, "claude")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code")
    assert result.returncode == 0, result.stderr
    assert "Could not read memories (HTTP 500)" in result.stdout
    assert claude.read_text() == ""


@pytest.mark.parametrize("status", [401, 500, None])
def test_a_server_that_refused_the_check_gets_no_connect_guide(
    install, installer_env: dict[str, str], status: int | None
) -> None:
    """Without a terminal or LOOM_CONNECT: the refusal said why, so no guide follows.
    None is a namespace no check can use."""
    run, target = install
    if status is None:
        assert run().returncode == 0
        with (target / ".env").open("a") as env:
            env.write('LOOM_DEFAULT_NAMESPACE=my"space\n')
    _start_stubs(installer_env, memory_status=status or 200)
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="")
    assert result.returncode == 0, result.stderr
    if status is None:
        assert "Skipped the memory check" in result.stdout
    assert "Connect an agent:" not in result.stdout


def _desktop(installer_env: dict[str, str]) -> tuple[dict[str, str], Path]:
    """A Linux desktop for a started install: DISPLAY set, a `uname` that says Linux
    and an `xdg-open` that logs the URL it gets. Returns (env, xdg-open log)."""
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    log = bin_dir / "xdg-open.log"
    # The installer starts xdg-open in the background and exits: on the test's
    # pseudo-terminal its exit hangs up the terminal, which must not kill the stub.
    opener = f'trap \'\' HUP\necho "$*" >> "{log}"'
    for name, body in (("uname", "echo Linux"), ("xdg-open", opener)):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env = {k: v for k, v in installer_env.items() if k != "LOOM_NO_OPEN"}
    return {**env, "LOOM_SKIP_START": "0", "DISPLAY": ":0"}, log


def _opened(log: Path, wait: float = 1) -> list[str]:
    """What xdg-open got. It runs in the background, so a call that expects it to
    have run waits up to `wait` seconds for it."""
    deadline = time.monotonic() + wait
    while not (log.exists() and log.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return log.read_text().splitlines() if log.exists() else []


def test_a_new_install_on_a_desktop_opens_the_dashboard_once(installer_env: dict[str, str]) -> None:
    _start_stubs(installer_env)
    env, log = _desktop(installer_env)
    _on_terminal(env, [], None)
    assert _opened(log, wait=60) == ["http://127.0.0.1:7777/"]
    log.unlink()
    _on_terminal(env, [], None)  # a rerun keeps its .env
    assert _opened(log) == []


def test_the_first_run_that_starts_loom_opens_the_dashboard(installer_env: dict[str, str]) -> None:
    """A run that wrote .env but stopped before Loom was up (a taken port, say)
    leaves the next one a new install."""
    _start_stubs(installer_env)
    env, log = _desktop(installer_env)
    _on_terminal({**env, "LOOM_SKIP_START": "1"}, [], None)
    assert _opened(log) == []
    _on_terminal(env, [], None)
    assert _opened(log, wait=60) == ["http://127.0.0.1:7777/"]


def test_a_namespace_no_check_can_use_opens_the_dashboard_once(
    installer_env: dict[str, str],
) -> None:
    _start_stubs(installer_env)
    env, log = _desktop(installer_env)
    _on_terminal({**env, "LOOM_SKIP_START": "1"}, [], None)
    target = Path(installer_env["LOOM_INSTALL_DIR"])
    dotenv = target / ".env"
    dotenv.write_text(dotenv.read_text() + 'LOOM_DEFAULT_NAMESPACE=my"space\n')
    _on_terminal(env, [], None)
    assert _opened(log, wait=60) == ["http://127.0.0.1:7777/"]
    assert not (target / ".memory-check-pending").exists()
    log.unlink()
    _on_terminal(env, [], None)
    assert _opened(log) == []


@pytest.mark.parametrize(
    "extra", [{"LOOM_NO_OPEN": "1"}, {"SSH_CONNECTION": "10.0.0.1 2 10.0.0.2 22"}]
)
def test_the_dashboard_is_not_opened_when_asked_not_to_or_over_ssh(
    installer_env: dict[str, str], extra: dict[str, str]
) -> None:
    _start_stubs(installer_env)
    env, log = _desktop(installer_env)
    _on_terminal({**env, **extra}, [], None)
    assert _opened(log) == []


def test_a_linux_terminal_with_no_display_opens_no_dashboard(installer_env: dict[str, str]) -> None:
    _start_stubs(installer_env)
    env, log = _desktop(installer_env)
    _on_terminal({k: v for k, v in env.items() if k != "DISPLAY"}, [], None)
    assert _opened(log) == []


@pytest.mark.parametrize(
    ("name", "client", "retry"),
    [
        (
            "claude",
            "claude-code",
            "claude plugin marketplace add ClickHouse/loom; claude plugin install loom@loom"
            " --config server_url=http://localhost:7777",
        ),
        (
            "codex",
            "codex",
            "codex plugin marketplace add ClickHouse/loom; codex plugin add loom@loom",
        ),
    ],
)
def test_a_failed_plugin_install_prints_the_commands_to_try_again(
    install, installer_env: dict[str, str], name: str, client: str, retry: str
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    cli = Path(installer_env["PATH"].split(":")[0]) / name
    cli.write_text('#!/bin/sh\ncase "$*" in "plugin list") exit 0 ;; esac\nexit 1\n')
    cli.chmod(0o755)
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT=client)
    assert result.returncode == 0, result.stderr
    assert f"to try again: {retry}\n" in result.stdout


@pytest.mark.parametrize(("answer", "installs"), [(b"\n", True), (b"n\n", False)])
def test_a_terminal_is_asked_before_an_agent_is_connected(
    installer_env: dict[str, str], answer: bytes, installs: bool
) -> None:
    """Enter means yes; anything starting with n means no."""
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    env = {k: v for k, v in installer_env.items() if k != "LOOM_CONNECT"}
    seen = _on_terminal({**env, "LOOM_SKIP_START": "0"}, [], b"[Y/n]", answer)
    assert b"Connect Claude Code to Loom? [Y/n]" in seen
    installed = "plugin install loom@loom --config server_url=http://localhost:7777"
    assert (installed in claude.read_text().splitlines()) == installs


def test_a_codex_marketplace_without_the_plugin_is_offered_the_plugin(
    install, installer_env: dict[str, str]
) -> None:
    """`codex plugin list` shows `not installed` once the marketplace is added."""
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex", "loom@loom  not installed           /x")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex")
    assert result.returncode == 0, result.stderr
    assert "plugin add loom@loom" in codex.read_text().splitlines()
    assert "Codex: connected" in result.stdout


def test_a_client_loom_connect_does_not_know_is_refused(install) -> None:
    run, _ = install
    result = run(LOOM_CONNECT="claude,codex")
    assert result.returncode == 1
    assert "LOOM_CONNECT takes claude-code, codex, vscode, all or none" in result.stderr
    assert ": claude" in result.stderr


def test_loom_connect_none_runs_no_agent_cli(install, installer_env: dict[str, str]) -> None:
    run, _ = install
    _start_stubs(installer_env)
    logs = [
        _agent_cli(installer_env, name, "  ❯ loom@loom") for name in ("claude", "codex", "code")
    ]
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert [log.read_text() for log in logs] == ["", "", ""]


@pytest.mark.parametrize("status", ["installed, enabled ", "installed, disabled"])
def test_a_codex_plugin_from_before_is_left_as_it_is(
    install, installer_env: dict[str, str], status: str
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex", f"loom@loom  {status}  1.2.21  /x")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex")
    assert result.returncode == 0, result.stderr
    assert codex.read_text().splitlines() == ["plugin list"]
    assert "Codex: the Loom plugin is installed already" in result.stdout
    assert "Connected:" not in result.stdout


@pytest.mark.parametrize("value", ["claude-code, codex", "claude-code codex", "*", "Codex"])
def test_a_loom_connect_with_spaces_or_other_characters_is_refused(install, value: str) -> None:
    run, _ = install
    result = run(LOOM_CONNECT=value)
    assert result.returncode == 1
    assert "LOOM_CONNECT takes claude-code, codex, vscode, all or none" in result.stderr


def test_a_loom_mcp_server_vscode_has_is_left_as_it_is(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    code = _agent_cli(installer_env, "code")

    user_dir = "Library/Application Support" if platform.system() == "Darwin" else ".config"
    mcp = Path(installer_env["HOME"]) / user_dir / "Code/User/mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text('{"servers": {"loom": {"type": "http", "url": "https://loom.example/mcp"}}}')
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode")
    assert result.returncode == 0, result.stderr
    assert code.read_text() == ""
    assert "VS Code: a loom MCP server is set up already" in result.stdout


def test_a_claude_mcp_entry_from_before_is_left_as_it_is(
    install, installer_env: dict[str, str]
) -> None:
    """`claude mcp add loom` (or `loom install`) set it up: the plugin would list
    every tool twice."""
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    config = Path(installer_env["HOME"]) / ".claude.json"
    config.write_text(
        '{"mcpServers": {"loom": {"type": "http", "url": "http://localhost:7777/mcp"}}}'
    )
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code")
    assert result.returncode == 0, result.stderr
    assert claude.read_text().splitlines() == ["plugin list"]
    assert "Claude Code: a loom MCP server is set up already" in result.stdout


def test_a_codex_mcp_entry_from_before_is_left_as_it_is(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex")
    config = Path(installer_env["HOME"]) / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.loom]\nurl = "http://localhost:7777/mcp"\n')
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex")
    assert result.returncode == 0, result.stderr
    assert codex.read_text().splitlines() == ["plugin list"]
    assert "Codex: a loom MCP server is set up already" in result.stdout


def test_vscode_under_wsl_is_not_offered(install, installer_env: dict[str, str]) -> None:
    """There `code` is the Windows VS Code, whose profile this run cannot read."""
    run, _ = install
    _start_stubs(installer_env)
    code = _agent_cli(installer_env, "code")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode", WSL_DISTRO_NAME="Ubuntu")
    assert result.returncode == 0, result.stderr
    assert "VS Code: not offered in a remote session or WSL" in result.stdout
    assert code.read_text() == ""


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_plugin_list_that_fails_leaves_the_client_alone(
    install, installer_env: dict[str, str], name: str
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    log = _agent_cli(installer_env, name, code=1)
    client = "claude-code" if name == "claude" else "codex"
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT=client)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["plugin list"]
    assert "could not list its plugins (above), so it is left as it is" in result.stdout


def _node_reports(installer_env: dict[str, str], body: str) -> None:
    node = Path(installer_env["PATH"].split(":")[0]) / "node"
    node.write_text(f"#!/bin/sh\n{body}\n")


@pytest.mark.parametrize("node", ["exit 127", "echo 16"])
def test_claude_code_without_node_18_on_path_gets_the_mcp_line_instead(
    install, installer_env: dict[str, str], node: str
) -> None:
    """Its hooks and MCP header helper run the `node` on PATH: no plugin is
    installed, and the MCP-only line is printed."""
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    _node_reports(installer_env, node)
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code")
    assert result.returncode == 0, result.stderr
    assert claude.read_text().splitlines() == ["plugin list"]
    assert 'claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding"' in (
        result.stdout
    )
    assert "Connected:" not in result.stdout


def test_a_test_memory_that_does_not_delete_is_tried_again(
    install, installer_env: dict[str, str]
) -> None:
    """A live test memory would turn the dashboard's first-run checklist off."""
    run, target = install
    _start_stubs(installer_env, delete_status=500)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "could not delete loom/installer-check; the next run tries again" in result.stdout
    assert (target / ".memory-check-pending").exists()


def test_codex_without_node_on_path_gets_the_mcp_line_too(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    codex = _agent_cli(installer_env, "codex")
    _node_reports(installer_env, "exit 127")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex")
    assert result.returncode == 0, result.stderr
    assert "plugin add loom@loom" in codex.read_text().splitlines()
    assert 'codex mcp add loom --url "http://localhost:7777/mcp?toolset=coding"' in result.stdout


@pytest.mark.parametrize("value", ["none,claude-code", "codex,none"])
def test_none_with_another_name_is_refused(install, value: str) -> None:
    run, _ = install
    result = run(LOOM_CONNECT=value)
    assert result.returncode == 1
    assert "LOOM_CONNECT=none connects no agent, so it takes no other name" in result.stderr


def test_claude_config_dir_is_read_for_an_mcp_entry(
    install, installer_env: dict[str, str], tmp_path: Path
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude")
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text('{"mcpServers": {"loom": {"type": "http"}}}')
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="claude-code", CLAUDE_CONFIG_DIR=str(config_dir))
    assert result.returncode == 0, result.stderr
    assert claude.read_text().splitlines() == ["plugin list"]
    assert "a loom MCP server is set up already" in result.stdout


def test_sign_in_on_clears_the_memory_check(install, installer_env: dict[str, str]) -> None:
    """A server with sign-in has no check to run, on this run or after a switch."""
    run, target = install
    _start_stubs(installer_env, memory_status=401)
    assert run(LOOM_SKIP_START="0").returncode == 0
    assert not (target / ".memory-check-pending").exists()


def test_a_save_that_does_not_read_back_is_still_deleted(
    install, installer_env: dict[str, str]
) -> None:
    """It may have landed after the read: no live test memory stays."""
    run, _ = install
    curl_log, _ = _start_stubs(installer_env, set_saves=False)
    assert run(LOOM_SKIP_START="0").returncode == 0
    assert any("memory.delete" in c for c in curl_log.read_text().splitlines())


@pytest.mark.skipif(
    any(Path(d, "code").exists() for d in ("/usr/bin", "/bin")),
    reason="a real VS Code CLI is on the trimmed PATH here",
)
def test_a_client_loom_connect_names_but_this_machine_lacks_is_named(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    bin_dir = Path(installer_env["PATH"].split(":")[0])
    (bin_dir / "code").unlink()
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode", PATH=f"{bin_dir}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    assert "VS Code: `code` is not on PATH" in result.stdout


def test_a_save_without_a_200_keeps_the_marker_and_deletes_nothing_yet(
    install, installer_env: dict[str, str]
) -> None:
    """A slow first save may still land: the next run checks again and deletes it."""
    run, target = install
    curl_log, _ = _start_stubs(installer_env, set_status=0)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "Could not confirm a test memory saved" in result.stdout
    assert (target / ".memory-check-pending").exists()
    assert not any("memory.delete" in c for c in curl_log.read_text().splitlines())


def test_loom_connect_naming_one_client_leaves_the_others_alone(
    install, installer_env: dict[str, str]
) -> None:
    run, _ = install
    _start_stubs(installer_env)
    claude = _agent_cli(installer_env, "claude", "  ❯ loom@loom")
    codex = _agent_cli(installer_env, "codex")
    code = _agent_cli(installer_env, "code")
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode")
    assert result.returncode == 0, result.stderr
    assert claude.read_text() == "" and codex.read_text() == ""
    assert any(c.startswith("--add-mcp ") for c in code.read_text().splitlines())


@pytest.mark.parametrize("value", [",codex", "codex,", "claude-code,,codex"])
def test_an_empty_loom_connect_item_is_refused(install, value: str) -> None:
    run, _ = install
    result = run(LOOM_CONNECT=value)
    assert result.returncode == 1
    assert "separated by single commas" in result.stderr


def test_xdg_config_home_holds_the_vscode_mcp_json(
    install, installer_env: dict[str, str], tmp_path: Path
) -> None:
    if platform.system() == "Darwin":
        pytest.skip("macOS keeps VS Code's user data under ~/Library")
    run, _ = install
    _start_stubs(installer_env)
    code = _agent_cli(installer_env, "code")
    mcp = tmp_path / "xdg/Code/User/mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text('{"servers": {"loom": {"type": "http"}}}')
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="vscode", XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    assert result.returncode == 0, result.stderr
    assert code.read_text() == ""


def test_a_read_a_lagging_replica_misses_is_tried_again(
    install, installer_env: dict[str, str]
) -> None:
    run, target = install
    curl_log, _ = _start_stubs(installer_env, misses=2)
    result = run(LOOM_SKIP_START="0")
    assert result.returncode == 0, result.stderr
    assert "Saved, read back and deleted a test memory" in result.stdout
    assert not (target / ".memory-check-pending").exists()
    gets = [c for c in curl_log.read_text().splitlines() if "memory.get" in c]
    assert len(gets) == 4  # the sign-in read, then two misses and a hit


def test_a_codex_mcp_entry_on_another_port_is_kept(
    install, installer_env: dict[str, str], tmp_path: Path
) -> None:
    """Set up by its docs' override (in CODEX_HOME here): not told to do it again."""
    run, _ = install
    _start_stubs(installer_env)
    _agent_cli(installer_env, "codex")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.loom]\nurl = "http://localhost:28977/mcp"\n')
    result = run(LOOM_SKIP_START="0", LOOM_CONNECT="codex", LOOM_PORT="28977", CODEX_HOME=str(home))
    assert result.returncode == 0, result.stderr
    assert "Codex: a loom MCP server is set up already" in result.stdout
    assert "always uses" not in result.stdout


LOCAL_PRESET = {
    "LOOM_LLM_PROVIDER": "local",
    "LOOM_LLM_MODEL": "qwen2.5:3b",
    "LOOM_LOCAL_LLM_BASE_URL": "http://host.docker.internal:11434/v1",
    "LOOM_ASSISTANT_PROVIDER": "local",
    "LOOM_ASSISTANT_MODEL": "qwen2.5:3b",
    "LOOM_MEMORY_EXTRACT_VERIFY_MODELS": "qwen2.5:3b",
    "LOOM_CONFLICT_RESOLVER_MODEL": "qwen2.5:3b",
    "LOOM_SESSION_BRIEF_LLM_MODEL": "",
    "LOOM_RETRIEVAL_JUDGE_MODEL": "",
}


def test_local_models_ollama_sets_every_model_job_on_it(install) -> None:
    run, target = install
    out = run(LOOM_LOCAL_MODELS="ollama")
    assert out.returncode == 0, out.stderr
    assert {k: _env(target)[k] for k in LOCAL_PRESET} == LOCAL_PRESET
    # The stub curl answers nothing, so the model is not there yet.
    assert "ollama pull qwen2.5:3b" in out.stdout


def test_without_a_terminal_ollama_is_not_offered(install) -> None:
    run, target = install
    assert run().returncode == 0
    assert _env(target)["LOOM_LLM_PROVIDER"] == "fake"
    assert "LOOM_LOCAL_LLM_BASE_URL" not in _env(target)


def test_local_models_takes_only_ollama_or_off(install) -> None:
    run, _ = install
    out = run(LOOM_LOCAL_MODELS="llama")
    assert out.returncode != 0 and "LOOM_LOCAL_MODELS takes ollama or off" in out.stderr


def _ollama_answers(installer_env: dict[str, str]) -> None:
    """Every curl succeeds, as with Ollama listening here and holding the model."""
    curl = Path(installer_env["PATH"].split(":", 1)[0]) / "curl"
    curl.write_text('#!/bin/sh\necho \'{"models":[{"name":"qwen2.5:3b"}]}\'\n')


def test_a_new_install_on_a_terminal_is_offered_a_running_ollama(installer_env) -> None:
    _ollama_answers(installer_env)
    seen = _on_terminal(installer_env, [], b"Use it for extraction", b"\n")
    assert b"Loom will run its model jobs on Ollama" in seen, seen
    assert b"ollama pull" not in seen
    assert _env(Path(installer_env["LOOM_INSTALL_DIR"]))["LOOM_LLM_PROVIDER"] == "local"


def test_a_rerun_or_off_is_not_offered_ollama(install, installer_env) -> None:
    run, target = install
    assert run().returncode == 0
    _ollama_answers(installer_env)
    # A rerun over the .env the first run wrote: its models stay as they are.
    seen = _on_terminal(installer_env, [], None)
    assert b"Use it for extraction" not in seen, seen
    assert _env(target)["LOOM_LLM_PROVIDER"] == "fake"
    # A new install with LOOM_LOCAL_MODELS=off is not asked either.
    (target / ".env").unlink()
    seen = _on_terminal({**installer_env, "LOOM_LOCAL_MODELS": "off"}, [], None)
    assert b"Use it for extraction" not in seen, seen
    assert _env(target)["LOOM_LLM_PROVIDER"] == "fake"
