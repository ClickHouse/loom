#!/usr/bin/env python3
"""Render the chart and check the Postgres memory authority it configures: with
`postgres.host` set, every pod that gets the release's config connects with the
same password-less verify-full DSN, reads the password from the Secret as
PGPASSWORD and mounts the CA bundle its `sslrootcert` names, and the
NetworkPolicy lets it out on the Postgres port; without it, no pod carries any
of that. The names the block renders are refused anywhere else beside it.

    python -m scripts.gates.helm_postgres

Needs `helm` on PATH and PyYAML.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scripts.gates.helm_metadata_egress import (
    EVERYTHING,
    LOOM_PODS,
    loom_pods,
    render,
    sets,
    web_policies,
)

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"
CLOUD = ("-f", str(CHART / "values-eks.yaml"), "-f", str(CHART / "values-clickhouse-cloud.yaml"))
# The pods that get `loom.config`: every Loom pod but helm test's.
CONFIGURED = LOOM_PODS - {"Pod/loom-test-health"}
RENDERED = (
    "LOOM_STORAGE_AUTHORITY",
    "LOOM_PG_DSN",
    "LOOM_PG_DURABILITY",
    "LOOM_PG_POOL_MAX_SIZE",
    "PGPASSWORD",
)


def loom_container(pod: dict[str, Any]) -> dict[str, Any]:
    (container,) = pod["spec"]["containers"]
    return container


def check_configured(docs: list[dict[str, Any]], what: str) -> None:
    pods = loom_pods(docs)
    assert set(pods) >= CONFIGURED, f"{what}: found only {sorted(pods)}"
    for name in CONFIGURED:
        container = loom_container(pods[name])
        env = {e["name"]: e for e in container["env"]}
        assert env["LOOM_STORAGE_AUTHORITY"]["value"] == "postgres", (what, name)
        assert env["LOOM_PG_DURABILITY"]["value"] == "synchronous", (what, name)
        assert env["LOOM_PG_POOL_MAX_SIZE"]["value"] == "5", (what, name)
        dsn = urlsplit(env["LOOM_PG_DSN"]["value"])
        assert dsn.password is None, f"{what}: {name}'s DSN carries a password"
        assert (dsn.username, dsn.hostname, dsn.port, dsn.path) == (
            "loom",
            "loom-postgres.example.com",
            5432,
            "/loom",
        ), (what, name, dsn)
        query = parse_qs(dsn.query)
        assert query["sslmode"] == ["verify-full"], (what, name, query)
        assert env["PGPASSWORD"]["valueFrom"] == {
            "secretKeyRef": {"name": "loom-postgres", "key": "password"}
        }, (what, name)
        # The CA file the DSN names is the caSecret key, mounted read-only.
        (rootcert,) = query["sslrootcert"]
        mounts = [m for m in container["volumeMounts"] if rootcert.startswith(m["mountPath"] + "/")]
        assert len(mounts) == 1 and mounts[0].get("readOnly"), (what, name, mounts)
        volumes = {v["name"]: v for v in pods[name]["spec"]["volumes"]}
        secret = volumes[mounts[0]["name"]]["secret"]
        key = rootcert.removeprefix(mounts[0]["mountPath"] + "/")
        assert secret == {
            "secretName": "loom-postgres-ca",
            "items": [{"key": key, "path": key}],
        }, (what, name, secret)
        assert key == "ca.crt", (what, name)


def egress_ports(docs: list[dict[str, Any]]) -> set[int]:
    return {
        p["port"]
        for spec in web_policies(docs)
        for rule in spec.get("egress", [])
        for p in rule.get("ports", [])
    }


def check_unconfigured(docs: list[dict[str, Any]], what: str) -> None:
    for name, pod in loom_pods(docs).items():
        for container in pod["spec"]["containers"]:
            names = {e["name"] for e in container.get("env", [])}
            assert not names & set(RENDERED), f"{what}: {name} has {names & set(RENDERED)}"
        assert "pg-ca" not in {v["name"] for v in pod["spec"].get("volumes", [])}, (what, name)
    assert 5432 not in egress_ports(docs), what


def refused(want: str, *args: str) -> None:
    cmd = ["helm", "template", "loom", str(CHART), *CLOUD, *sets("secrets.jwtSecret=" + "j" * 40)]
    done = subprocess.run([*cmd, *args], capture_output=True, text=True)
    assert done.returncode != 0, f"the chart accepted {args}"
    assert want in done.stderr, f"unexpected refusal for {args}: {done.stderr}"


def main() -> None:
    docs = render(*CLOUD, *sets(*EVERYTHING))
    check_configured(docs, "cloud overlay")
    assert 5432 in egress_ports(docs), "cloud overlay: no egress to Postgres"
    # Off, the one egress-only policy opens every port already: the block leaves it as is.
    off = sets("networkPolicy.enabled=false", *EVERYTHING)
    eks = ("-f", str(CHART / "values-eks.yaml"))
    assert web_policies(render(*CLOUD, *off)) == web_policies(render(*eks, *off)), "policy off"
    # Another port reaches the DSN and the policy as the same number.
    moved = render(*CLOUD, *sets("postgres.port=6432", *EVERYTHING))
    dsn = next(
        e["value"]
        for e in loom_container(loom_pods(moved)["Deployment/loom"])["env"]
        if e["name"] == "LOOM_PG_DSN"
    )
    assert urlsplit(dsn).port == 6432 and 6432 in egress_ports(moved), dsn
    check_unconfigured(render(*sets("networkPolicy.enabled=true", *EVERYTHING)), "defaults")
    check_unconfigured(render(*eks, *sets(*EVERYTHING)), "eks")
    # Without the block, these names are the documented way to set the authority.
    render(*eks, *sets("env.LOOM_PG_DSN=x", "extraEnv[0].name=PGPASSWORD", "extraEnv[0].value=x"))
    for name in RENDERED:
        want = f"set {name} through postgres.*"
        refused(f"{want}, not env", *sets(f"env.{name}=x"))
        refused(f"{want}, not secrets.extra", *sets(f"secrets.extra.{name}=x"))
        refused(f"{want}, not extraEnv", *sets(f"extraEnv[0].name={name}", "extraEnv[0].value=x"))
    refused("postgres.host needs postgres.caSecret.name", *sets("postgres.caSecret.name="))
    for field, bad in (("user", "lo@m"), ("database", "a/b"), ("host", "h?x")):
        refused(f"postgres.{field} goes into the DSN as written", *sets(f"postgres.{field}={bad}"))
    for field in ("storage_authority", "pg_dsn", "pg_durability", "pg_pool_max_size"):
        refused(
            f"set {field} through postgres.*, not settingsFile", *sets(f"settingsFile.{field}=x")
        )
    for secret in ("passwordSecret", "caSecret"):
        refused(f"postgres.{secret}.key must be a Secret key", *sets(f"postgres.{secret}.key="))
    for port in ("0", "05432", "70000", "99999999999999999999", "5432?x"):
        refused("postgres.port must be a port number", "--set-string", f"postgres.port={port}")
    refused(
        "postgres.host needs postgres.passwordSecret.name", *sets("postgres.passwordSecret.name=")
    )
    print("postgres: every configured pod connects over verify-full; refusals hold")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(f"postgres: {e}")
