"""A hostile repository job inside the real sandbox: it tries to read the
server's secrets, reach the metadata address, loopback and private ranges (directly,
through the egress proxy, through a redirect and through a .gitmodules URL), escape
with `unshare`, fork-bomb, and leave a process behind. Every attempt fails and
nothing outlives the job.

Needs a host where bubblewrap can create user namespaces; skipped elsewhere."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import server.core.hosts as hosts
from server.code.app.index import compiler_tier, egress, sandbox
from server.code.app.index.gitrepo import auth_env

pytestmark = pytest.mark.skipif(
    not sandbox.sandbox_available(), reason="bubblewrap cannot create namespaces here"
)


@pytest.fixture(autouse=True)
def _real_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_available", True)


CANARY = "canary-7f3a9c"
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


async def _python(code: str, **kw) -> sandbox.JobResult:
    return await sandbox.run_sandboxed(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        env=ENV,
        timeout_s=kw.pop("timeout_s", 30),
        merge_stderr=True,
        **kw,
    )


def _running(marker: str) -> list[int]:
    """Host pids whose command line carries `marker`."""
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if marker.encode() in cmdline:
                pids.append(int(entry.name))
    return pids


async def test_host_secrets_are_out_of_reach(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "server-home"
    home.mkdir()
    (home / "secret.txt").write_text(CANARY)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOOM_CANARY_SECRET", CANARY)
    done = await _python(
        f"""
        import os
        for path in ({str(home / "secret.txt")!r}, "/proc/{os.getpid()}/environ",
                     "/proc/1/environ", "/etc/shadow", "/var/run/docker.sock"):
            try:
                print(path, open(path, "rb").read()[:200])
            except OSError as e:
                print(path, "refused", type(e).__name__)
        print("env", sorted(os.environ))
        """
    )
    assert done.returncode == 0, done.stdout
    assert CANARY.encode() not in done.stdout
    assert b"LOOM_CANARY_SECRET" not in done.stdout


async def test_no_direct_network(tmp_path: Path) -> None:
    listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        done = await _python(
            f"""
            import socket
            for host, port in (("169.254.169.254", 80), ("127.0.0.1", {port}),
                               ("10.0.0.1", 443), ("1.1.1.1", 443)):
                try:
                    socket.create_connection((host, port), timeout=2)
                    print("REACHED", host, port)
                except OSError as e:
                    print("refused", host, type(e).__name__)
            try:
                socket.getaddrinfo("example.com", 443)
                print("REACHED dns")
            except OSError:
                print("no dns")
            """
        )
    finally:
        listener.close()
    assert b"REACHED" not in done.stdout, done.stdout


async def test_the_proxy_refuses_private_targets_and_redirects_to_them(
    tmp_path: Path, monkeypatch
) -> None:
    """public.example is a test server that answers every request with a redirect
    to private.example; the job follows it the way git or npm would, with a new
    CONNECT, and the proxy refuses that hop."""

    async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://private.example/x\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(redirect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_vet, real_open = egress.vet_host, asyncio.open_connection

    async def vet(host: str) -> tuple[str, str]:
        if host == "public.example":
            return "", "127.0.0.1"  # stands in for a public address
        return await real_vet(host)

    async def open_connection(host: str, p: int, **kw):
        return await real_open(host, port if host == "127.0.0.1" else p, **kw)

    monkeypatch.setattr(egress, "vet_host", vet)
    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(hosts, "_resolve", lambda h: ["10.0.0.5"] if "private" in h else [h])
    try:
        done = await _python(
            f"""
            import socket
            def connect(target):
                s = socket.create_connection(("127.0.0.1", {sandbox.RELAY_PORT}), timeout=5)
                s.sendall(f"CONNECT {{target}} HTTP/1.1\\r\\n\\r\\n".encode())
                return s, s.recv(4096)
            for target in ("169.254.169.254:443", "169.254.169.254:80", "127.0.0.1:443",
                           "10.0.0.1:443", "private.example:443"):
                print(target, connect(target)[1][:12])
            s, head = connect("public.example:443")
            print("public", head[:12])
            s.sendall(b"GET /repo.git/info/refs HTTP/1.1\\r\\nHost: public.example\\r\\n\\r\\n")
            location = s.recv(4096).split(b"Location: ")[1].split(b"\\r\\n")[0].decode()
            host = location.split("/")[2]
            print("redirect", host, connect(host + ":443")[1][:12])
            """,
            network=True,
        )
    finally:
        server.close()
    out = done.stdout.decode()
    assert done.returncode == 0, out
    assert "public b'HTTP/1.1 200'" in out
    lines = [ln for ln in out.splitlines() if not ln.startswith("public")]
    assert lines and all("HTTP/1.1 40" in ln for ln in lines), out


async def test_submodule_urls_to_private_hosts_and_local_transports_fail(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(hosts, "_resolve", lambda h: [h])  # address literals only
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"}
    env = {**auth_env(), **ident, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], check=True, env=env, capture_output=True, text=True)
        return done.stdout.strip()

    secret_repo, repo, pwned = tmp_path / "secret-repo", tmp_path / "checkout", tmp_path / "pwned"
    git("init", "-q", str(secret_repo))
    git("-C", str(secret_repo), "commit", "-q", "--allow-empty", "-m", "secret")
    pin = git("-C", str(secret_repo), "rev-parse", "HEAD")
    git("init", "-q", str(repo))
    urls = {
        "meta": "https://169.254.169.254/x.git",
        "private": "https://10.0.0.1/x.git",
        "local": f"file://{secret_repo}",
        "path": str(secret_repo),
        "ext": f"ext::sh -c touch% {pwned}",
    }
    (repo / ".gitmodules").write_text(
        "".join(f'[submodule "{n}"]\n\tpath = {n}\n\turl = {u}\n' for n, u in urls.items())
    )
    for name in urls:
        git("-C", str(repo), "update-index", "--add", "--cacheinfo", f"160000,{pin},{name}")
    git("-C", str(repo), "add", ".gitmodules")
    git("-C", str(repo), "commit", "-q", "-m", "submodules")

    caplog.set_level("WARNING", logger="loom.code_index.egress")
    with pytest.raises(RuntimeError) as failed:
        await compiler_tier.git(repo, "submodule", "update", "--init", "--recursive", network=True)
    assert not pwned.exists()
    assert not any((repo / name / ".git").exists() for name in urls)
    text = str(failed.value) + caplog.text
    assert "a private or local address" in caplog.text, text


async def test_unshare_is_refused_inside(tmp_path: Path) -> None:
    done = await _python(
        """
        import os
        try:
            os.unshare(os.CLONE_NEWUSER)
            print("ESCAPED")
        except OSError as e:
            print("refused", e.errno)
        """
    )
    assert b"ESCAPED" not in done.stdout and b"refused" in done.stdout


async def test_a_raw_clone_into_a_new_user_namespace_is_refused(monkeypatch) -> None:
    """clone(CLONE_NEWUSER) is unshare by another name; refused by seccomp even
    on a bwrap too old for --disable-userns."""
    monkeypatch.setattr(sandbox, "_disable_userns", False)
    done = await _python(
        """
        import ctypes, os, signal
        libc = ctypes.CDLL(None, use_errno=True)
        nr = 220 if os.uname().machine == "aarch64" else 56
        pid = libc.syscall(nr, 0x10000000 | signal.SIGCHLD, 0, 0, 0, 0)
        if pid == 0:
            os._exit(0)
        print("ESCAPED" if pid > 0 else f"refused {ctypes.get_errno()}")
        """
    )
    assert b"ESCAPED" not in done.stdout and b"refused" in done.stdout, done.stdout


async def test_a_fork_bomb_is_bounded_and_nothing_survives(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.settings, "code_sandbox_max_pids", 64)
    marker = "bomb-31337"
    bomb = f"""
        # {marker}
        import os, time
        while True:
            try:
                os.fork()
            except OSError:
                time.sleep(0.01)
        """
    with pytest.raises(TimeoutError):
        await _python(bomb, timeout_s=5)
    await asyncio.sleep(0.5)
    assert _running(marker) == []


async def test_an_orphan_does_not_outlive_its_job() -> None:
    done = await sandbox.run_sandboxed(
        ["sh", "-c", "sleep 4242 & echo started"], env=ENV, timeout_s=10
    )
    assert done.stdout == b"started\n"
    await asyncio.sleep(0.5)
    assert _running("sleep 4242") == []


async def test_the_ssh_relay_is_refused_a_private_host(monkeypatch) -> None:
    """ssh remotes go through the relay's connect mode (GIT_SSH_COMMAND's
    ProxyCommand), and the proxy refuses a host that resolves privately."""
    monkeypatch.setattr(hosts, "_resolve", lambda h: ["10.0.0.5"])
    relay = [sys.executable, "-I", sandbox._RELAY_INSIDE, "connect"]
    done = await sandbox.run_sandboxed(
        [*relay, "git.private.example", "22", f"{sandbox._EGRESS_INSIDE}/proxy.sock"],
        env=ENV,
        timeout_s=20,
        network=True,
        merge_stderr=True,
    )
    assert done.returncode == 1 and b"refused" in done.stdout
