"""Builds N-1 and N side by side on one ClickHouse (a rolling upgrade).

Boots the base build (default: the merge base with origin/main) on an empty
ClickHouse, then this checkout, which applies its newer migrations, then the
base again: the N-1 replica restarting on N's schema. A third process runs
this checkout with a changed dashboard bundle, as N+1's frontend would be.
Then it writes from each build and reads from the other, and checks:

- memories, runs and conflict verdicts one build wrote read back whole on the
  other, and survive the other build's version writes;
- a column only a newer schema has (`ALTER ADD` here, as N+1's migration
  would) survives a version write by a build that does not know it
  (server/core/rmt_carry.py);
- a build that boots on a ledger with migrations it does not carry logs the
  WARNING naming them;
- every asset a build's dashboard page names loads from a replica of the other
  build (needs npm);
- after the cross-boot each build answers ready;
- each build answers `insights.query` on N's analyst entities, which N wrote
  over N-1's, and no logged statement text or error holds the scope key or
  its pads (both skipped where N-1 has no analysis plane);
- with every process on `LOOM_BACKGROUND_WORKERS=auto`, exactly one process
  runs the workers, and it is N+1: its tree carries one no-op migration more,
  so N hands the lease over to it.

The rules these check are docs/architecture/rolling-upgrades.md. ClickHouse
runs in a throwaway container (the CI image and config), so the gate needs
docker; without it, or `git`, it skips with exit 0, or fails with `--require`
(CI).

    python scripts/gates/mixed_version.py [--base REV] [--keep] [--require]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
CH_IMAGE = "clickhouse/clickhouse-server:26.5.1.882"
CH_PASSWORD = "loomdev"
JWT_SECRET = "mixed-version-gate-secret-" + "0" * 32
# How long the carrying processes may take to acknowledge a new column
# (server/core/rmt_carry.py `ACK_WAIT_S`).
CARRY_ACK_WAIT_S = 120.0
# The N+1 bundle: one line more in the entry module changes its hashed name.
NEXT_BUNDLE_LINE = '\nconsole.debug("mixed-version gate: the N+1 bundle");\n'
# How long a published asset or a settled lease may take to show.
SETTLE_S = 90.0


_PORTS: list[int] = []


def _free_port() -> int:
    if _PORTS:
        return _PORTS.pop(0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _skip(why: str, *, required: bool) -> int:
    if required:
        print(f"mixed-version gate cannot run: {why}")
        return 1
    print(f"mixed-version gate skipped: {why}")
    return 0


@contextlib.contextmanager
def clickhouse(keep: bool) -> Iterator[int]:
    port = _free_port()
    name = f"loom-mixed-{os.getpid()}"
    cfg = ROOT / "deploy" / "clickhouse-config"
    mounts = [
        (ROOT / "deploy" / "lemmagen3", "/var/lib/clickhouse/lemmagen3"),
        (cfg / "lemmatizers.xml", "/etc/clickhouse-server/config.d/lemmatizers.xml"),
        (cfg / "nlp_user_settings.xml", "/etc/clickhouse-server/users.d/nlp_user_settings.xml"),
        (cfg / "keeper.xml", "/etc/clickhouse-server/config.d/keeper.xml"),
    ]
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8123"]
    cmd += ["-e", "CLICKHOUSE_DB=loom", "-e", "CLICKHOUSE_USER=loom"]
    cmd += [
        "-e",
        f"CLICKHOUSE_PASSWORD={CH_PASSWORD}",
        "-e",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
    ]
    cmd += ["--ulimit", "nofile=262144:262144"]
    for src, dst in mounts:
        cmd += ["-v", f"{src}:{dst}:ro"]
    subprocess.run([*cmd, CH_IMAGE], check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2).status_code == 200:
                    break
            time.sleep(1)
        else:
            raise RuntimeError("ClickHouse did not start")
        yield port
    finally:
        if not keep:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def sql(
    port: int,
    query: str,
    params: dict[str, str] | None = None,
    settings: dict[str, Any] | None = None,
) -> str:
    """`query`'s answer; `params` bind its `{name:Type}` placeholders. A
    setting that changes how the statement is logged applies only when sent
    with the request, not in a SETTINGS clause."""
    bound = {f"param_{k}": v for k, v in (params or {}).items()}
    r = httpx.post(
        f"http://127.0.0.1:{port}/",
        params={"user": "loom", "password": CH_PASSWORD, **bound, **(settings or {})},
        content=query.encode(),
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse: {r.text[:400]}")
    return r.text.strip()


def worktree_copy(into: Path) -> Path:
    """This checkout's files, tracked and untracked (not ignored), uncommitted edits included."""
    names = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for name in filter(None, names):
        src = ROOT / name.decode()
        if src.is_file():
            dst = into / name.decode()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return into


