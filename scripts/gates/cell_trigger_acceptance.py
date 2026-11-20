"""Run the fixed trigger acceptance workload on fresh, job-owned stores."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

GIB = 1024**3
SUPPORT = Path(__file__).resolve().parent / "testsupport/cell_trigger"

_CLICKHOUSE_READY = """import http.client
import sys

connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=2)
ready = False
try:
    connection.request(
        "POST", "/", body="SELECT 1",
        headers={"X-ClickHouse-User": "loom", "X-ClickHouse-Key": "loomdev"},
    )
    response = connection.getresponse()
    ready = response.status == 200 and response.read(3) == b"1\\n"
except (OSError, http.client.HTTPException):
    pass
finally:
    connection.close()
if ready:
    print("1")
"""


def require(ok: object, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def failure_site(error: BaseException | None) -> dict | None:
    if error is None:
        return None
    site = {"file": Path(__file__).name, "line": 0, "stage": "unknown"}
    trace = error.__traceback__
    while trace is not None:
        if (
            Path(trace.tb_frame.f_code.co_filename).resolve() == Path(__file__).resolve()
            and trace.tb_frame.f_code.co_name != "require"
        ):
            site.update(line=trace.tb_lineno, stage=trace.tb_frame.f_code.co_name[:64])
        trace = trace.tb_next
    return site


def source_hashes(root: Path) -> dict:
    paths = {
        name: SUPPORT / name
        for name in (
            "fixture.py",
            "native.py",
            "tls_proxy.py",
            "capture.py",
            "owned_children.py",
            "protocol.json",
        )
    }
    paths.update(
        driver=root / "bench/cell_trigger_gate.py",
        entrypoint=Path(__file__).resolve(),
        workflow=root / ".github/workflows/docker-smoke.yml",
    )
    return {name: sha(path) for name, path in paths.items()}


def write(path: Path, value: object, *, replace: bool = False) -> None:
    target = path.with_name(path.name + ".tmp") if replace else path
    with target.open("x", encoding="utf-8") as stream:
        os.chmod(target, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)


def source(root: Path) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True, timeout=10).strip()

    require(not git("status", "--porcelain"), "Acceptance requires a clean checkout")
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}


def resources(path: Path, available_gib: int = 10) -> None:
    available = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    require(available >= available_gib * 1024**2, "Acceptance available RAM floor")
    require(shutil.disk_usage(path).free >= 16 * GIB, "Acceptance requires 16 GiB free disk")


def ports(count: int) -> list[int]:
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def container_matches(actual: dict, expected: dict, label: str) -> bool:
    return (
        actual.get("Id") == expected["id"]
        and actual.get("Created") == expected["created"]
        and actual.get("Config", {}).get("Labels", {}).get("loom.ci.trigger") == label
    )


def native_cleanup_complete(folder: Path, fixture_id: str) -> None:
    fixture = folder / "fixture"
    if not fixture.exists():
        return  # Native validation never began; only the creator's stores exist.
    receipt = fixture / "outer-result.json"
    require(
        not receipt.is_symlink()
        and receipt.stat().st_uid == os.geteuid()
        and receipt.stat().st_size <= 65536,
        "Private native cleanup receipt",
    )
    value = json.loads(receipt.read_text())
    require(
        value.get("schema") == 2
        and value.get("fixture_id") == fixture_id
        and value.get("native_cleanup_complete") is True,
        "Native retirement, scratch cleanup or child reap remains incomplete",
    )


class Stores:
    def __init__(self, root: Path, folder: Path, fixture_id: str, run: dict) -> None:
        self.root, self.folder, self.fixture_id, self.run = root, folder, fixture_id, run
        self.items: list[dict] = []
        self.network = None
        self.prefix = "loom-trigger-" + fixture_id[:12]
        self.cancelled = 0
        self.terminal = False
        self.phase = "store_creation"
        self.creation_deadline = time.monotonic() + 900
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(folder),
            "LANG": "C.UTF-8",
            "DOCKER_CONFIG": str(folder / "docker"),
        }

    def save(self) -> None:
        write(
            self.folder / "stores.json",
            {
                "fixture_id": self.fixture_id,
                "ci_run": self.run,
                "items": self.items,
                "network": self.network,
            },
            replace=True,
        )

    def interrupted(self, signum: int, _frame: object) -> None:
        self.cancelled = self.cancelled or signum
        if self.terminal:
            raise SystemExit(128 + self.cancelled)

    def command(self, *argv: str, timeout: int = 120, cleanup: bool = False) -> str:
        if not cleanup:
            require(not self.cancelled, "Acceptance creation cancelled")
            resources(self.folder, 4)
            remaining = self.creation_deadline - time.monotonic()
            require(remaining > 0, "Owned store creation deadline")
            timeout = min(timeout, max(1, int(remaining)))
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout)
        require(len(result.stdout) + len(result.stderr) <= 8 * 1024**2, "Creator output bound")
        require(result.returncode == 0, "Owned store command failed: " + argv[0])
        return result.stdout.strip()

    def inspect(self, identity: str, *, cleanup: bool = False) -> dict:
        rows = json.loads(self.command("docker", "inspect", identity, cleanup=cleanup))
        require(len(rows) == 1, "Exactly one owned container")
        return rows[0]

    def create(self, role: str, args: list[str]) -> dict:
        name = self.prefix + "-" + role
        cidfile = self.folder / (role + ".cid")
        # The intent precedes Docker. A failed create is resolved by its unique label.
        intent = {"role": role, "name": name, "id": None, "created": None}
        self.items.append(intent)
        self.save()
        self.command(
            "docker",
            "create",
            "--name",
            name,
            "--cidfile",
            str(cidfile),
            "--label",
            "loom.ci.trigger=" + self.fixture_id,
            "--label",
            "loom.ci.run=" + self.run["run_id"],
            *args,
        )
        identity = cidfile.read_text().strip()
        require(re.fullmatch(r"[a-f0-9]{64}", identity), "Actual created container ID")
        actual = self.inspect(identity)
        require(
            actual["Name"] == "/" + name
            and actual["Config"]["Labels"]["loom.ci.trigger"] == self.fixture_id,
            "Created container identity",
        )
        intent.update(id=identity, created=actual["Created"])
        self.save()
        write(
            self.folder / (role + "-creator.json"),
            {
                "task": "cell-trigger-acceptance",
                "ci_run": self.run,
                "container_id": identity,
                "created_at": actual["Created"],
            },
        )
        resources(self.folder)
        self.command("docker", "start", identity)
        return intent

    def wait(self, phase: str, probe, seconds: int = 120) -> None:
        require(
            phase in {"primary_tcp", "standby_sync", "clickhouse_query"}, "Known readiness phase"
        )
        self.phase = phase
        end = time.monotonic() + seconds
        while True:
            require(not self.cancelled, "Owned store readiness cancelled")
            try:
                if probe():
                    return
            except (RuntimeError, subprocess.SubprocessError, ValueError):
                pass
            require(time.monotonic() < end, "Owned store readiness deadline")
            time.sleep(1)

    def _clickhouse_ready(self, port: int) -> bool:
        return (
            self.command(sys.executable, "-I", "-S", "-c", _CLICKHOUSE_READY, str(port), timeout=2)
            == "1"
        )

    def failure_diagnostics(self) -> dict:
        observations = []
        statuses = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
        for item in self.items:
            row = {"role": item["role"], "available": False}
            observations.append(row)
            if item["id"] is None:
                continue
            try:
                records = json.loads(
                    self.command("docker", "inspect", item["id"], timeout=5, cleanup=True)
                )
                require(
                    len(records) == 1 and container_matches(records[0], item, self.fixture_id),
                    "Diagnostic container ownership changed",
                )
                state = records[0]["State"]
                row.update(
                    available=True,
                    status=state["Status"] if state["Status"] in statuses else "unknown",
                    exit_code=state["ExitCode"] if type(state["ExitCode"]) is int else None,
                    oom_killed=state["OOMKilled"] if type(state["OOMKilled"]) is bool else None,
                )
                if item["role"] in {"pg-primary", "pg-standby"} and state["Running"] is True:
                    value = self.command(
                        "docker",
                        "exec",
                        item["id"],
                        "psql",
                        "-U",
                        "loom",
                        "-d",
                        "loom",
                        "-Atc",
                        "SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END "
                        "|| ':' || COALESCE((SELECT sync_state FROM pg_stat_replication "
                        "WHERE application_name='loom_standby'), 'absent')",
                        timeout=5,
                        cleanup=True,
                    )
                    recovery, sync = value.split(":")
                    if recovery in {"primary", "standby"} and sync in {
                        "sync",
                        "async",
                        "potential",
                        "quorum",
                        "absent",
                    }:
                        row["pg_state"] = {"recovery": recovery == "standby", "standby_sync": sync}
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
                KeyError,
            ) as error:
                row["diagnostic_error_type"] = type(error).__name__
        return {"phase": self.phase, "containers": observations}

    def start(self, ch_image: str) -> tuple[dict, dict, dict, list[int]]:
        selected = ports(8)
        ch_port, native_port, pg_port, standby_port = selected[:4]
        self.command("docker", "pull", ch_image, timeout=300)
        self.command("docker", "pull", "postgres:16", timeout=300)
        network_name = self.prefix + "-net"
        self.network = {"name": network_name, "id": None}
        self.save()
        self.network["id"] = self.command(
            "docker",
            "network",
            "create",
            "--label",
            "loom.ci.trigger=" + self.fixture_id,
            network_name,
        )
        self.save()
        memory_config = self.folder / "memory.xml"
        memory_config.write_text(
            "<clickhouse><max_server_memory_usage>4831838208</max_server_memory_usage></clickhouse>\n"
        )
        memory_config.chmod(0o644)
        ch_data = self.folder / "clickhouse-data"
        ch_data.mkdir(mode=0o777)
        ch_data.chmod(0o777)
        # Matching ownership avoids recursive chown through the read-only dictionary mount.
        ch_owner = ch_data.stat()
        mounts = [
            (ch_data, "/var/lib/clickhouse", False),
            (self.root / "deploy/lemmagen3", "/var/lib/clickhouse/lemmagen3", True),
            (self.folder / "memory.xml", "/etc/clickhouse-server/config.d/memory.xml", True),
        ]
        for filename, section in (
            ("lemmatizers.xml", "config.d"),
            ("nlp_user_settings.xml", "users.d"),
            ("keeper.xml", "config.d"),
            ("access_control.xml", "config.d"),
        ):
            mounts.append(
                (
                    self.root / "deploy/clickhouse-config" / filename,
                    "/etc/clickhouse-server/" + section + "/" + filename,
                    True,
                )
            )
        for filename, section in (
            ("log_queries_readonly.xml", "users.d"),
            ("custom_settings_prefixes.xml", "config.d"),
        ):
            mounts.append(
                (
                    self.root / "dev/clickhouse-ci" / filename,
                    "/etc/clickhouse-server/" + section + "/" + filename,
                    True,
                )
            )
        ch_args = [
            "--memory",
            str(5 * GIB),
            "--memory-swap",
            str(5 * GIB),
            "-p",
            f"127.0.0.1:{ch_port}:8123",
            "-p",
            f"127.0.0.1:{native_port}:9000",
            "-e",
            "CLICKHOUSE_DB=loom",
            "-e",
            f"CLICKHOUSE_UID={ch_owner.st_uid}",
            "-e",
            f"CLICKHOUSE_GID={ch_owner.st_gid}",
            "-e",
            "CLICKHOUSE_USER=loom",
            "-e",
            "CLICKHOUSE_PASSWORD=loomdev",
            "-e",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--ulimit",
            "nofile=262144:262144",
        ]
        for host, target, readonly in mounts:
            ch_args += ["-v", str(host) + ":" + target + (":ro" if readonly else "")]
        ch = self.create("clickhouse", [*ch_args, ch_image])
        pg_data = self.folder / "pg-data"
        pg_data.mkdir()
        pg = self.create(
            "pg-primary",
            [
                "--network",
                network_name,
                "--network-alias",
                "primary",
                "--memory",
                str(GIB),
                "--memory-swap",
                str(GIB),
                "-p",
                f"127.0.0.1:{pg_port}:5432",
                "-v",
                str(pg_data) + ":/var/lib/postgresql/data",
                "-e",
                "POSTGRES_USER=loom",
                "-e",
                "POSTGRES_PASSWORD=loomdev",
                "-e",
                "POSTGRES_DB=loom",
                "postgres:16",
                "-c",
                "wal_level=logical",
                "-c",
                "max_wal_senders=10",
                "-c",
                "max_replication_slots=10",
                "-c",
                "synchronous_commit=on",
                "-c",
                "shared_buffers=128MB",
            ],
        )
        psql = [
            "docker",
            "exec",
            pg["id"],
            "psql",
            "-U",
            "loom",
            "-d",
            "loom",
            "-Atc",
        ]
        self.wait(
            "primary_tcp",
            lambda: self.command(
                "docker",
                "exec",
                pg["id"],
                "pg_isready",
                "-U",
                "loom",
                "-d",
                "loom",
                "-h",
                "127.0.0.1",
            ),
        )
        self.phase = "replication_configuration"
        self.command(*psql, "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator'")
        self.command(
            "docker",
            "exec",
            pg["id"],
            "bash",
            "-c",
            "echo 'host replication replicator all scram-sha-256' >> \"$PGDATA/pg_hba.conf\"",
        )
        self.command(*psql, "ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (loom_standby)'")
        self.command(*psql, "SELECT pg_reload_conf()")
        standby_data = self.folder / "standby-data"
        standby_data.mkdir(mode=0o777)
        standby_data.chmod(0o777)
        standby_script = self.folder / "standby.sh"
        standby_script.write_text("""#!/usr/bin/env bash
