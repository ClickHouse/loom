"""The Python and TypeScript SDKs share `~/.loom/credentials` and its lock:
a session either one stores or refreshes is the other's, and a refresh in
one waits for a refresh in the other. The TypeScript side runs from source
under Node's type stripping (Node 22.6 or later)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from loom import credentials

ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_TS = ROOT / "sdk" / "typescript" / "src" / "credentials.ts"
SERVER = "https://loom.example"

# The source imports its siblings as `./errors.js`, as the compiler wants:
# resolve each to the `.ts` file beside it.
_RESOLVE = """
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (e) {
    if (specifier.endsWith(".js") && context.parentURL?.endsWith(".ts"))
      return next(specifier.slice(0, -3) + ".ts", context);
    throw e;
  }
}
"""

# A fake Loom for the Node side: discovery and a refresh that answers
# `node-a1`, after `$HOLD_MS` and a line on stdout saying it is in flight.
_FAKE_FETCH = """
const META = {
  device_authorization_endpoint: "https://loom.example/oauth/device_authorization",
  token_endpoint: "https://loom.example/oauth/token",
  revocation_endpoint: "https://loom.example/oauth/revoke",
};
const fetch = async (url) => {
  const path = new URL(url).pathname;
  if (path === "/.well-known/oauth-authorization-server") return Response.json(META);
  if (path === "/oauth/token") {
    console.log("refreshing");
    await new Promise((r) => setTimeout(r, Number(process.env.HOLD_MS ?? 0)));
    const body = { access_token: "node-a1", refresh_token: "node-r1", expires_in: 3600 };
    return Response.json(body, { headers: { "Loom-Sessions": "1" } });
  }
  throw new Error(`unexpected ${url}`);
};
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout
    major, minor = (int(p) for p in out.strip().lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        pytest.skip(f"node {out.strip()} cannot strip types (22.6 or later)")
    return node


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", Path("~/.loom/credentials"))
    monkeypatch.delenv("LOOM_PROFILE", raising=False)
    return home


def _script(tmp_path: Path, body: str) -> list[str]:
    """argv running `body` as an ES module with the TS credentials imported."""
    (tmp_path / "resolve.mjs").write_text(_RESOLVE)
    (tmp_path / "register.mjs").write_text(
        'import { register } from "node:module";\nregister("./resolve.mjs", import.meta.url);\n'
    )
    script = tmp_path / "run.mjs"
    script.write_text(
        f"import * as creds from {json.dumps(CREDENTIALS_TS.as_uri())};\n"
        + _FAKE_FETCH
        + textwrap.dedent(body)
    )
    return [
        _node(),
        "--experimental-strip-types",
        "--no-warnings",
        "--import",
        (tmp_path / "register.mjs").as_uri(),
        str(script),
    ]


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
    return {**env, "HOME": str(home), "USERPROFILE": str(home), **extra}


def _run(argv: list[str], home: Path) -> str:
    done = subprocess.run(argv, env=_env(home), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


def test_a_session_python_stored_is_used_by_typescript(home, tmp_path) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 3600}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    out = _run(
        _script(
            tmp_path,
            """
            const token = await creds.accessToken("work", { server: "https://loom.example", fetch });
            const login = await creds.storedLogin("https://LOOM.example:443/", "work");
            console.log(JSON.stringify({ token, server: login?.server }));
            """,
        ),
        home,
    )
    assert json.loads(out) == {"token": "py-a1", "server": SERVER}


def test_a_refresh_typescript_made_is_used_by_python(home, tmp_path) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    out = _run(
        _script(tmp_path, 'console.log(await creds.accessToken("work", { fetch }));'),
        home,
    )
    assert out == "node-a1"
    entry = credentials._read()["profiles"]["work"]
    assert entry["session"] == "s1" and entry["marked"] is True
    assert entry["refresh_token"] == "node-r1"
    assert entry["expires_at"] > time.time() + 3500
    assert credentials.access_token("work", server=SERVER) == "node-a1"  # fresh: no refresh
    path = home / ".loom" / "credentials"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_python_waits_for_a_refresh_typescript_holds_the_lock_for(home, tmp_path) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    argv = _script(tmp_path, 'console.log(await creds.accessToken("work", { fetch }));')
    with subprocess.Popen(
        argv, env=_env(home, HOLD_MS="800"), stdout=subprocess.PIPE, text=True
    ) as node:
        assert node.stdout is not None
        assert node.stdout.readline().strip() == "refreshing"  # Node holds the lock now
        t0 = time.monotonic()
        with credentials._locked():
            waited = time.monotonic() - t0
            token = credentials._read()["profiles"]["work"]["access_token"]
        assert node.wait(timeout=60) == 0
    assert waited >= 0.4
    assert token == "node-a1"  # read under the lock: Node's refresh, not the spent one


def test_typescript_waits_for_a_lock_python_holds(home, tmp_path) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    argv = _script(
        tmp_path,
        """
        console.log("asking");
        const t0 = performance.now();
        const token = await creds.accessToken("work", { fetch });
        console.log(JSON.stringify({ token, waited: performance.now() - t0 }));
        """,
    )
    with credentials._locked():
        node = subprocess.Popen(argv, env=_env(home), stdout=subprocess.PIPE, text=True)
        assert node.stdout is not None
        assert node.stdout.readline().strip() == "asking"  # Node is at the lock now
        time.sleep(0.5)
        assert node.poll() is None  # still waiting
        # What a Python refresh under this lock stored: Node must read it, not refresh.
        fresh = {"access_token": "py-a2", "refresh_token": "py-r2", "expires_in": 3600}
        credentials._save("work", SERVER, fresh, session="s1", marked=True)
    out, _ = node.communicate(timeout=60)
    assert node.returncode == 0
    answer = json.loads(out.strip().splitlines()[-1])
    assert answer["token"] == "py-a2"
    assert answer["waited"] >= 400


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_typescript_interrupted_mid_refresh_releases_the_lock(home, tmp_path, sig) -> None:
    """Ended by a signal it has no handler for, Node still ends by that
    signal, and the lock it held is gone."""
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    argv = _script(tmp_path, 'console.log(await creds.accessToken("work", { fetch }));')
    lock = home / ".loom" / ".credentials.lock.d"
    with subprocess.Popen(
        argv, env=_env(home, HOLD_MS="30000"), stdout=subprocess.PIPE, text=True
    ) as node:
        assert node.stdout is not None
        assert node.stdout.readline().strip() == "refreshing"
        assert lock.exists()
        node.send_signal(sig)
        assert node.wait(timeout=30) == -sig
    assert not lock.exists()


def test_typescript_breaks_a_lock_a_killed_python_process_held_once_silent(home, tmp_path) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    hold = textwrap.dedent(
        """
        from loom import credentials
        held = credentials._locked()
        held.__enter__()
        print("held", flush=True)
        input()
        """
    )
    env = _env(home, PYTHONPATH=str(Path(credentials.__file__).parents[1]))
    with subprocess.Popen(
        [sys.executable, "-c", hold],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as py:
        assert py.stdout is not None
        assert py.stdout.readline().strip() == "held"
        py.kill()
        py.wait(timeout=30)
    lock = home / ".loom" / ".credentials.lock.d"
    assert lock.exists()  # left behind
    silent = time.time() - credentials._LOCK_SILENT_S - 1  # two heartbeats missed
    os.utime(lock, (silent, silent))
    t0 = time.monotonic()
    out = _run(_script(tmp_path, 'console.log(await creds.accessToken("work", { fetch }));'), home)
    assert out == "node-a1"
    assert time.monotonic() - t0 < 20  # not a wait for the stale age


def test_typescript_waits_for_a_python_breaker_s_claim_and_sweeps_its_leftovers(
    home, tmp_path
) -> None:
    tokens = {"access_token": "py-a1", "refresh_token": "py-r1", "expires_in": 30}
    credentials._save("work", SERVER, tokens, session="s1", marked=True)
    loom = home / ".loom"
    lock = loom / ".credentials.lock.d"
    lock.mkdir()
    (lock / "owner").write_text(json.dumps({"pid": 1, "host": "another-machine", "time": 0}))
    old = time.time() - credentials._LOCK_STALE_S - 1
    os.utime(lock, (old, old))
    claim = loom / ".credentials.lock.d.breaking"
    claim.mkdir()  # a Python waiter is breaking it
    for name in (".credentials.lock.d.0a.tmp", ".credentials.lock.d.0b.stale"):
        (loom / name).mkdir()
        os.utime(loom / name, (old, old))
    argv = _script(
        tmp_path, 'console.log("asking");\nconsole.log(await creds.accessToken("work", { fetch }));'
    )
    with subprocess.Popen(argv, env=_env(home), stdout=subprocess.PIPE, text=True) as node:
        assert node.stdout is not None
        assert node.stdout.readline().strip() == "asking"
        time.sleep(0.5)
        assert node.poll() is None  # waits for the claim
        claim.rmdir()  # that waiter gave up
        out, _ = node.communicate(timeout=60)
    assert node.returncode == 0
    assert out.strip().splitlines()[-1] == "node-a1"
    assert sorted(p.name for p in loom.glob(".credentials.lock.d*")) == []


def test_both_sdks_time_the_lock_alike(home, tmp_path) -> None:
    out = _run(
        _script(
            tmp_path,
            """
            const timings = [
              creds.LOCK_HEARTBEAT_MS,
              creds.LOCK_SILENT_MS,
              creds.LOCK_STALE_MS,
              creds.REFRESH_DEADLINE_MS,
              creds.RELEASE_WAIT_MS,
              creds.RENAME_WAIT_MS,
            ];
            console.log(JSON.stringify({ timings, renameTries: creds.RENAME_TRIES }));
            """,
        ),
        home,
    )
    py = [
        credentials._LOCK_HEARTBEAT_S,
        credentials._LOCK_SILENT_S,
        credentials._LOCK_STALE_S,
        credentials._REFRESH_DEADLINE_S,
        credentials._RELEASE_WAIT_S,
        credentials._RENAME_WAIT_S,
    ]
    assert json.loads(out) == {
        "timings": [round(s * 1000) for s in py],
        "renameTries": credentials._RENAME_TRIES,
    }