def add_noop_migration(tree: Path) -> str:
    """One migration more than `tree` carries, so it boots with a higher
    schema_version, as N+1 would."""
    migrations = tree / "schemas" / "migrations"
    last = max(int(f.name[:3]) for f in migrations.glob("[0-9][0-9][0-9]_*.sql"))
    name = f"{last + 1:03d}_mixed_version_gate.sql"
    (migrations / name).write_text(
        "-- The mixed-version gate's N+1: no schema change.\nSELECT 1;\n"
    )
    return name


def build_frontend(tree: Path, *, node_modules: Path | None = None) -> bool:
    """`frontend/dist` for a tree that has none; False without npm."""
    front = tree / "frontend"
    if (front / "dist" / "index.html").exists():
        return True
    if not shutil.which("npm") or not (front / "package.json").exists():
        return False
    if not (front / "node_modules").exists():
        if node_modules is not None and node_modules.exists():
            (front / "node_modules").symlink_to(node_modules)
        else:
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=front, check=True)
    subprocess.run(["npm", "run", "build"], cwd=front, check=True, capture_output=True)
    return True


def base_tree(rev: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", rev], check=True, capture_output=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)
    return into


class Server:
    """One Loom process of one build; `restart` keeps its port."""

    def __init__(self, name: str, tree: Path, ch_port: int, logs: Path) -> None:
        self.name, self.tree, self.ch_port = name, tree, ch_port
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = logs / f"{name}.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.workers = "off"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
        env.update(
            PYTHONPATH=str(self.tree),
            LOOM_CH_HOST="127.0.0.1",
            LOOM_CH_PORT=str(self.ch_port),
            LOOM_CH_USER="loom",
            LOOM_CH_PASSWORD=CH_PASSWORD,
            LOOM_CH_SECURE="false",
            LOOM_AUTH_REQUIRED="true",
            LOOM_OAUTH_ENABLED="true",  # a sign-in on every build, so they answer each other's
            LOOM_JWT_SECRET=JWT_SECRET,
            LOOM_EMBEDDING_MODEL="fake-ngram-1024",
            LOOM_LLM_PROVIDER="fake",
            LOOM_BACKGROUND_WORKERS=self.workers,
            LOOM_LATENCY_BUDGET_MULTIPLIER="10",
            # Low enough that the fake embedder judges the two region facts close.
            LOOM_CONFLICT_THRESHOLD="0.5",
            LOOM_OTEL_ENABLED="false",
            LOOM_INSTANCE_LABEL=f"ci:mixed-version-{self.name}",
        )
        if self.workers != "off":
            env["LOOM_DEPLOYMENT"] = "1"  # one shared lease, not one per instance label
        return env

    def start(self) -> None:
        out = self.log.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.main:app"]
            + ["--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.tree,
            env=self.env(),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its workers child stops with it
        )
        deadline = time.monotonic() + 600  # an empty database replays the journal
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited at boot:\n{self.tail()}")
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{self.url}/v1/health", timeout=2).status_code == 200:
                    return
            time.sleep(1)
        raise RuntimeError(f"{self.name} never became healthy:\n{self.tail()}")

    def ready(self) -> tuple[bool, list[str]]:
        """Whether `/v1/health/ready` answers 200 within SETTLE_S, and the
        checks that failed last."""
        deadline = time.monotonic() + SETTLE_S
        failing: list[str] = []
        while True:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = httpx.get(f"{self.url}/v1/health/ready", timeout=10)
                if r.status_code == 200:
                    return True, []
                failing = list(r.json().get("failing") or [r.status_code])
            if time.monotonic() > deadline:
                return False, failing
            time.sleep(2)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(30)
        if self.proc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def tail(self, n: int = 6000) -> str:
        return self.log.read_text(errors="replace")[-n:] if self.log.exists() else ""

    def call(self, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
        r = httpx.post(
            f"{self.url}/v1/{op}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if r.status_code != 200:
            raise AssertionError(f"{self.name} {op}: {r.status_code} {r.text[:400]}")
        return dict(r.json())

    def page_assets(self) -> list[str]:
        """The `/assets/...` files this replica's dashboard page names."""
        page = httpx.get(f"{self.url}/", timeout=30).text
        return sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', page)))

    def leader(self) -> tuple[bool, str]:
        r = httpx.get(f"{self.url}/v1/health", params={"include": "workers"}, timeout=10)
        lease = dict(r.json().get("workers_leader") or {})
        reason = str(lease.get("reason") or "")
        return bool(lease.get("held")) and reason.startswith("holds "), reason


def mint(org: str, ns: str) -> str:
    os.environ.setdefault("LOOM_JWT_SECRET", JWT_SECRET)
    sys.path.insert(0, str(ROOT))
    import jwt

    from server.auth.jwt import mint_token

    raw = mint_token(
        subject="sa:mixed-version",
        org=org,
        namespaces=[ns],
        roles=["admin"],
        ttl_seconds=3600,
        secret=JWT_SECRET,
    )
    # Signed as kid `default` (the root secret), which every build verifies.
    claims = jwt.decode(raw, JWT_SECRET, algorithms=["HS256"], options={"verify_aud": False})
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256", headers={"kid": "default"})


class Scenario:
    """The checks, each run in both directions between the two builds."""

    def __init__(self, old: Server, new: Server, nxt: Server, ch_port: int) -> None:
        self.builds = {"N-1": old, "N": new}
        self.new = new
        self.next = nxt
        self.ch_port = ch_port
        self.org = f"mixed-{uuid.uuid4().hex[:8]}"
        self.ident = {"org": self.org, "namespace": "mixed", "agent": "gate"}
        self.token = mint(self.org, "mixed")
        self.failures: list[str] = []

    def check(self, cond: bool, what: str) -> None:
        print(("  ok    " if cond else "  FAIL  ") + what)
        if not cond:
            self.failures.append(what)

    def call(self, build: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.builds[build].call(self.token, op, body)

    def get(self, build: str, key: str) -> dict[str, Any]:
        out = self.call(build, "memory.get", {**self.ident, "memory_key": key})
        return dict(out.get("memory") or out)

    def run_all(self) -> list[str]:
        self.all_ready()
        ids = self.memories_read_across()
        for a, b in (("N", "N-1"), ("N-1", "N")):
            self.run_completes_across(a, b)
            self.verdict_survives(a, b)
        self.unknown_column_survives(ids)
        self.newer_ledger_warns()
        self.analysis_across()
        self.sign_in_across()
        self.dashboard_loads_across()
        self.one_leader()
        return self.failures

    def all_ready(self) -> None:
        for label, srv in {**self.builds, "N+1": self.next}.items():
            ok, failing = srv.ready()
            self.check(
                ok,
                f"{label} answers ready after the cross-boot"
                + (f" ({failing})" if failing else ""),
            )

    def memories_read_across(self) -> dict[str, str]:
        ids = {}
        for label in self.builds:
            body = {**self.ident, "memory_key": f"facts/{label}", "value": f"by {label}"}
            ids[label] = str(self.call(label, "memory.set", body)["memory_id"])
        for label, other in (("N", "N-1"), ("N-1", "N")):
            got = self.get(label, f"facts/{other}")
            self.check(got.get("value") == f"by {other}", f"{label} reads what {other} wrote")
        return ids

    def analysis_across(self) -> None:
        if not (self.builds["N-1"].tree / "server/insights/domain/analysis.py").exists():
            print("  skip  insights.query: N-1 has no analysis plane")
            return
        from server.insights.domain import analysis as boundary

        version = sql(
            self.ch_port,
            "SELECT value FROM system.settings_profile_elements "
            "WHERE profile_name = {user:String} AND setting_name = {name:String}",
            {"user": boundary.ANALYST_USER, "name": boundary.VERSION_SETTING},
        )
        self.check(
            version.strip("'\\") == str(boundary.ENTITY_VERSION),
            f"N wrote the analyst entities ({version})",
        )
        body = {"org": self.org, "namespace": "mixed", "sql": "SELECT count() FROM loom.memory"}
        # N first: it answers only after its own write, so N-1's answer is on N's entities.
        for label in ("N", "N-1"):
            srv = self.builds[label]
            # Each process answers once its own probe has passed.
            deadline = time.monotonic() + SETTLE_S
            while True:
                r = httpx.post(
                    f"{srv.url}/v1/insights.query",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=60,
                )
                if r.status_code == 200 or time.monotonic() > deadline:
                    break
                time.sleep(2)
            self.check(r.status_code == 200, f"{label} answers insights.query on N's entities")
        policy = sql(
            self.ch_port,
            "SELECT select_filter FROM system.row_policies WHERE short_name = {name:String} "
            "AND database = 'loom' AND table = 'memory'",
            {"name": boundary.POLICY_NAME},
        )
        literals = re.findall(r"unhex\(\\?'([0-9a-f]+)\\?'\)", policy)
        key = next((h for h in literals if len(h) == 64), "")
        self.check(bool(key), "the memory policy names the scope key")
        secrets = [*literals, *boundary.key_spellings(bytes.fromhex(key))]
        sql(self.ch_port, "SYSTEM FLUSH LOGS")
        logged = sql(
            self.ch_port,
            "SELECT count() FROM system.query_log WHERE "
            "multiSearchAnyCaseInsensitive(query, {k:Array(String)}) "
            "OR multiSearchAnyCaseInsensitive(formatted_query, {k:Array(String)}) "
            "OR multiSearchAnyCaseInsensitive(exception, {k:Array(String)})",
            {"k": "[" + ", ".join("'" + h.replace("\\", "\\\\") + "'" for h in secrets) + "]"},
            boundary.LOG_TEXT_CUT,
        )
        self.check(
            logged == "0", "no logged statement or error holds the scope key after the upgrade"
        )

    def run_completes_across(self, a: str, b: str) -> None:
        run_id = self.call(a, "run.begin", {**self.ident, "goal": f"begun on {a}"})["run_id"]
        done = {**self.ident, "run_id": run_id, "status": "completed"}
        self.call(b, "run.complete", done)
        run = self.call(a, "run.get", {**self.ident, "run_id": run_id})
        run = dict(run.get("run") or run)
        self.check(
            run.get("status") == "completed" and run.get("goal") == f"begun on {a}",
            f"a run begun on {a} and completed on {b} keeps its goal",
        )

    def verdict_survives(self, a: str, b: str) -> None:
        # Each direction has its own subject, so its pair is not judged against the other's.
        subject = "Logs" if a == "N" else "Backups"
        key = f"retention/{a}"
        for days in (30, 90):
            body = {**self.ident, "memory_key": f"{key}/{days}"}
            out = self.call(a, "memory.set", {**body, "value": f"{subject} are kept {days} days."})
        verdict = self.get(a, f"{key}/90")
        self.check(verdict.get("status") == "conflicted", f"{a} judges the second fact conflicted")
        tags = {"org": self.org, "memory_id": out["memory_id"], "tags": ["touched"]}
        self.call(b, "memory.update_by_id", tags)
        after = self.get(a, f"{key}/90")
        self.check(
            after.get("status") == "conflicted"
            and after.get("conflict_with") == verdict.get("conflict_with"),
            f"{a}'s conflict verdict survives {b}'s version write",
        )

    def unknown_column_survives(self, ids: dict[str, str]) -> None:
        """A column only a newer schema has (added here as N+1's migration
        would) survives a version write by a build that does not know it."""
        sql(self.ch_port, "ALTER TABLE loom.memory ADD COLUMN gate_probe String DEFAULT ''")
        sql(
            self.ch_port,
            "INSERT INTO loom._migrations (filename, checksum, applied_by) "
            "VALUES ('998_gate_probe.sql', 'gate', 'mixed-version-gate')",
        )
        # What N+1's boot does before it writes the column: wait for every carrying
        # process to acknowledge it. The version writes below then come at once.
        self.await_carry_acks()
        for label in self.builds:
            where = f"org = '{self.org}' AND memory_key = 'facts/{label}'"
            sql(
                self.ch_port,
                "INSERT INTO loom.memory SELECT * REPLACE "
                "('kept' AS gate_probe, version + 1 AS version) "
                f"FROM loom.memory FINAL WHERE {where}",
            )
            tags = {"org": self.org, "memory_id": ids[label], "tags": ["v2"]}
            self.call(label, "memory.update_by_id", tags)
            probe = sql(self.ch_port, f"SELECT gate_probe FROM loom.memory FINAL WHERE {where}")
            self.check(probe == "kept", f"{label}'s version write keeps a column it does not know")
        sql(self.ch_port, "ALTER TABLE loom.memory DROP COLUMN gate_probe")

    def await_carry_acks(self) -> None:
        """Every live `loom.carry_acks` row has loom.memory's current columns
        (the fingerprint of server/core/rmt_carry.py)."""
        names = sql(
            self.ch_port,
            "SELECT name FROM system.columns WHERE database = 'loom' AND table = 'memory' "
            "AND default_kind NOT IN ('MATERIALIZED', 'ALIAS', 'EPHEMERAL')",
        ).split("\n")
        want = hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]
        deadline = time.monotonic() + CARRY_ACK_WAIT_S
        while True:
            acks = sql(
                self.ch_port,
                "SELECT JSONExtractString(fingerprints, 'loom.memory') FROM loom.carry_acks FINAL "
                "WHERE at > now64(3) - toIntervalSecond(30) "
                "AND JSONHas(fingerprints, 'loom.memory') FORMAT TSV",
            )
            behind = [a for a in acks.split("\n") if a and a != want]
            if not behind:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"carrying processes never acknowledged the column: {behind}")
            time.sleep(0.5)

    def newer_ledger_warns(self) -> None:
        sql(
            self.ch_port,
            "INSERT INTO loom._migrations (filename, checksum, applied_by) "
            "VALUES ('999_gate_future.sql', 'gate', 'mixed-version-gate')",
        )
        self.new.restart()
        log = self.new.tail()
        self.check(
            "999_gate_future.sql" in log and "schema is newer than this build" in log,
            "N booting on a newer ledger logs the WARNING naming the file",
        )

    def sign_in_across(self) -> None:
        """A sign-in whose steps land on both builds fails cleanly, as a client
        can retry, and leaves the grant to the build that started it."""
        new, old = self.builds["N"], self.builds["N-1"]
        r = httpx.post(
            f"{new.url}/oauth/device_authorization", data={"client_id": "loom-cli"}, timeout=30
        )
        self.check(r.status_code == 200, f"N starts a device grant ({r.status_code})")
        if r.status_code != 200:
            return
        poll = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": r.json()["device_code"],
            "client_id": "loom-cli",
        }
        answer = httpx.post(f"{old.url}/oauth/token", data=poll, timeout=30)
        self.check(
            answer.status_code < 500, f"N-1 answers N's device poll cleanly ({answer.status_code})"
        )
        again = httpx.post(f"{new.url}/oauth/token", data=poll, timeout=30)
        pending = again.status_code == 400 and "authorization_pending" in again.text
        self.check(pending, "N's device grant outlives a poll N-1 answered")
        refresh = {
            "grant_type": "refresh_token",
            "refresh_token": "not-a-token",
            "client_id": "loom-cli",
        }
        for label, srv in self.builds.items():
            got = httpx.post(f"{srv.url}/oauth/token", data=refresh, timeout=30)
            refused = got.status_code == 400 and "invalid_grant" in got.text
            # A build that serves no sign-in has no token endpoint at all.
            self.check(
                refused or (label == "N-1" and got.status_code == 404),
                f"{label} refuses an unknown refresh as invalid_grant ({got.status_code})",
            )
            if label == "N-1" and "Loom-Sessions" in got.headers:
                print("  note  N-1 keeps sessions; a refresh it answers is not checked")
                return
        self.check("Loom-Sessions" not in answer.headers, "N-1's answers say they are N-1's")
        self.refresh_across(old.url, kept=True)
        self.refresh_across(new.url, kept=False)

    def refresh_across(self, url: str, *, kept: bool) -> None:
        """The SDK refreshing a session against `url`: N-1's refusal leaves it
        to ask again, N's ends it."""
        sys.path.insert(0, str(ROOT / "sdk" / "python"))
        from loom import credentials
        from loom.errors import AuthInvalid, BackendUnavailable, LoomError

        real = credentials.CREDENTIALS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            credentials.CREDENTIALS_FILE = Path(tmp) / "credentials"
            spent = {"access_token": "x", "refresh_token": "not-a-token", "expires_in": 0}
            credentials._save("gate", url, spent, session="s", marked=True)
            failed: Exception | None = None
            try:
                credentials.access_token("gate", refused="x")
            except LoomError as e:
                failed = e
            finally:
                stored = credentials.stored_login(url, "gate") is not None
                credentials.CREDENTIALS_FILE = real
        want = BackendUnavailable if kept else AuthInvalid
        label = "N-1" if kept else "N"
        self.check(
            isinstance(failed, want) and stored == kept,
            f"the SDK {'keeps' if kept else 'ends'} a session {label} refuses ({failed!r})",
        )

    def dashboard_loads_across(self) -> None:
        """A page served by one build asks any replica for its assets."""
        pages = {"N-1": self.builds["N-1"], "N": self.new, "N+1": self.next}
        built = {label: (srv.tree / "frontend" / "dist").exists() for label, srv in pages.items()}
        if not built["N"]:
            print("  note  no npm: the dashboard bundles are not built, nor checked")
            return
        for a, b in (("N", "N+1"), ("N+1", "N"), ("N", "N-1"), ("N-1", "N")):
            page, other = pages[a], pages[b]
            if not built[a]:
                print(f"  note  {a}'s bundle is not built; its page is not checked")
                continue
            assets = page.page_assets()
            missing = [p for p in assets if not self.resolves(other, p)]
            self.check(
                bool(assets) and not missing,
                f"every asset of {a}'s page loads from {b} ({len(assets)} files)"
                + (f"; missing {missing}" if missing else ""),
            )

    @staticmethod
    def resolves(srv: Server, path: str) -> bool:
        """200 for `path` on `srv` within SETTLE_S: a replica publishes after boot."""
        deadline = time.monotonic() + SETTLE_S
        while True:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{srv.url}{path}", timeout=10).status_code == 200:
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(2)

    def one_leader(self) -> None:
        """Every process on `auto`: one holds the workers lease, and it is N+1,
        the build with the higher schema_version."""
        servers = {"N-1": self.builds["N-1"], "N": self.new, "N+1": self.next}
        for srv in servers.values():
            srv.workers = "auto"
            srv.restart()
        # N takes the lease first and hands it over once N+1's beats name it. A
        # first beat goes out before its lease exists, so N+1 may hold it before
        # the older builds read a beat of N+1's naming it: wait for both.
        deadline = time.monotonic() + SETTLE_S
        steady = 0
        holders: list[str] = []
        reasons: dict[str, str] = {}
        while time.monotonic() < deadline and steady < 3:
            state = {k: s.leader() for k, s in servers.items()}
            holders = [k for k, (held, _) in state.items() if held]
            reasons = {k: why for k, (_, why) in state.items()}
            aside = all("to a newer build" in reasons[k] for k in ("N-1", "N"))
            steady = steady + 1 if holders == ["N+1"] and aside else 0
            time.sleep(2)
        self.check(
            holders == ["N+1"],
            f"one process of the newest build runs the workers ({holders}; {reasons})",
        )
        for label in ("N-1", "N"):
            self.check(
                "to a newer build" in reasons[label],
                f"{label} leaves the lease to N+1 ({reasons[label]})",
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--base", default="", help="the N-1 revision (default: merge base with origin/main)"
    )
    ap.add_argument("--keep", action="store_true", help="keep ClickHouse and the logs")
    ap.add_argument(
        "--port-base", type=int, default=0, help="use PORT..PORT+3 (default: free ports)"
    )
    ap.add_argument(
        "--require", action="store_true", help="fail instead of skipping when it cannot run"
    )
    args = ap.parse_args(argv)
    if args.port_base:
        _PORTS.extend(range(args.port_base, args.port_base + 4))
    if not shutil.which("docker") or not shutil.which("git"):
        return _skip("needs docker and git", required=args.require)
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return _skip("the docker daemon is not reachable", required=args.require)
    base = args.base
    if not base:
        mb = subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"],
            capture_output=True,
            text=True,
        )
        if mb.returncode != 0:
            return _skip(
                "no origin/main to take the base build from (pass --base)",
                required=args.require,
            )
        base = mb.stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="loom-mixed-"))
    failures: list[str] = []
    servers: list[Server] = []
    try:
        with clickhouse(args.keep) as ch_port:
            old = Server("base", base_tree(base, work / "base"), ch_port, work)
            new = Server("head", ROOT, ch_port, work)
            nxt = Server("next", worktree_copy(work / "next"), ch_port, work)
            servers += [old, new, nxt]
            print(f"next carries {add_noop_migration(nxt.tree)}")
            if build_frontend(ROOT):
                print("dashboard bundles: base, head, and head with one line more")
                build_frontend(old.tree)
                with (nxt.tree / "frontend" / "src" / "main.tsx").open("a") as f:
                    f.write(NEXT_BUNDLE_LINE)
                build_frontend(nxt.tree, node_modules=ROOT / "frontend" / "node_modules")
            print(f"base {base[:12]} boots on an empty ClickHouse")
            old.start()
            print("head boots and applies its migrations")
            new.start()
            print("base restarts on head's schema")
            old.restart()
            nxt.start()
            failures = Scenario(old, new, nxt, ch_port).run_all()
    except Exception:
        for s in servers:
            print(f"--- {s.name} log tail ---\n{s.tail()}")
        raise
    finally:
        for s in servers:
            s.stop()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"logs kept in {work}")
    if failures:
        print(f"mixed-version gate: {len(failures)} failure(s)")
        return 1
    print("mixed-version gate: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