set -euo pipefail
export PGDATA=/var/lib/postgresql/data
if [ "$(id -u)" = 0 ]; then
  chown postgres:postgres "$PGDATA"
  exec gosu postgres bash "$0"
fi
pg_basebackup -h primary -U replicator -D "$PGDATA" -R -X stream
connection="host=primary user=replicator password=replicator"
echo "primary_conninfo = '$connection application_name=loom_standby'" \
  >> "$PGDATA/postgresql.auto.conf"
chmod 700 "$PGDATA"
exec postgres -D "$PGDATA" -c shared_buffers=128MB
""")
        standby_script.chmod(0o644)
        standby = self.create(
            "pg-standby",
            [
                "--network",
                network_name,
                "--memory",
                str(GIB),
                "--memory-swap",
                str(GIB),
                "-p",
                f"127.0.0.1:{standby_port}:5432",
                "-v",
                str(standby_data) + ":/var/lib/postgresql/data",
                "-e",
                "PGPASSWORD=replicator",
                "-v",
                str(standby_script) + ":/standby.sh:ro",
                "--entrypoint",
                "bash",
                "postgres:16",
                "/standby.sh",
            ],
        )
        self.wait(
            "standby_sync",
            lambda: (
                self.command(
                    *psql,
                    (
                        "SELECT sync_state FROM pg_stat_replication WHERE "
                        "application_name='loom_standby'"
                    ),
                )
                == "sync"
            ),
        )
        self.wait("clickhouse_query", lambda: self._clickhouse_ready(ch_port))
        return ch, pg, standby, selected

    def observation(self, item: dict, role: str) -> dict:
        actual = self.inspect(item["id"])
        require(
            container_matches(actual, item, self.fixture_id) and actual["State"]["Running"],
            "Actual job-owned running store",
        )
        pid = actual["State"]["Pid"]
        fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
        creator = self.folder / (item["role"] + "-creator.json")
        mappings = [
            {
                "host": value["HostIp"],
                "host_port": int(value["HostPort"]),
                "internal_port": int(key.split("/")[0]),
                "protocol": key.split("/")[1],
            }
            for key, values in actual["NetworkSettings"]["Ports"].items()
            for value in values or []
        ]
        disk_path = (
            self.folder
            / {
                "clickhouse": "clickhouse-data",
                "pg_primary": "pg-data",
                "pg_standby": "standby-data",
            }[role]
        )
        return {
            "role": role,
            "container_id": item["id"],
            "container_name": actual["Name"].lstrip("/"),
            "created_at": actual["Created"],
            "owner_evidence": {"path": str(creator), "sha256": sha(creator)},
            "host_pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "uid": Path("/proc", str(pid)).stat().st_uid,
            "ports": mappings,
            "memory_limit_bytes": actual["HostConfig"]["Memory"],
            "server_limit_bytes": 9 * GIB // 2 if role == "clickhouse" else 0,
            "shared_buffers_bytes": 128 * 1024**2 if role != "clickhouse" else 0,
            "retained": False,
            "disk_path": str(disk_path),
        }

    def cleanup(self) -> list[str]:
        errors = []
        for item in reversed(self.items):
            try:
                if item["id"] is not None and not self.command(
                    "docker",
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    "id=" + item["id"],
                    "--format",
                    "{{.ID}}",
                    cleanup=True,
                ):
                    item["removed"] = True
                    self.save()
                    continue
                if item.get("removed"):
                    require(
                        not self.command(
                            "docker",
                            "ps",
                            "-a",
                            "--no-trunc",
                            "--filter",
                            "id=" + item["id"],
                            "--format",
                            "{{.ID}}",
                            cleanup=True,
                        ),
                        "Previously removed container reappeared",
                    )
                    continue
                # Unique name plus exact random label resolves a partial create only.
                actual = self.inspect(item["id"] or item["name"], cleanup=True)
                if item["id"] is None:
                    require(
                        actual["Name"] == "/" + item["name"]
                        and actual["Config"]["Labels"].get("loom.ci.trigger") == self.fixture_id,
                        "Partial create belongs to this job",
                    )
                    item.update(id=actual["Id"], created=actual["Created"])
                require(
                    container_matches(actual, item, self.fixture_id), "Cleanup ownership changed"
                )
                self.command("docker", "rm", "--force", "--volumes", item["id"], cleanup=True)
                remaining = self.command(
                    "docker",
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    "id=" + item["id"],
                    "--format",
                    "{{.ID}}",
                    cleanup=True,
                )
                require(not remaining, "Owned container remains after cleanup")
                item["removed"] = True
                self.save()
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
                KeyError,
            ) as error:
                errors.append(type(error).__name__ + ":" + item["role"])
        if self.network is not None:
            try:
                if self.network["id"] is not None and not self.command(
                    "docker",
                    "network",
                    "ls",
                    "--no-trunc",
                    "--filter",
                    "id=" + self.network["id"],
                    "--format",
                    "{{.ID}}",
                    cleanup=True,
                ):
                    self.network["removed"] = True
                    self.save()
                if self.network.get("removed"):
                    require(
                        not self.command(
                            "docker",
                            "network",
                            "ls",
                            "--no-trunc",
                            "--filter",
                            "id=" + self.network["id"],
                            "--format",
                            "{{.ID}}",
                            cleanup=True,
                        ),
                        "Previously removed network reappeared",
                    )
                else:
                    rows = json.loads(
                        self.command(
                            "docker",
                            "network",
                            "inspect",
                            self.network["id"] or self.network["name"],
                            cleanup=True,
                        )
                    )
                    require(
                        len(rows) == 1
                        and rows[0]["Labels"].get("loom.ci.trigger") == self.fixture_id
                        and (self.network["id"] is None or rows[0]["Id"] == self.network["id"]),
                        "Network cleanup ownership changed",
                    )
                    self.network["id"] = rows[0]["Id"]
                    self.command("docker", "network", "rm", rows[0]["Id"], cleanup=True)
                    self.network["removed"] = True
                    self.save()
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
                KeyError,
            ) as error:
                errors.append(type(error).__name__ + ":network")
        write(
            self.folder / "store-cleanup.json",
            {
                "removed": not errors,
                "errors": errors,
                "containers": [{"id": item["id"], "role": item["role"]} for item in self.items],
            },
            replace=True,
        )
        return errors


def dependency_receipt(root: Path, output: Path) -> dict:
    # The CI job uses the same ARM64 CPython ABI as the reviewed codec contract.
    sys.path.insert(0, str(SUPPORT))
    from fixture import CODEC_FILES, CODEC_VERSIONS

    value = {
        "schema": 2,
        "root": str(root),
        "python": str(root / ".venv/bin/python"),
        "python_sha256": sha(root / ".venv/bin/python"),
        "lockfile_sha256": sha(root / "uv.lock"),
        "versions": CODEC_VERSIONS,
        "codec_sources": {
            name: {"path": str(root / ".venv" / name), "sha256": sha(root / ".venv" / name)}
            for name in CODEC_FILES
        },
    }
    write(output, value)
    return {"path": str(output), "sha256": sha(output)}


def _progress_row(row, *, budget: bool) -> bool:
    if type(row) is not list or not row or not all(type(n) is int and 0 <= n < 2**63 for n in row):
        return False
    if budget:
        return (
            len(row) == 7
            and row[:2] == [0, 1]
            and row[2] in (0, 1)
            and row[4] <= 7020000
            and row[6] in (0, 1)
        )
    if row[0] == 0:
        return row == [0, 1, 100, 45500]
    if row[0] == 1:
        return (
            len(row) == 6
            and row[1] in (*range(1, 9), *range(10, 26))
            and row[2] <= 456
            and row[3] <= 45500
        )
    return len(row) == 4 and row[0] == 2 and row[1] <= 3216 and row[3] in (0, 1)


def _read_provenance_progress(path: Path, *, budget: bool):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
        "Owned regular provenance diagnostic",
    )
    cap, records = (512, 2) if budget else (823552, 3217)
    require(info.st_size <= cap, "Bounded provenance diagnostic")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            actual = os.fstat(stream.fileno())
            require(
                (actual.st_dev, actual.st_ino, actual.st_uid, actual.st_nlink)
                == (info.st_dev, info.st_ino, os.getuid(), 1)
                and stat.S_ISREG(actual.st_mode),
                "Stable provenance diagnostic",
            )
            data = stream.read(cap + 1)
    finally:
        os.close(fd)
    require(len(data) <= cap, "Bounded provenance diagnostic read")
    incomplete = int(bool(data) and not data.endswith(b"\n"))
    lines = data.splitlines()
    require(
        len(lines) <= records and all(len(line) + 1 <= 256 for line in lines),
        "Bounded provenance diagnostic records",
    )
    if incomplete:
        lines = lines[:-1]
    rows = [json.loads(line.decode("ascii")) for line in lines]
    require(all(_progress_row(row, budget=budget) for row in rows), "Numeric provenance records")
    if budget:
        require(all(row[2] == index for index, row in enumerate(rows)), "Ordered budget snapshots")
    elif rows:
        require(rows[0] == [0, 1, 100, 45500], "Provenance diagnostic header")
        require(
            all(row[0] == 1 for row in rows[1:-1]) and (len(rows) == 1 or rows[-1][0] in (1, 2)),
            "Ordered provenance records",
        )
        if rows[-1][0] == 2:
            require(rows[-1][1] == len(rows) - 1, "Provenance record count")
    return rows, incomplete


def _publish_provenance_progress(fixture: Path, destination: Path) -> None:
    diagnostic = None
    try:
        child = _read_provenance_progress(fixture / "provenance-progress.jsonl", budget=False)
        budget = _read_provenance_progress(fixture / "provenance-budget.jsonl", budget=True)
        if child is not None or budget is not None:
            diagnostic = {
                "schema": 1,
                "rows": child[0] if child else [],
                "budget": budget[0] if budget else [],
                "incomplete_tail": int(bool((child and child[1]) or (budget and budget[1]))),
            }
    except (OSError, ValueError, RuntimeError):
        diagnostic = {"schema": 1, "invalid": 1}
    if diagnostic is not None:
        try:
            write(destination / "provenance-progress.json", diagnostic)
        except OSError:
            # Missing diagnostics cannot replace the required work or cleanup result.
            pass


def publish(
    folder: Path,
    destination: Path,
    error: BaseException | None,
    *,
    phase: str,
    diagnostic_error_type: str | None,
) -> None:
    destination.mkdir(mode=0o700)
    # Raw native logs/settings contain bearer and bootstrap credentials: never upload them.
    fixture = folder / "fixture"
    for name in (
        "outer-result.json",
        "driver.jsonl",
        "provenance.json",
        "final-retirement.json",
        "teardown.json",
    ):
        path = fixture / name
        if path.is_file():
            shutil.copyfile(path, destination / name)
    for name in ("source-before.json", "source-after.json", "startup-diagnostics.json"):
        if (folder / name).is_file():
            shutil.copyfile(folder / name, destination / name)
    _publish_provenance_progress(fixture, destination)
    # Only the fixed numeric-only API diagnostic is public; raw API logs stay private.
    costs = fixture / "serving-cell/request-costs.jsonl"
    diagnostic = None
    try:
        if costs.is_file() and not costs.is_symlink():
            diagnostic = {"schema": 1, "invalid": 1}
            require(costs.stat().st_size <= 65536, "Bounded request cost diagnostic")
            rows = [json.loads(line) for line in costs.read_text(encoding="ascii").splitlines()]
            require(len(rows) <= 194, "Bounded request cost records")
            for row in rows:
                require(
                    type(row) is list and row and all(type(n) is int and n >= 0 for n in row),
                    "Numeric request cost record",
                )
                require(
                    (row[0] == 0 and len(row) == 8)
                    or (row[0] in (1, 2) and len(row) == 11)
                    or (row[0] == 3 and len(row) == 5),
                    "Closed request cost record",
                )
            diagnostic = {"schema": 1, "rows": rows}
    except (OSError, ValueError, RuntimeError):
        diagnostic = {"schema": 1, "invalid": 1}
    if diagnostic is not None:
        try:
            write(destination / "request-costs.json", diagnostic)
        except OSError:
            # Diagnostic storage cannot replace the work or cleanup outcome.
            pass
    intent_output = destination / "delivery-intent"
    for arm in range(10):
        for suffix in (".jsonl", "-receipt.json"):
            path = fixture / "delivery-intent" / (f"arm-{arm}" + suffix)
            if path.is_file() and not path.is_symlink():
                intent_output.mkdir(mode=0o700, exist_ok=True)
                shutil.copyfile(path, intent_output / path.name)
    if (folder / "store-cleanup.json").exists():
        shutil.copyfile(folder / "store-cleanup.json", destination / "store-cleanup.json")
    write(
        destination / "result.json",
        {
            "accepted": error is None,
            "error_type": type(error).__name__ if error is not None else "",
            "failure": failure_site(error),
            "phase": phase,
            "diagnostic_error_type": diagnostic_error_type,
            "utc": datetime.datetime.now(datetime.UTC).isoformat(),
        },
    )


def cleanup_only(root: Path, run: dict) -> int:
    state = Path(os.environ["RUNNER_TEMP"]) / "cell-trigger-state.json"
    if not state.exists():
        return 0  # The creator never published an intent and could not create a store.
    require(
        not state.is_symlink()
        and state.stat().st_uid == os.geteuid()
        and state.stat().st_size < 65536,
        "Owned CI cleanup pointer",
    )
    pointer = json.loads(state.read_text())
    fixture_id = pointer["fixture_id"]
    require(str(uuid.UUID(fixture_id)) == fixture_id, "Cleanup fixture UUID")
    folder = Path(os.environ["RUNNER_TEMP"]) / ("cell-trigger-" + fixture_id)
    require(
        folder.resolve() == folder
        and pointer == {"fixture_id": fixture_id, "folder": str(folder), "ci_run": run},
        "Cleanup pointer belongs to this exact CI attempt",
    )
    ledger = folder / "stores.json"
    require(
        not ledger.is_symlink()
        and ledger.stat().st_uid == os.geteuid()
        and ledger.stat().st_size < 65536,
        "Owned creator ledger",
    )
    data = json.loads(ledger.read_text())
    require(data["fixture_id"] == fixture_id and data["ci_run"] == run, "Current creator run")
    stores = Stores(root, folder, fixture_id, run)
    roles = {"clickhouse", "pg-primary", "pg-standby"}
    require(
        len(data["items"]) <= 3
        and len({item["role"] for item in data["items"]}) == len(data["items"]),
        "Bounded exact creator roles",
    )
    for item in data["items"]:
        require(
            item["role"] in roles
            and item["name"] == stores.prefix + "-" + item["role"]
            and (item["id"] is None or re.fullmatch(r"[a-f0-9]{64}", item["id"])),
            "Exact recorded container identity",
        )
    network = data["network"]
    require(network is None or network["name"] == stores.prefix + "-net", "Exact recorded network")
    stores.items, stores.network = data["items"], network
    native_cleanup_complete(folder, fixture_id)
    errors = stores.cleanup()
    destination = Path(os.environ["RUNNER_TEMP"]) / "cell-trigger-artifacts"
    destination.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(folder / "store-cleanup.json", destination / "store-cleanup.json")
    return int(bool(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clickhouse-image")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    require(
        os.environ.get("GITHUB_ACTIONS") == os.environ.get("CI") == "true", "GitHub Actions only"
    )
    root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    run = {
        "run_id": os.environ["GITHUB_RUN_ID"],
        "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "repository": os.environ["GITHUB_REPOSITORY"],
    }
    if args.cleanup_only:
        return cleanup_only(root, run)
    require(args.clickhouse_image, "Resolved ClickHouse release image")
    require(
        Path(sys.executable) == root / ".venv/bin/python" and sys.version_info[:2] == (3, 12),
        "Use the locked checkout Python 3.12",
    )
    before = source(root)
    resources(Path(os.environ["RUNNER_TEMP"]))
    fixture_id = str(uuid.uuid4())
    folder = Path(os.environ["RUNNER_TEMP"]) / ("cell-trigger-" + fixture_id)
    folder.mkdir(mode=0o700)
    os.umask(0o077)
    hashes_before = source_hashes(root)
    write(folder / "source-before.json", {**before, "source_hashes": hashes_before, "ci_run": run})
    stores = Stores(root, folder, fixture_id, run)
    stores.save()
    write(
        Path(os.environ["RUNNER_TEMP"]) / "cell-trigger-state.json",
        {"fixture_id": fixture_id, "folder": str(folder), "ci_run": run},
    )
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stores.interrupted)
    error = None
    diagnostic_error_type = None
    cleanup_complete = False
    try:
        ch, primary, standby, selected = stores.start(args.clickhouse_image)
        stores.phase = "dependency_metadata"
        resources(folder)
        dependencies = dependency_receipt(root, folder / "dependencies.json")
        from fixture import runtime

        spec = {
            "schema": 2,
            "fixture_id": fixture_id,
            "root": str(root),
            **before,
            "python": str(root / ".venv/bin/python"),
            "python_sha256": sha(root / ".venv/bin/python"),
            "lockfile_sha256": sha(root / "uv.lock"),
            "helper_path": str(SUPPORT / "owned_children.py"),
            "source_hashes": hashes_before,
            "workdir": str(folder / "fixture"),
            "ports": dict(zip(("api", "router", "probe", "tls"), selected[4:], strict=True)),
            "databases": {
                name: "trigger_" + fixture_id.replace("-", "") + "_" + str(index)
                for index, name in enumerate(
                    ("pg_cell", "pg_directory", "ch_schema", "ch_telemetry")
                )
            },
            "stores": {
                role: stores.observation(item, role)
                for item, role in (
                    (ch, "clickhouse"),
                    (primary, "pg_primary"),
                    (standby, "pg_standby"),
                )
            },
            "pg_admin_dsn": f"postgresql://loom:loomdev@127.0.0.1:{selected[2]}/postgres",
            "pg_standby_dsn": f"postgresql://loom:loomdev@127.0.0.1:{selected[3]}/postgres",
            "ch": {
                "host": "127.0.0.1",
                "port": selected[0],
                "username": "loom",
                "password": "loomdev",
                "keeper_prefix": "/keeper_map_tables",
            },
            "dependency_receipt": dependencies,
            "ci_run": run,
        }
        write(folder / "spec.json", spec)
        require(not stores.cancelled, "Cancelled before native acceptance")
        stores.phase = "native"
        require(runtime(spec) == 0, "Trigger acceptance did not pass")
        require(source(root) == before, "Acceptance source changed")
    except BaseException as caught:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        error = caught
        try:
            write(folder / "startup-diagnostics.json", stores.failure_diagnostics())
        except BaseException as diagnostic_error:  # noqa: BLE001 — diagnostic failure cannot replace the selected first error.
            diagnostic_error_type = type(diagnostic_error).__name__
    finally:
        # Native ownership closes first; exact created containers are removed even on failure.
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, stores.interrupted)
        try:
            native_cleanup_complete(folder, fixture_id)
            cleanup_errors = stores.cleanup()
            cleanup_complete = not cleanup_errors
            if cleanup_errors and error is None:
                error = RuntimeError("Owned store cleanup incomplete")
        except BaseException as caught:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            error = caught if error is None else error
        if stores.cancelled and error is None:
            error = InterruptedError("Acceptance interrupted")
        try:
            require(source(root) == before, "Final acceptance source changed")
            hashes_after = source_hashes(root)
            write(
                folder / "source-after.json",
                {**before, "source_hashes": hashes_after, "ci_run": run},
            )
            require(
                hashes_after == hashes_before, "Final driver, support or workflow bytes changed"
            )
        except BaseException as caught:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            error = caught if error is None else error
        try:
            publish(
                folder,
                Path(os.environ["RUNNER_TEMP"]) / "cell-trigger-artifacts",
                error,
                phase=stores.phase,
                diagnostic_error_type=diagnostic_error_type,
            )
        except BaseException as caught:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            error = caught if error is None else error
    stores.terminal = cleanup_complete
    if stores.cancelled and error is None:
        error = InterruptedError("Acceptance interrupted during publication")
    print(
        json.dumps(
            {
                "accepted": error is None,
                "error_type": type(error).__name__ if error is not None else "",
                "failure": failure_site(error),
                "phase": stores.phase,
                "diagnostic_error_type": diagnostic_error_type,
            }
        )
    )
    return int(error is not None or bool(stores.cancelled))


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as error:  # noqa: BLE001 — preflight failures must emit only nonsecret diagnostics.
        result = {
            "accepted": False,
            "error_type": type(error).__name__,
            "failure": failure_site(error),
        }
        print(json.dumps(result), flush=True)
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_TEMP"):
            output = Path(os.environ["RUNNER_TEMP"]) / "cell-trigger-artifacts"
            output.mkdir(mode=0o700, exist_ok=True)
            if not (output / "result.json").exists():
                write(output / "result.json", result)
        code = 1
    raise SystemExit(code)
